"""A tiny in-memory stand-in for the parts of Elasticsearch the pool reader uses (filter-context search, get, mget, count, terms
aggregation). It evaluates only term, terms, range, exists and bool; anything else is an error, so a test that sends a scored
query fails loudly instead of being silently ignored. Not a model of Elasticsearch: the real behaviour is in the `_es` tests."""
import re

from living_context.gateway.indices import physical_name

SHARDS_ONE = {"total": 1, "successful": 1, "failed": 0}


def doc_answer(method, index, did, found, source=None, *, created=False):
    """The answer Elasticsearch 9 gives a single-document request (the shapes tests/security/test_pools_rescue_es.py pins on the
    cluster): every answer names the concrete `_index` and the `_id`; a delete says `deleted` or `not_found`; a read says `found`."""
    if method == "DELETE":
        return (200 if found else 404), {"_index": index, "_id": did, "_version": 2, "result": "deleted" if found else "not_found",
                                         "_shards": SHARDS_ONE, "_seq_no": 3, "_primary_term": 1}
    if method == "GET":
        return (200, {"_index": index, "_id": did, "_version": 1, "_seq_no": 1, "_primary_term": 1, "found": True,
                      "_source": dict(source)}) if found else (404, {"_index": index, "_id": did, "found": False})
    return (201 if created else 200), {"_index": index, "_id": did, "_version": 1, "result": "created" if created else "updated",
                                       "_shards": SHARDS_ONE, "_seq_no": 1, "_primary_term": 1}


def behind(alias):
    """The physical index the fakes answer for a request through an alias (`indices.physical_name` with a fixed suffix); a name that is
    already physical answers as itself."""
    return alias if re.search(r"-p[0-9a-f]{8}$", alias) else physical_name(alias, b"\xd0\xc5\x70\x4e")


def unconfirmed_deletes(index, did, *, strict=True):
    """Answers to `DELETE /<index>/_doc/<did>` that confirm nothing (review round 5). At f16ea94 every 404 read as "already deleted"
    and every 200 as deleted, so each of these certified a document's removal while it was still there. `strict`: the request named a
    concrete index (an erasure, by the hit's `_index`), so even the physical form of the name is another index."""
    other = index.rsplit("-", 1)[0] + "-other"
    out = {
        "404 with an empty body": (404, {}),
        "404 not_found for another _id": (404, {"_index": index, "_id": did + "x", "result": "not_found"}),
        "404 not_found for another index": (404, {"_index": other, "_id": did, "result": "not_found"}),
        "404 not_found without _index": (404, {"_id": did, "result": "not_found"}),
        "404 index_not_found naming another index": (404, {"error": {"type": "index_not_found_exception", "index": other,
                                                                     "resource.id": other}, "status": 404}),
        "404 that says deleted": (404, {"_index": index, "_id": did, "result": "deleted"}),
        "404 with a list body": (404, []),
        "200 with an empty body": (200, {}),
        "200 that says not_found": (200, {"_index": index, "_id": did, "result": "not_found"}),
        "200 that says noop": (200, {"_index": index, "_id": did, "result": "noop"}),
        "200 deleted for another _id": (200, {"_index": index, "_id": did + "x", "result": "deleted"}),
        "200 deleted for another index": (200, {"_index": other, "_id": did, "result": "deleted"}),
    }
    if strict:
        sib = physical_name(index, b"\x0a\x0b\x0c\x0d")
        out["404 not_found for the physical form of the name"] = (404, {"_index": sib, "_id": did, "result": "not_found"})
        out["200 deleted for the physical form of the name"] = (200, {"_index": sib, "_id": did, "result": "deleted"})
    return out


def conflict(index):
    """The typed version conflict Elasticsearch 9 answers a lost compare-and-set or an `op_type=create` on an existing id: HTTP 409,
    `version_conflict_engine_exception`, naming the physical index (pinned on the cluster by test_pools_rescue_es.py)."""
    return 409, {"error": {"type": "version_conflict_engine_exception", "reason": "version conflict", "index": index},
                 "status": 409}


