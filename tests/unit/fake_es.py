"""A tiny in-memory stand-in for the parts of Elasticsearch the pool reader uses (filter-context search, get, mget, count, terms
aggregation). It evaluates only term, terms, range, exists and bool; anything else is an error, so a test that sends a scored
query fails loudly instead of being silently ignored. Not a model of Elasticsearch: the real behaviour is in the `_es` tests."""
import re


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
        if m.group(2).startswith("_doc/"):
            did = m.group(3)
            if did not in docs:
                return 404, {"found": False}
            return 200, {"found": True, "_id": did, "_source": docs[did], "_seq_no": 1, "_primary_term": 1}
        if m.group(2) == "_mget":
            return 200, {"docs": [({"_id": i, "found": True, "_source": docs[i], "_seq_no": 1, "_primary_term": 1} if i in docs
                                   else {"_id": i, "found": False}) for i in body["ids"]]}
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
    """The tenant's authz index as a dict: PUT, DELETE and a `terms kind` search. `down` makes every call fail (a blocked index)."""

    def __init__(self):
        self.docs, self.down = {}, False

    def request(self, method, path, body=None, **kw):
        if self.down:
            return 503, {}
        p = path.split("?")[0]
        if p.endswith("/_pit"):   # a point in time: the snapshot is the record ids in insertion order (one shard)
            self.pits = getattr(self, "pits", {})
            pid = f"pit{len(self.pits)}"
            self.pits[pid] = list(self.docs)
            return 200, {"id": pid, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0}}
        if p == "/_pit" and method == "DELETE":
            return 200, {"succeeded": getattr(self, "pits", {}).pop(body["id"], None) is not None, "num_freed": 1}
        did = path.split("/_doc/")[1].split("?")[0] if "/_doc/" in path else None
        if method == "PUT":
            self.docs[did] = body
            return 200, {}
        if method == "DELETE":
            return (200, {}) if self.docs.pop(did, None) is not None else (404, {})
        q = body["query"]
        kinds = q["terms"]["kind"] if "terms" in q else [q["term"]["kind"]]
        ids = self.pits[body["pit"]["id"]] if "pit" in body else list(self.docs)
        keys = [i for i in ids if i in self.docs and self.docs[i].get("kind") in kinds]   # insertion order stands for `_shard_doc`
        start = (int(body["search_after"][0]) + 1) if body.get("search_after") else 0
        size = int(body.get("size", 10))
        self.searches = getattr(self, "searches", 0) + 1
        page = list(enumerate(keys))[start:start + size]
        return 200, {"timed_out": False, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},   # a complete answer, as ES gives
                     "hits": {"hits": [{"_id": i, "_source": self.docs[i], "sort": [n]} for n, i in page]}}


class ShardedFake(FakeEs):
    """Several one-shard indices behind one pattern, with the two cursors Elasticsearch offers a multi-index scan:

    * `sort: _doc` gives each hit its shard-LOCAL document number, so the first document of every index has sort value [0]; a
      `search_after: [k]` continues after k in every shard, so a page boundary that falls between equal values skips the rest (F12);
    * a point in time (`POST /<pattern>/_pit`, then `POST /_search` with `pit` and `sort: _shard_doc`) gives every hit a value unique
      across the snapshot (shard number and document number), so `search_after` neither skips nor repeats.

    `partial` makes the next N search answers HTTP 200 with a failed shard; `alias_status` answers `GET /<name>/_alias`. Not a
    model of Elasticsearch beyond these cursors: the real behaviour is in the `_es` tests."""

    def __init__(self):
        super().__init__()
        self.aliases, self.pits, self.partial, self.alias_status, self.closed_pits = {}, {}, 0, None, []

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
            hit = {n: {"aliases": {target: {}}} for n, al in self.aliases.items() if target in al}
            if target in self.docs:
                hit[target] = {"aliases": {}}
            return (200, hit) if hit else (404, {"error": {"type": "index_not_found_exception"}, "status": 404})
        if op.startswith("_doc/"):
            phys = self._indices(target)
            did = m.group(3)
            for n in phys:
                if did in self.docs[n] and method == "DELETE":
                    del self.docs[n][did]
                    return 200, {"result": "deleted"}
            return 404, {"result": "not_found"}
        phys = self._indices(target)
        if op == "_refresh":
            return 200, {"_shards": self._shards(len(phys))}
        if op == "_pit":
            if not phys and "ignore_unavailable=true" not in qs and "*" not in target:
                return 404, {"error": {"type": "index_not_found_exception"}, "status": 404}
            pid = f"pit{len(self.pits) + len(self.closed_pits)}"
            self.pits[pid] = [(n, list(self.docs[n].items())) for n in phys]   # the snapshot
            return 200, {"id": pid, "_shards": self._shards(len(phys))}
        return self._page([(n, list(self.docs[n].items())) for n in phys], body, unique=False)

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
    `fail` maps a (method, index-substring) to a status answered once per entry in its list (an injected HTTP error)."""

    def __init__(self):
        self.docs, self.seq, self.fail, self.sent = {}, 0, {}, []

    def request(self, method, path, body=None, **kw):
        p, _, qs = path.partition("?")
        self.sent.append((method, p))
        for (m, sub), statuses in list(self.fail.items()):
            if m == method and sub in p and statuses:
                return statuses.pop(0), {"error": {"type": "injected"}}
        if p.endswith("/_refresh"):
            return 200, {"_shards": {"total": 1, "successful": 1, "failed": 0}}
        m = re.match(r"^/([^/]+)/_doc/(.+)$", p)
        assert m, path
        key = (m.group(1), m.group(2))
        params = dict(x.split("=", 1) for x in qs.split("&") if x)
        cur = self.docs.get(key)
        if method == "GET":
            if cur is None:
                return 404, {"found": False}
            return 200, {"found": True, "_id": key[1], "_source": dict(cur[0]), "_seq_no": cur[1], "_primary_term": 1}
        if method == "DELETE":
            return (200, {"result": "deleted"}) if self.docs.pop(key, None) is not None else (404, {"result": "not_found"})
        if method == "PUT":
            if params.get("op_type") == "create" and cur is not None:
                return 409, {"error": {"type": "version_conflict_engine_exception"}}
            if "if_seq_no" in params and (cur is None or int(params["if_seq_no"]) != cur[1]):
                return 409, {"error": {"type": "version_conflict_engine_exception"}}
            self.seq += 1
            self.docs[key] = (dict(body), self.seq)
            return (201 if cur is None else 200), {"result": "created" if cur is None else "updated"}
        raise AssertionError(path)
