"""
Small in-memory stand-in for the supabase-py client, covering the query
builder calls the ReceiptKept routes use. Lets the tests exercise real
endpoint code without touching the shared production database.
"""

import copy
import uuid
from datetime import datetime


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Not:
    def __init__(self, q):
        self.q = q

    def is_(self, col, val):
        self.q._filters.append(lambda r: not _is(r.get(col), val))
        return self.q


def _is(v, val):
    if val in ("null", None):
        return v is None
    return v == val


def _cmp_val(v):
    return "" if v is None else str(v)


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._filters = []
        self._op = "select"
        self._payload = None
        self._order = []
        self._limit = None
        self._count = None
        self._upsert_conflict = None

    # builders
    def select(self, *_cols, count=None):
        self._op = "select"
        self._count = count
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False):
        self._op, self._payload = "upsert", payload
        self._upsert_conflict = (on_conflict, ignore_duplicates)
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(lambda r: _cmp_val(r.get(col)) == _cmp_val(val))
        return self

    def neq(self, col, val):
        self._filters.append(lambda r: _cmp_val(r.get(col)) != _cmp_val(val))
        return self

    def in_(self, col, vals):
        vals = {_cmp_val(v) for v in vals}
        self._filters.append(lambda r: _cmp_val(r.get(col)) in vals)
        return self

    def gte(self, col, val):
        self._filters.append(lambda r: r.get(col) is not None and str(r.get(col)) >= str(val))
        return self

    def lte(self, col, val):
        self._filters.append(lambda r: r.get(col) is not None and str(r.get(col))[: len(str(val))] <= str(val))
        return self

    def lt(self, col, val):
        self._filters.append(lambda r: r.get(col) is not None and str(r.get(col)) < str(val))
        return self

    def gt(self, col, val):
        self._filters.append(lambda r: r.get(col) is not None and str(r.get(col)) > str(val))
        return self

    def ilike(self, col, pattern):
        p = pattern.lower().strip("%")
        self._filters.append(lambda r: p in str(r.get(col) or "").lower())
        return self

    def is_(self, col, val):
        self._filters.append(lambda r: _is(r.get(col), val))
        return self

    def filter(self, col, op, val):
        if op == "is":
            return self.is_(col, val)
        if op == "eq":
            return self.eq(col, val)
        raise NotImplementedError(op)

    @property
    def not_(self):
        return _Not(self)

    def order(self, col, desc=False):
        self._order.append((col, desc))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, a, b):
        self._limit = b - a + 1
        return self

    def single(self):
        return self

    def maybe_single(self):
        return self

    # execution
    def _rows(self):
        return self.db.tables.setdefault(self.table, [])

    def _match(self):
        return [r for r in self._rows() if all(f(r) for f in self._filters)]

    def _fill(self, row):
        row = dict(row)
        row.setdefault("id", str(uuid.uuid4()))
        now = datetime.utcnow().isoformat()
        row.setdefault("created_at", now)
        if self.table == "receipts":
            row.setdefault("uploaded_at", now)
        return row

    def execute(self):
        rows = self._rows()
        if self._op == "insert":
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            new = [self._fill(p) for p in payload]
            for n in new:
                self.db.check_unique(self.table, n)
            rows.extend(new)
            return _Result(copy.deepcopy(new))
        if self._op == "upsert":
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            conflict, ignore = self._upsert_conflict
            cols = [c.strip() for c in (conflict or "id").split(",")]
            out = []
            for p in payload:
                existing = next((r for r in rows if all(_cmp_val(r.get(c)) == _cmp_val(p.get(c)) for c in cols)), None)
                if existing:
                    if not ignore:
                        existing.update(p)
                        out.append(copy.deepcopy(existing))
                else:
                    n = self._fill(p)
                    rows.append(n)
                    out.append(copy.deepcopy(n))
            return _Result(out)
        matched = self._match()
        if self._op == "update":
            for r in matched:
                r.update(copy.deepcopy(self._payload))
            return _Result(copy.deepcopy(matched))
        if self._op == "delete":
            for r in matched:
                rows.remove(r)
            return _Result(copy.deepcopy(matched))
        out = matched
        for col, desc in reversed(self._order):
            out = sorted(out, key=lambda r: _cmp_val(r.get(col)), reverse=desc)
        count = len(out)
        if self._limit is not None:
            out = out[: self._limit]
        return _Result(copy.deepcopy(out), count=count)


class _Bucket:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def upload(self, path, content, opts=None):
        self.db.storage_files[(self.name, path)] = content
        return {"path": path}

    def download(self, path):
        return self.db.storage_files[(self.name, path)]

    def remove(self, paths):
        for p in paths:
            self.db.storage_files.pop((self.name, p), None)

    def create_signed_url(self, path, expires):
        return {"signedURL": f"https://signed.example/{path}?expires={expires}"}

    def create_signed_urls(self, paths, expires):
        # like Supabase: unknown files come back with an error
        out = []
        for p in paths:
            if (self.name, p) in self.db.storage_files:
                out.append({"path": p, "error": None, "signedURL": f"https://signed.example/{p}?expires={expires}"})
            else:
                out.append({"path": p, "error": "Object not found", "signedURL": None})
        return out

    def get_public_url(self, path):
        return f"https://public.example/{path}"


class _Storage:
    def __init__(self, db):
        self.db = db

    def from_(self, name):
        return _Bucket(self.db, name)


class FakeSupabase:
    def __init__(self):
        self.tables = {}
        self.storage_files = {}
        self.storage = _Storage(self)
        # table -> list of column tuples that must be unique together
        self.unique = {}
        # rpc name -> fn(params) returning data; a missing one raises (like a
        # function that doesn't exist in the database)
        self.rpc_handlers = {}

    def rpc(self, name, params=None):
        db = self

        class _Call:
            def execute(self_inner):
                if name not in db.rpc_handlers:
                    raise Exception(f"function {name} does not exist")
                return _Result(db.rpc_handlers[name](params or {}))
        return _Call()

    def table(self, name):
        return _Query(self, name)

    def check_unique(self, table, row):
        for cols in self.unique.get(table, []):
            key = tuple(_cmp_val(row.get(c)) for c in cols)
            for r in self.tables.get(table, []):
                if r is not row and tuple(_cmp_val(r.get(c)) for c in cols) == key:
                    raise Exception(f'duplicate key value violates unique constraint on {table} {cols}')