def unconfirmed_puts(index, did):
    """Answers to `PUT /<index>/_doc/<did>` (through the alias `index`) that confirm no write (review round 6, F18 and F19). At 4e7ee85
    the authz replacement and the taint write accepted any 200 or 201 on its status alone, so each of these reported a write that may
    never have landed; a bare or foreign 409 is not the typed conflict a compare-and-set loop may retry on."""
    phys, other = behind(index), index.rsplit("-", 1)[0] + "-other"
    seq = {"_shards": SHARDS_ONE, "_seq_no": 4, "_primary_term": 1, "_version": 2}
    return {
        "200 with an empty body": (200, {}),
        "201 with an empty body": (201, {}),
        "200 with a list body": (200, []),
        "200 updated for another _id": (200, {"_index": phys, "_id": did + "x", "result": "updated", **seq}),
        "200 updated for another index": (200, {"_index": other, "_id": did, "result": "updated", **seq}),
        "200 updated without _index": (200, {"_id": did, "result": "updated", **seq}),
        "200 that says noop": (200, {"_index": phys, "_id": did, "result": "noop", **seq}),
        "200 that says deleted": (200, {"_index": phys, "_id": did, "result": "deleted", **seq}),
        "200 updated without sequence fields": (200, {"_index": phys, "_id": did, "result": "updated", "_shards": SHARDS_ONE}),
        "201 created on no successful shard": (201, {"_index": phys, "_id": did, "result": "created", "_seq_no": 4, "_primary_term": 1,
                                                    "_shards": {"total": 1, "successful": 0, "failed": 1}}),
        "409 with an empty body": (409, {}),
        "409 conflict naming another index": conflict(other),
    }


class FakeEs:
    def __init__(self):
        self.docs = {}      # index -> {id: source}
        self.requests = []  # (method, path, body)

    def put(self, index, doc_id, source):
        self.docs.setdefault(index, {})[doc_id] = dict(source)

    def _match(self, src, q):
        (k, v), = q.items()
        if k == "term":
            (f, x), = v.items()
            x = x.get("value") if isinstance(x, dict) else x
            return src.get(f) == x
        if k == "terms":
            (f, xs), = v.items()
            have = src.get(f)
            have = have if isinstance(have, list) else [have]
            return any(h in xs for h in have)
        if k == "exists":
            return v["field"] in src
        if k == "range":
            (f, r), = v.items()
            x = src.get(f)
            return x is not None and all({"gt": x > b, "gte": x >= b, "lt": x < b, "lte": x <= b}[o] for o, b in r.items())
        if k == "bool":
            fl = v.get("filter", [])
            fl = fl if isinstance(fl, list) else [fl]
            mn = v.get("must_not", [])
            mn = mn if isinstance(mn, list) else [mn]
            sh = v.get("should", [])
            return all(self._match(src, c) for c in fl) and not any(self._match(src, c) for c in mn) and (not sh or any(self._match(src, c) for c in sh))
        raise AssertionError(f"fake ES cannot evaluate {k}")

    def request(self, method, path, body=None, **kw):
        self.requests.append((method, path, body))
        m = re.match(r"^/([^/]+)/(_doc/(.+)|_mget|_search|_count)$", path)
        assert m, path
        index = m.group(1)
        docs = self.docs.get(index, {})
        if m.group(2).startswith("_doc/"):   # answered by the physical index behind the alias, as Elasticsearch does
            did = m.group(3)
            if did not in docs:
                return 404, {"_index": behind(index), "_id": did, "found": False}
            return 200, {"_index": behind(index), "found": True, "_id": did, "_source": docs[did], "_seq_no": 1, "_primary_term": 1}
        if m.group(2) == "_mget":
            return 200, {"docs": [({"_index": behind(index), "_id": i, "found": True, "_source": docs[i], "_seq_no": 1, "_primary_term": 1}
                                   if i in docs else {"_index": behind(index), "_id": i, "found": False}) for i in body["ids"]]}
        hits = [(i, s) for i, s in docs.items() if self._match(s, body["query"])]
        shards = {"total": 1, "successful": 1, "skipped": 0, "failed": 0}   # a complete answer, as Elasticsearch gives
        if m.group(2) == "_count":
            return 200, {"count": len(hits), "_shards": shards}
        out = {"timed_out": False, "_shards": shards, "hits": {"hits": [{"_id": i, "_source": s} for i, s in hits[: body.get("size", 10)]]}}
        if "aggs" in body:
            out["aggregations"] = {}
            for name, a in body["aggs"].items():
                f = a["terms"]["field"]
                counts = {}
                for _, s in hits:
                    counts[s.get(f)] = counts.get(s.get(f), 0) + 1
                out["aggregations"][name] = {"buckets": [{"key": k, "doc_count": c} for k, c in sorted(counts.items(), key=str)]}
        return 200, out


