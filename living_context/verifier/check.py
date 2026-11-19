"""The independent erasure verifier (DESIGN 14.7, 14.6 step 4, 19; BUILD-PLAN Step 14).

Written to share nothing with the executor: it does not import `erasure/` or the gateway's handler, admission, validator or
lineage scan; it builds its own query bodies and reads every index of the tenant by direct reads with its own read-only
credential (an `EsClient` constructed by the caller from the verifier's own key file; the transport is the only thing shared).

Canaries are planted by the tests through the real writer: a short unique string, a long one (over 2 KB, so Elasticsearch stores it
compressed), a canary vector, a canary subject token, a canary principal in the authz index and a canary line in plaintext logs.
`positive_control` must find every canary by every method (text search, kNN with the canary vector, get by id, lineage query,
authz read, log grep with its control marker) before an absence counts; `residue` then runs the same probes after the erasure and
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


@dataclass
class Report:
    steps: list = field(default_factory=list)       # (step name, probes run, found)
    found: dict = field(default_factory=dict)       # method -> count
    log_control_found: bool = False

    @property
    def probes(self) -> int:
        return sum(n for _, n, _ in self.steps)

    @property
    def total(self) -> int:
        return sum(self.found.values())


def _phrase(s: str) -> str:
    return '"' + ESC.sub(r"\\\1", s) + '"'


class Verifier:
    def __init__(self, es: EsClient, tenant: str, *, log_paths: Iterable[Path] = (), segment_dir: Optional[Path] = None):
        self._es, self.tenant = es, tenant
        self._logs, self._seg = [Path(p) for p in log_paths], segment_dir

    def _search(self, index: str, body: dict) -> list:
        st, r = self._es.request("POST", f"/{index}/_search?ignore_unavailable=true&allow_no_indices=true", body)
        if st != 200:
            raise VerifierError(f"direct read failed ({st})")
        return r["hits"]["hits"]

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
        for f in VECTOR_FIELDS:
            hits = self._search(self._all, {"size": 20, "_source": False, "knn": {"field": f, "query_vector": list(vector),
                                                                                  "k": 20, "num_candidates": 200}})
            n += sum(1 for h in hits if self._near(h["_score"]))
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
        st, _ = self._es.request("POST", f"/{self._all}/_refresh")
        if st != 200:   # reading unrefreshed data could report a leftover as gone: a step that cannot run is an error
            raise VerifierError(f"refresh before the probes failed ({st})")
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
        if self._logs:
            erased = [n for n in (canaries.short, canaries.long) if n]
            g = self._logs_grep(erased + ([canaries.log_marker] if canaries.log_marker else []))
            rep.log_control_found = bool(canaries.log_marker) and g.get(canaries.log_marker, 0) > 0
            step("logs", len(erased), sum(g[n] for n in erased))
        return rep

    def positive_control(self, canaries: Canaries) -> Report:
        """Before the erasure: every method must find its canary. Raises if one is blind (the verifier is then worthless)."""
        rep = self.probe(canaries)
        for name, probes, found in rep.steps:
            if probes and name != "logs" and found == 0:
                raise VerifierError(f"positive control failed: {name} found nothing")
        if self._logs and canaries.log_marker and not rep.log_control_found:
            raise VerifierError("positive control failed: the log grep did not find its control marker")
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
        return rep
