"""The independent erasure verifier (DESIGN 14.7, 14.6 step 4, 19; BUILD-PLAN Step 14).

Written to share nothing with the executor: it does not import `erasure/` or the gateway's handler, admission, validator or
lineage scan; it builds its own query bodies and reads every index of the tenant by direct reads with its own read-only
credential (an `EsClient` constructed by the caller from the verifier's own key file; the transport is the only thing shared).

Canaries are planted by the tests through the real writer: a short unique string, a long one (over 2 KB, so Elasticsearch stores it
compressed), a canary vector, a canary subject token, a canary principal in the authz index and a canary line in plaintext logs.
`positive_control` must find every canary by every method (text search, kNN with the canary vector, get by id, lineage query,
authz read, percolation of the canary document against the stored queries, a person named only in query text, what those
searches shaped, log grep with its control marker) before an absence counts; `residue` then runs the same probes after the erasure and
counts what it finds. Both reports carry the step list and the number of probes run; a step that cannot run is an error, never a
skip (LAB-R8). A raw-file grep over segment files is NOT a check (stored fields are compressed): `segment_grep` exists only so the
tests can show that such a verifier misses the long canary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..gateway.es import EsClient

ESC = re.compile(r'([+\-=&|><!(){}\[\]^"~*?:\\/])')
VECTOR_FIELDS = ("embedding", "summary_embedding")
SIMILARITY = 0.98   # cosine; int8-quantised HNSW scores a copy of the canary vector at about 0.998, not exactly 1.0 (observed 0.9978 after merges and deletions)


class VerifierError(Exception):
    pass


@dataclass
class Canaries:
    short: str = ""
    long: str = ""
    vector: Sequence[float] = ()
    subject_token: str = ""
    leaf_ids: Sequence[str] = ()
    principal: str = ""
    log_marker: str = ""          # the control marker: never an erased canary, must always be found in the logs
    data_marker: str = ""         # the control marker in a file under the data root: must always be found there
    percolate_text: str = ""      # a canary document: percolated against the stored queries, which must match none after erasure
    query_subject_token: str = "" # a person who appears only in query text (another person's query mentions them)
    search_ids: Sequence[str] = ()  # the searches that mention the query-only subject: what they shaped (edges, members) is residue


@dataclass
class Report:
    steps: list = field(default_factory=list)       # (step name, probes run, found)
    found: dict = field(default_factory=dict)       # method -> count
    log_control_found: bool = False
    data_control_found: bool = False

    @property
    def probes(self) -> int:
        return sum(n for _, n, _ in self.steps)

    @property
    def total(self) -> int:
        return sum(self.found.values())


def _phrase(s: str) -> str:
    return '"' + ESC.sub(r"\\\1", s) + '"'


def grep_data_roots(roots: Iterable[Path], needles: Iterable[str]) -> dict:
    """Number of files under the roots whose raw bytes hold each needle. Every file is read (a symbolic link is not followed), so a
    root that cannot be read is an error and never a clean result."""
    needles = [n for n in needles if n]
    found = {n: 0 for n in needles}
    raw = {n: n.encode() for n in needles}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            raise VerifierError("data root is not a directory")
        for f in sorted(root.rglob("*")):
            if f.is_symlink() or not f.is_file():
                continue
            data = f.read_bytes()
            for n in needles:
                found[n] += raw[n] in data
    return found


def _complete(st, r, what: str) -> dict:
    """The verifier's OWN check that a search answer is complete (DESIGN 14.7: it shares no read path with the gateway): HTTP 200, not
    timed out or terminated early, every shard successful and none failed, a hits section. Anything else is a step that cannot run,
    never an absence (review F13)."""
    if st != 200 or not isinstance(r, dict) or r.get("timed_out") is not False or r.get("terminated_early"):
        raise VerifierError(f"{what}: not a complete read ({st})")
    sh = r.get("_shards")
    if not isinstance(sh, dict) or sh.get("failed") != 0 or not isinstance(sh.get("total"), int) or sh.get("successful") != sh.get("total"):
        raise VerifierError(f"{what}: not every shard answered")
    if not isinstance(r.get("hits"), dict) or not isinstance(r["hits"].get("hits"), list):
        raise VerifierError(f"{what}: no hits section")
    return r


def _exact_total(r: dict, what: str) -> int:
    total = r["hits"].get("total")
    if not isinstance(total, dict) or total.get("relation") != "eq" or not isinstance(total.get("value"), int):
        raise VerifierError(f"{what}: the total is not exact")
    return total["value"]


def _refreshed(st, r, what: str) -> None:
    """A refresh that every primary ran: HTTP 200 and a `_shards` section with integer counts and no failed shard."""
    sh = r.get("_shards") if isinstance(r, dict) else None
    if st != 200 or not isinstance(sh, dict) or sh.get("failed") != 0 or not isinstance(sh.get("total"), int) \
            or not isinstance(sh.get("successful"), int):
        raise VerifierError(f"{what}: refresh failed ({st})")


def _listed(st, r, what: str) -> list:
    """The index names of a `_resolve/index` answer: HTTP 200 and an `indices` list whose every item carries a string name. A failed
    or malformed listing is never "no index of the pool" (review round 6: an item without a name was skipped)."""
    rows = r.get("indices") if st == 200 and isinstance(r, dict) else None
    if not isinstance(rows, list) or not all(isinstance(x, dict) and isinstance(x.get("name"), str) and x["name"] for x in rows):
        raise VerifierError(f"{what}: the index listing failed ({st})")
    return [x["name"] for x in rows]


class Verifier:
    def __init__(self, es: EsClient, tenant: str, *, log_paths: Iterable[Path] = (), segment_dir: Optional[Path] = None,
                 data_roots: Iterable[Path] = ()):
        self._es, self.tenant = es, tenant
        self._logs, self._seg = [Path(p) for p in log_paths], segment_dir
        self._roots = [Path(p) for p in data_roots]

    def _search(self, index: str, body: dict) -> list:
        return _complete(*self._es.request(
            "POST", f"/{index}/_search?ignore_unavailable=true&allow_no_indices=true&allow_partial_search_results=false", body),
            "direct read")["hits"]["hits"]

    @property
    def _all(self) -> str:
        return f"lc-{self.tenant}-*"

    def _text(self, needle: str) -> int:
        """Search every field of every index of the tenant for the phrase (stored and indexed text)."""
        hits = self._search(self._all, {"size": 100, "_source": False,
                                        "query": {"query_string": {"query": _phrase(needle), "default_field": "*",
                                                                   "lenient": True, "analyze_wildcard": False}}})
        return len(hits)

    def _knn(self, vector: Sequence[float]) -> int:
        n = 0
        self.knn_best = 0.0   # the best score seen, for the error message of a blind control
        for f in VECTOR_FIELDS:
            hits = self._search(self._all, {"size": 20, "_source": False, "knn": {"field": f, "query_vector": list(vector),
                                                                                  "k": 20, "num_candidates": 200,
                                                                                  "rescore_vector": {"oversample": 3.0}}})
            n += sum(1 for h in hits if self._near(h["_score"]))
            self.knn_best = max([self.knn_best] + [h["_score"] for h in hits])
        return n

    @staticmethod
    def _near(score: float) -> bool:
        # Elasticsearch scores cosine as (1 + cos) / 2; a copy of the canary vector scores 1.0
        return score >= (1 + SIMILARITY) / 2

    def _by_id(self, leaf_ids: Sequence[str]) -> int:
        values = [f"{lid}:{v}" for lid in leaf_ids for v in range(1, 4)] + list(leaf_ids)
        return len(self._search(self._all, {"size": 100, "_source": False, "query": {"ids": {"values": values}}})) if values else 0

    def _lineage(self, canaries: Canaries) -> int:
        should = []
        if canaries.subject_token:
            should.append({"term": {"lin_subjects": canaries.subject_token}})
        if canaries.leaf_ids:
            should.append({"terms": {"lin_leaf_ids": list(canaries.leaf_ids)}})
            should.append({"terms": {"lin_nodes": list(canaries.leaf_ids)}})
        if not should:
            return 0
        return len(self._search(self._all, {"size": 100, "_source": False, "query": {"bool": {"should": should, "minimum_should_match": 1}}}))

    def _percolate(self, text: str) -> int:
        """Percolate the canary document against every stored-query index of the tenant (DESIGN 14.7, E0/E1): the stored queries
        that still match it. A stored query is a lexical percolator query, so it is found by its words, not by a text field."""
        return len(self._search(f"lc-{self.tenant}-*-sq", {"size": 100, "_source": False,
                                                           "query": {"percolate": {"field": "q", "document": {"text": text}}}}))

    def _query_only(self, token: str) -> int:
        """Rows, stored queries and anything else that names a person only because a query mentioned them."""
        return len(self._search(self._all, {"size": 100, "_source": False, "query": {"term": {"lin_subjects": token}}}))

    def _query_shaped(self, search_ids: Sequence[str]) -> int:
        """Artefacts shaped by those searches: an edge that cites them as evidence, a member placed by them, lineage by query."""
        ids = list(search_ids)
        return len(self._search(self._all, {"size": 100, "_source": False, "query": {"bool": {"minimum_should_match": 1, "should": [
            {"terms": {"evidence": ids}}, {"terms": {"shaped_by": ids}}, {"terms": {"lin_queries": ids}}, {"terms": {"search_id": ids}}]}}}))

    def _authz(self, principal: str) -> int:
        return len(self._search(f"lc-{self.tenant}-authz", {"size": 100, "_source": False, "query": {"query_string": {
            "query": _phrase(principal), "default_field": "*", "lenient": True}}})) if principal else 0

    def _logs_grep(self, needles: Iterable[str]) -> dict:
        found = {n: 0 for n in needles}
        for p in self._logs:
            text = p.read_text(errors="replace")
            for n in found:
                found[n] += text.count(n)
        return found

    def _data_root(self, needles: Iterable[str]) -> dict:
        """E1: a raw-bytes grep of every file the gateway, its workers and the model port write under the run's data root
        (outside Elasticsearch's own data directory). A model-output store outside the indices is found here, not by lineage."""
        return grep_data_roots(self._roots, needles)

    def segment_grep(self, needle: str) -> int:
        """NOT a verifier: counts files under the segment directory that hold the bytes of `needle`. Compressed stored fields hide
        a long value from it (LAB-R6); the tests show it missing the long canary."""
        if self._seg is None:
            raise VerifierError("no segment directory")
        raw = needle.encode()
        return sum(1 for f in self._seg.rglob("*") if f.is_file() and raw in f.read_bytes())

    # ---- the checks ---------------------------------------------------------------------------------------------------
    def probe(self, canaries: Canaries) -> Report:
        """Run every probe once and count what is found."""
        # reading unrefreshed data could report a leftover as gone: an error, not a skip
        _refreshed(*self._es.request("POST", f"/{self._all}/_refresh"), "refresh before the probes")
        rep = Report()

        def step(name: str, probes: int, found: int):
            rep.steps.append((name, probes, found))
            rep.found[name] = found

        texts = [t for t in (canaries.short, canaries.long) if t]
        step("text", len(texts), sum(self._text(t) for t in texts))
        step("knn", 1 if len(canaries.vector) else 0, self._knn(canaries.vector) if len(canaries.vector) else 0)
        step("get_by_id", 1 if canaries.leaf_ids else 0, self._by_id(canaries.leaf_ids))
        step("lineage", 1, self._lineage(canaries))
        step("authz", 1 if canaries.principal else 0, self._authz(canaries.principal))
        if canaries.percolate_text:
            step("percolate", 1, self._percolate(canaries.percolate_text))
        if canaries.query_subject_token:
            step("query_only", 1, self._query_only(canaries.query_subject_token))
        if canaries.search_ids:
            step("query_shaped", 1, self._query_shaped(canaries.search_ids))
        if self._logs:
            erased = [n for n in (canaries.short, canaries.long) if n]
            g = self._logs_grep(erased + ([canaries.log_marker] if canaries.log_marker else []))
            rep.log_control_found = bool(canaries.log_marker) and g.get(canaries.log_marker, 0) > 0
            step("logs", len(erased), sum(g[n] for n in erased))
        if self._roots:
            g = self._data_root([n for n in (canaries.short, canaries.data_marker) if n])
            rep.data_control_found = bool(canaries.data_marker) and g.get(canaries.data_marker, 0) > 0
            step("data_root", 1 if canaries.short else 0, g.get(canaries.short, 0))
        return rep

    def positive_control(self, canaries: Canaries) -> Report:
        """Before the erasure: every method must find its canary. Raises if one is blind (the verifier is then worthless)."""
        rep = self.probe(canaries)
        for name, probes, found in rep.steps:
            if probes and name not in ("logs", "data_root") and found == 0:   # those two are proved by their control markers
                raise VerifierError(f"positive control failed: {name} found nothing" +
                                    (f" (best score {getattr(self, 'knn_best', 0):.4f})" if name == "knn" else ""))
        if self._logs and canaries.log_marker and not rep.log_control_found:
            raise VerifierError("positive control failed: the log grep did not find its control marker")
        if self._roots and canaries.data_marker and not rep.data_control_found:
            raise VerifierError("positive control failed: the data-root grep did not find its control marker")
        return rep

    def residue(self, canaries: Canaries, before: Report) -> Report:
        """After the erasure: the same probes, valid only against a positive control of the same shape and step count."""
        rep = self.probe(canaries)
        if [(n, p) for n, p, _ in rep.steps] != [(n, p) for n, p, _ in before.steps]:
            raise VerifierError("the residue run is not the shape of the positive control")
        if sum(p for _, p, _ in rep.steps) == 0:
            raise VerifierError("no checks were run")
        if self._logs and not rep.log_control_found:
            raise VerifierError("the log grep lost its control marker")
        if self._roots and canaries.data_marker and not rep.data_control_found:
            raise VerifierError("the data-root grep lost its control marker")
        return rep


def pool_residue(es: EsClient, tenant: str, pool: Optional[str] = None, leaf_ids: Sequence[str] = (), *, texts: Sequence[str] = ()) -> dict:
    """The independent residue check of a deleted pool (POOLS 9.4; its own code, the verifier's own key, no shared path with the
    executor): the indices of the tenant whose name carries the pool id, the documents of every index that name the pool in `pool` or
    `lin_pools` or a leaf of it in `lin_leaf_ids`, and a phrase search for each canary text. Everything must be zero after the delete;
    run it first against a pool that exists (the positive control) so a skipped index cannot read as clean."""
    _refreshed(*es.request("POST", f"/lc-{tenant}-*/_refresh?ignore_unavailable=true"), "pool residue")
    names = _listed(*es.request("GET", f"/_resolve/index/lc-{tenant}-*"), "pool residue")
    out = {"indices": sum(1 for n in names if pool and f"-{pool}-" in n), "docs": 0, "by_index": {}, "text": 0}
    should = [{"term": {"pool": pool}}, {"term": {"lin_pools": pool}}] if pool else []
    if leaf_ids:
        should.append({"terms": {"lin_leaf_ids": sorted(leaf_ids)}})
    if not should:
        raise VerifierError("nothing to check")
    path = f"/lc-{tenant}-*/_search?ignore_unavailable=true&allow_partial_search_results=false"
    r = _complete(*es.request("POST", path, {
        "size": 0, "track_total_hits": True, "query": {"bool": {"should": should, "minimum_should_match": 1}},
        "aggs": {"by": {"terms": {"field": "_index", "size": 200}}}}), "pool residue search")
    out["docs"] = _exact_total(r, "pool residue search")
    buckets = (r.get("aggregations") or {}).get("by", {}).get("buckets")
    if not isinstance(buckets, list):
        raise VerifierError("pool residue search: no per-index counts")
    out["by_index"] = {b["key"]: b["doc_count"] for b in buckets}
    for t in texts:
        r = _complete(*es.request("POST", path, {"size": 0, "track_total_hits": True, "query": {"match_phrase": {"text": t}}}),
                      "pool residue text search")
        out["text"] += _exact_total(r, "pool residue text search")
    return out