class FakeAuthz:
    """The tenant's authz index as a dict, with what the authz paths rely on (the shapes tests/security/test_pools_rescue_es.py pins):

    * PUT, GET and DELETE of one document, answered about the physical index behind the alias (`behind`), with the document's own
      sequence number (one counter for the index, primary term 1). A PUT may be conditional, as on the cluster: `op_type=create` lands
      only on an absent document, `if_seq_no` and `if_primary_term` only on the document at exactly that sequence number; otherwise the
      typed 409 (`conflict`) and nothing is written. A DELETE leaves nothing behind (a deleted document is absent again).
    * a `terms kind` search in a point in time (insertion order stands for `_shard_doc`); every hit names the physical index.

    `down` makes every call fail (a blocked index). `doc_answers[(method, id)]` gives the exact (status, body) of that document request
    instead, without touching the record (a malformed answer). `hold` (a set of ids, or True for every id) answers a PUT with 503 and
    keeps it: `land_late()` applies it later, its conditions checked then (a write that timed out at the client and still reached the
    index); `late_refused` counts the late writes the index refused. `rewrite_hits(hits)`, when set, replaces the hits of every search
    page (a foreign or malformed hit)."""

    def __init__(self):
        self.docs, self.down, self.doc_answers = {}, False, {}
        self.seqs, self.seq = {}, 0
        self.hold, self.late, self.late_refused = set(), [], 0
        self.rewrite_hits = None

    def land_late(self):
        late, self.late = self.late, []
        for path, did, phys, body in late:
            if self._put(path, did, phys, body)[0] == 409:
                self.late_refused += 1

    def _put(self, path, did, phys, body):
        qs = dict(x.split("=", 1) for x in path.partition("?")[2].split("&") if "=" in x)
        have = did in self.docs
        if (qs.get("op_type") == "create" and have) or ("if_seq_no" in qs and (
                not have or str(self.seqs.get(did, 0)) != qs["if_seq_no"] or qs.get("if_primary_term") != "1")):
            return conflict(phys)
        self.seq += 1
        self.docs[did], self.seqs[did] = body, self.seq
        return (200 if have else 201), {"_index": phys, "_id": did, "_version": self.seq, "result": "updated" if have else "created",
                                        "_shards": SHARDS_ONE, "_seq_no": self.seq, "_primary_term": 1}

    def request(self, method, path, body=None, **kw):
        if self.down:
            return 503, {}
        p = path.split("?")[0]
        if p.endswith("/_pit"):   # a point in time: the snapshot is the record ids in insertion order (one shard)
            self.pits = getattr(self, "pits", {})
            pid = f"pit{len(self.pits)}"
            self.pits[pid] = (list(self.docs), behind(p[1:-len("/_pit")]))
            return 200, {"id": pid, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0}}
        if p == "/_pit" and method == "DELETE":
            return 200, {"succeeded": getattr(self, "pits", {}).pop(body["id"], None) is not None, "num_freed": 1}
        did = path.split("/_doc/")[1].split("?")[0] if "/_doc/" in path else None
        if did is not None and (method, did) in self.doc_answers:
            return self.doc_answers[(method, did)]
        phys = behind(p.split("/_doc/")[0].lstrip("/")) if did is not None else None
        if method == "PUT":
            if self.hold is True or did in self.hold:
                self.late.append((path, did, phys, body))
                return 503, {}
            return self._put(path, did, phys, body)
        if method == "GET":
            if did not in self.docs:
                return 404, {"_index": phys, "_id": did, "found": False}
            return 200, {"_index": phys, "_id": did, "_version": self.seqs.get(did, 0) + 1, "_seq_no": self.seqs.get(did, 0), "_primary_term": 1,
                         "found": True, "_source": self.docs[did]}
        if method == "DELETE":
            found = self.docs.pop(did, None) is not None
            self.seqs.pop(did, None)
            self.seq += 1
            return (200 if found else 404), {"_index": phys, "_id": did, "_version": self.seq, "result": "deleted" if found else "not_found",
                                             "_shards": SHARDS_ONE, "_seq_no": self.seq, "_primary_term": 1}
        q = body["query"]
        kinds = q["terms"]["kind"] if "terms" in q else [q["term"]["kind"]]
        ids, phys = self.pits[body["pit"]["id"]] if "pit" in body else (list(self.docs), None)
        keys = [i for i in ids if i in self.docs and self.docs[i].get("kind") in kinds]   # insertion order stands for `_shard_doc`
        start = (int(body["search_after"][0]) + 1) if body.get("search_after") else 0
        size = int(body.get("size", 10))
        self.searches = getattr(self, "searches", 0) + 1
        page = list(enumerate(keys))[start:start + size]
        hits = [{"_index": phys, "_id": i, "_source": self.docs[i], "sort": [n]} for n, i in page]
        if self.rewrite_hits is not None:
            hits = self.rewrite_hits(hits)
        return 200, {"timed_out": False, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},   # a complete answer, as ES gives
                     "hits": {"hits": hits}}


