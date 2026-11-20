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
        if m.group(2) == "_count":
            return 200, {"count": len(hits)}
        out = {"hits": {"hits": [{"_id": i, "_source": s} for i, s in hits[: body.get("size", 10)]]}}
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
        did = path.split("/_doc/")[1].split("?")[0] if "/_doc/" in path else None
        if method == "PUT":
            self.docs[did] = body
            return 200, {}
        if method == "DELETE":
            return (200, {}) if self.docs.pop(did, None) is not None else (404, {})
        kinds = body["query"]["terms"]["kind"]
        return 200, {"hits": {"hits": [{"_id": i, "_source": d} for i, d in self.docs.items() if d.get("kind") in kinds]}}