class ShardedFake(FakeEs):
    """Several one-shard indices behind one pattern, with the two cursors Elasticsearch offers a multi-index scan:

    * `sort: _doc` gives each hit its shard-LOCAL document number, so the first document of every index has sort value [0]; a
      `search_after: [k]` continues after k in every shard, so a page boundary that falls between equal values skips the rest (F12);
    * a point in time (`POST /<pattern>/_pit`, then `POST /_search` with `pit` and `sort: _shard_doc`) gives every hit a value unique
      across the snapshot (shard number and document number), so `search_after` neither skips nor repeats.

    `partial` makes the next N search answers HTTP 200 with a failed shard; `alias_status` answers `GET /<name>/_alias` with that
    error status; `alias_answers[name]` and `pit_answers[name]` give the exact (status, body) of `GET /<name>/_alias` and
    `POST /<name>/_pit` (a malformed answer); `doc_answers[(method, name, id)]` gives the exact (status, body) of that document request
    without touching the document. A document request answers with the concrete index (`doc_answer`). A missing name answers the 404 Elasticsearch gives, naming it (`index`, `resource.id`;
    the shape tests/security/test_pools_rescue_es.py checks on the cluster). Not a model of Elasticsearch beyond these cursors: the
    real behaviour is in the `_es` tests."""

    def __init__(self):
        super().__init__()
        self.aliases, self.pits, self.partial, self.alias_status, self.closed_pits = {}, {}, 0, None, []
        self.alias_answers, self.pit_answers, self.doc_answers = {}, {}, {}

    def _indices(self, pattern):
        import fnmatch
        return sorted(n for n in self.docs for part in pattern.split(",")
                      if fnmatch.fnmatchcase(n, part) or part in self.aliases.get(n, ()))

    def _shards(self, n):
        if self.partial:
            self.partial -= 1
            return {"total": max(n, 1), "successful": max(n, 1) - 1, "skipped": 0, "failed": 1}
        return {"total": n, "successful": n, "skipped": 0, "failed": 0}

    def request(self, method, path, body=None, **kw):
        self.requests.append((method, path, body))
        p, _, qs = path.partition("?")
        if method == "DELETE" and p == "/_pit":
            self.closed_pits.append(body["id"])
            return 200, {"succeeded": self.pits.pop(body["id"], None) is not None, "num_freed": 1}
        m = re.match(r"^/([^/]+)/(_refresh|_pit|_search|_alias|_doc/(.+))$", p)
        if p == "/_search":
            pit = self.pits[body["pit"]["id"]]
            return self._page(pit, body, unique=True)
        assert m, path
        target, op = m.group(1), m.group(2)
        if op == "_alias":
            if self.alias_status is not None:
                return self.alias_status, {"error": {"type": "unavailable_shards_exception"}}
            if target in self.alias_answers:
                return self.alias_answers[target]
            hit = {n: {"aliases": {a: {} for a in sorted(al)}} for n, al in self.aliases.items() if target in al}
            if target in self.docs:
                hit[target] = {"aliases": {a: {} for a in sorted(self.aliases.get(target, ()))}}
            return (200, hit) if hit else self._missing(target)
        if op.startswith("_doc/"):
            did = m.group(3)
            if (method, target, did) in self.doc_answers:
                return self.doc_answers[(method, target, did)]
            phys = self._indices(target)
            if not phys:
                return self._missing(target)
            for n in phys:
                if did in self.docs[n]:
                    if method == "DELETE":
                        del self.docs[n][did]
                    return doc_answer(method, n, did, True, self.docs[n].get(did))
            return doc_answer(method, phys[0], did, False)
        phys = self._indices(target)
        if op == "_refresh":
            return 200, {"_shards": self._shards(len(phys))}
        if op == "_pit":
            if target in self.pit_answers:
                return self.pit_answers[target]
            if not phys and "ignore_unavailable=true" not in qs and "*" not in target:
                return self._missing(target)
            pid = f"pit{len(self.pits) + len(self.closed_pits)}"
            self.pits[pid] = [(n, list(self.docs[n].items())) for n in phys]   # the snapshot
            return 200, {"id": pid, "_shards": self._shards(len(phys))}
        return self._page([(n, list(self.docs[n].items())) for n in phys], body, unique=False)

    @staticmethod
    def _missing(name):
        return 404, {"error": {"type": "index_not_found_exception", "reason": f"no such index [{name}]", "resource.type": "index_or_alias",
                               "resource.id": name, "index_uuid": "_na_", "index": name}, "status": 404}

    def _page(self, snapshot, body, *, unique):
        hits = []
        for shard, (name, docs) in enumerate(snapshot):
            for local, (did, src) in enumerate(docs):
                if self._match(src, body["query"]) if "match_all" not in body["query"] else True:
                    key = [shard * 2 ** 31 + local] if unique else [local]
                    hits.append((key, shard, name, did, src))
        hits.sort(key=lambda h: (h[0], h[1]))
        if body.get("search_after") is not None:
            hits = [h for h in hits if h[0] > list(body["search_after"])]
        size = body.get("size", 10)
        fields = body.get("_source")
        page = [{"_index": name, "_id": did, "sort": key,
                 "_source": {k: v for k, v in src.items() if fields is None or fields is True or k in fields}}
                for key, _, name, did, src in hits[:size]]
        return 200, {"timed_out": False, "_shards": self._shards(len(snapshot)), "hits": {"hits": page}}


class DocStore:
    """Documents by (index, id) with the write semantics the writer relies on: `op_type=create` answers 409 when the id exists,
    `if_seq_no`/`if_primary_term` is a compare-and-set (409 on a mismatch), GET answers `_seq_no`/`_primary_term`, refresh is a no-op.
    `fail` maps a (method, index-substring) to a status answered once per entry in its list (an injected HTTP error). Document answers
    name the physical index behind the alias the request named (`behind`), as Elasticsearch does. `doc_answers[(method, index, id)]` is a
    list of exact (status, body) answers given once each, in order, before the document request is served normally."""

    def __init__(self):
        self.docs, self.seq, self.fail, self.sent, self.doc_answers = {}, 0, {}, [], {}

    def request(self, method, path, body=None, **kw):
        p, _, qs = path.partition("?")
        self.sent.append((method, p))
        for (m, sub), statuses in list(self.fail.items()):
            if m == method and sub in p and statuses:
                return statuses.pop(0), {"error": {"type": "injected"}}
        if p.endswith("/_refresh"):
            return 200, {"_shards": {"total": 1, "successful": 1, "failed": 0}}
        if p == "/_mget":
            out = []
            for d in body["docs"]:
                cur = self.docs.get((d["_index"], d["_id"]))
                out.append({"_index": behind(d["_index"]), "_id": d["_id"], "found": cur is not None,
                            **({"_source": dict(cur[0]), "_seq_no": cur[1], "_primary_term": 1} if cur else {})})
            return 200, {"docs": out}
        if p.endswith("/_search") and "sort" not in body:   # a filter of `term` clauses (the class reader's searches)
            index = p[1:-len("/_search")]
            terms = [next(iter(c["term"].items())) for c in body["query"]["bool"]["filter"]]
            hits = [{"_index": index, "_id": did, "_source": dict(src)} for (ix, did), (src, _) in sorted(self.docs.items())
                    if ix == index and all(src.get(f) == v for f, v in terms)][: body.get("size", 10)]
            return 200, {"timed_out": False, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
                         "hits": {"total": {"value": len(hits), "relation": "eq"}, "hits": hits}}
        if p.endswith("/_search"):   # match_all sorted by one numeric field (the repair scans), search_after on it
            index, field = p[1:-len("/_search")], next(iter(body["sort"][0]))
            rows = sorted(((src.get(field, 0), did, src) for (ix, did), (src, _) in self.docs.items() if ix == index), key=lambda r: r[0])
            if body.get("search_after"):
                rows = [r for r in rows if r[0] > body["search_after"][0]]
            hits = [{"_index": index, "_id": did, "_source": dict(src), "sort": [v]} for v, did, src in rows[: body.get("size", 10)]]
            return 200, {"timed_out": False, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0}, "hits": {"hits": hits}}
        m = re.match(r"^/([^/]+)/_doc/(.+)$", p)
        assert m, path
        key = (m.group(1), m.group(2))
        if self.doc_answers.get((method, *key)):
            return self.doc_answers[(method, *key)].pop(0)
        params = dict(x.split("=", 1) for x in qs.split("&") if x)
        cur = self.docs.get(key)
        phys = behind(key[0])
        if method == "GET":
            if cur is None:
                return doc_answer("GET", phys, key[1], False)
            st, r = doc_answer("GET", phys, key[1], True, cur[0])
            return st, dict(r, _seq_no=cur[1])
        if method == "DELETE":
            return doc_answer("DELETE", phys, key[1], self.docs.pop(key, None) is not None)
        if method == "PUT":
            if params.get("op_type") == "create" and cur is not None:
                return conflict(phys)
            if "if_seq_no" in params and (cur is None or int(params["if_seq_no"]) != cur[1]):
                return conflict(phys)
            self.seq += 1
            self.docs[key] = (dict(body), self.seq)
            return doc_answer("PUT", phys, key[1], True, created=cur is None)
        raise AssertionError(path)
