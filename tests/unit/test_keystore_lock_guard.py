"""F14 and class guard (c): every Keystore method that uses the shared SQLite connection runs under the keystore lock.

The universe is enumerated, not listed by hand: every function defined in the Keystore class body (AST) that reads `self._db`
or a local bound from it, or calls a private helper that does. Each public member of that universe is called through a
connection proxy that records any use of the connection while the calling thread does not hold the lock. A member that
cannot be called from the table below fails the test, so a new method cannot join the universe unchecked.

The deterministic F14 case pauses one thread's `encrypt` inside its transaction (after BEGIN IMMEDIATE, at the INSERT) and
starts an orphan-version cleanup (`destroy_exact_version`) on a second thread: the cleanup must wait for the lock, never use
the connection while the first transaction is open.
"""
import ast
import inspect
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from living_context.keystore import Keystore, store

SRC = Path(inspect.getsourcefile(store))

# __init__ builds and configures the connection before the object is reachable from another thread, and close() ends it;
# both are listed so the guard still enumerates them. Every other connection user must hold the lock.
CONSTRUCTION_ONLY = {"__init__"}


class TrackingLock:
    """An RLock that can say whether the CURRENT thread holds it, and records threads waiting for it."""

    def __init__(self):
        self._l = threading.RLock()
        self._owner, self._count = None, 0
        self.waiting = set()

    def acquire(self, blocking=True, timeout=-1):
        me = threading.get_ident()
        if self._owner != me:
            self.waiting.add(me)
        try:
            ok = self._l.acquire(blocking, timeout)
        finally:
            self.waiting.discard(me)
        if ok:
            self._owner, self._count = me, self._count + 1
        return ok

    def release(self):
        self._count -= 1
        if self._count == 0:
            self._owner = None
        self._l.release()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()

    def held(self):
        return self._owner == threading.get_ident()


class ProxyConnection:
    """Wraps the keystore's sqlite3 connection: records every use made without the lock, and can pause a chosen statement."""

    def __init__(self, ks, db):
        self._ks, self._db = ks, db
        self.unlocked = []
        self.pause_on, self.paused, self.resume = None, threading.Event(), threading.Event()
        self.in_txn_thread = None
        self.overlaps = []

    def _check(self, what, sql=""):
        me = threading.get_ident()
        if not self._ks._lock.held():
            self.unlocked.append((what, sql.split("(")[0][:40]))
        if self.in_txn_thread not in (None, me):
            self.overlaps.append((what, sql[:40]))
        if sql.startswith("BEGIN"):
            self.in_txn_thread = me
        if sql.startswith(("COMMIT", "ROLLBACK")) and self.in_txn_thread == me:
            self.in_txn_thread = None

    def execute(self, sql, *a):
        self._check("execute", sql)
        if self.pause_on and sql.startswith(self.pause_on) and not self.paused.is_set():
            self.paused.set()
            self.resume.wait(10)
        return self._db.execute(sql, *a)

    def executemany(self, sql, *a):
        self._check("executemany", sql)
        return self._db.executemany(sql, *a)

    def executescript(self, sql):
        self._check("executescript", sql)
        return self._db.executescript(sql)

    def close(self):
        self._check("close")
        return self._db.close()

    def __getattr__(self, name):   # any other use of the connection is a use too
        self._check(name)
        return getattr(self._db, name)


def connection_users(source: str, cls: str = "Keystore") -> set:
    """The functions of class `cls` that use the connection, directly (self._db, or a name bound from it) or through a
    private helper of the class that does (transitively)."""
    tree = ast.parse(source)
    body = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls).body
    fns = {f.name: f for f in body if isinstance(f, ast.FunctionDef)}
    def is_conn(node, aliases):
        return (isinstance(node, ast.Attribute) and node.attr == "_db" and isinstance(node.value, ast.Name)
                and node.value.id == "self") or (isinstance(node, ast.Name) and node.id in aliases)

    direct, calls = set(), {}
    for name, f in fns.items():
        aliases = {t.id for n in ast.walk(f) if isinstance(n, ast.Assign) and is_conn(n.value, set())
                   for t in n.targets if isinstance(t, ast.Name)}
        if any(is_conn(n, set()) for n in ast.walk(f)):
            direct.add(name)
        calls[name] = set()
        for n in ast.walk(f):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "self" and n.func.attr in fns):
                calls[name].add(n.func.attr)
                if any(is_conn(a, aliases) for a in list(n.args) + [k.value for k in n.keywords]):
                    direct.add(n.func.attr)   # a helper handed the connection (`self._after_destroy(db)`) uses it
    users, grew = set(direct), True
    while grew:
        grew = False
        for name, callees in calls.items():
            if name not in users and callees & users:
                users.add(name)
                grew = True
    return users


def module_level_assignments(source: str, cls: str = "Keystore") -> list:
    """Attributes assigned onto the class after its body (`Keystore.x = ...`, `setattr(Keystore, ...)` with a constant name):
    a method added that way is outside the class-body universe and must be enumerated too."""
    out = []
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == cls:
                    out.append(t.attr)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr" and len(n.args) >= 2
                and isinstance(n.args[0], ast.Name) and n.args[0].id == cls and isinstance(n.args[1], ast.Constant)):
            out.append(n.args[1].value)
    return out


def _args(ks, name):
    """Arguments that let each public connection user run once. A member missing here fails the guard."""
    tok = ks.subject_token("p@example.org")
    ct, nonce, _ = Keystore.encrypt(ks, "leafarg", 1, b"x", [tok])   # set-up call, made while the proxy is not yet installed
    table = {
        "subject_token": ("p@example.org",),
        "digest": ("idem", b"body"),
        "has_key": ("leafarg", 1),
        "principal_token": ("p@example.org",),
        "register_identifier": ("q@example.org",),
        "encrypt": ("leafnew", 1, b"y", [tok]),
        "decrypt": ("leafarg", 1, ct, nonce),
        "destroy": {"leaf_id": "leafarg"},
        "destroy_versions": ([("leafarg", 1)],),
        "destroy_exact_version": ("leafarg", 1),
        "count_keys": (),
        "close": (),
    }
    return table.get(name)


def unlocked_uses(klass=Keystore, source=None) -> dict:
    """Call every public connection user of `klass` once through the proxy; return {method: [unlocked uses]} for each one
    that touched the connection without the lock, or could not be called."""
    source = source if source is not None else SRC.read_text()
    universe = connection_users(source)
    universe |= {n for n in vars(klass) if not n.startswith("_") and callable(vars(klass)[n])}   # members a subclass adds
    out = {}
    for name in sorted(universe - CONSTRUCTION_ONLY):
        if name.startswith("_"):
            continue   # private helpers are covered through the public methods that call them (checked below)
        with tempfile.TemporaryDirectory() as d:
            ks = klass(Path(d) / "tena.db", "tena")
            ks._lock = TrackingLock()
            args = _args(ks, name)
            if args is None:
                out[name] = ["no argument row: add it to _args so the guard can call it"]
                ks.close()
                continue
            proxy = ProxyConnection(ks, ks._db)
            ks._db = proxy
            try:
                fn = getattr(ks, name)
                fn(**args) if isinstance(args, dict) else fn(*args)
            except sqlite3.Error:
                pass
            if proxy.unlocked:
                out[name] = proxy.unlocked
            if name != "close":
                proxy._db.close()
    return out


class KeystoreLockGuard(unittest.TestCase):
    def test_guard_universe_is_enumerated_from_the_class_body(self):
        users = connection_users(SRC.read_text())
        # the universe found by the AST includes every method known to use the connection today, so the walk is not empty
        for name in ("encrypt", "decrypt", "destroy", "destroy_versions", "destroy_exact_version", "count_keys", "has_key",
                     "register_identifier", "subject_token", "digest", "principal_token", "_hmac_key", "_after_destroy", "close"):
            self.assertIn(name, users)
        self.assertEqual(module_level_assignments(SRC.read_text()), [],
                         "a method assigned onto Keystore after its body is outside the enumerated universe")

    def test_every_connection_user_holds_the_lock(self):
        self.assertEqual(unlocked_uses(), {})

    def test_private_helpers_are_called_only_from_locked_public_methods(self):
        tree = ast.parse(SRC.read_text())
        body = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Keystore").body
        users = connection_users(SRC.read_text())
        private = {u for u in users if u.startswith("_") and u not in CONSTRUCTION_ONLY}
        for f in body:
            if not isinstance(f, ast.FunctionDef):
                continue
            called = {n.func.attr for n in ast.walk(f) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"}
            for helper in called & private:
                self.assertTrue(not f.name.startswith("_") or f.name in private or f.name in CONSTRUCTION_ONLY,
                                f"{f.name} calls connection helper {helper} outside a locked public method")

    def test_planted_bypass_is_caught(self):
        # A public method added to a subclass without the lock, and a method unwrapped back to its raw function: both found.
        planted = type("Planted", (Keystore,), {"leak": lambda self: self._db.execute("SELECT COUNT(*) FROM leaf_keys").fetchone()})
        raw = Keystore.__dict__["destroy_exact_version"]
        unwrapped = type("Unwrapped", (Keystore,), {"destroy_exact_version": getattr(raw, "__wrapped__", raw)})
        src = SRC.read_text()
        self.assertIn("leak", unlocked_uses(planted, src))
        self.assertIn("destroy_exact_version", unlocked_uses(unwrapped, src))
        self.assertEqual(module_level_assignments(src + "\nKeystore.leak = lambda self: self._db\n"), ["leak"])
        self.assertEqual(module_level_assignments(src + "\nsetattr(Keystore, 'leak2', None)\n"), ["leak2"])

    def test_f14_orphan_cleanup_waits_for_an_open_encrypt_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            ks = Keystore(Path(d) / "tena.db", "tena")
            ks._lock = TrackingLock()
            tok = ks.subject_token("p@example.org")
            ks.encrypt("orphan", 2, b"orphan version", [tok])   # the key of a version whose append never happened
            proxy = ProxyConnection(ks, ks._db)
            ks._db = proxy
            proxy.pause_on = "INSERT INTO leaf_keys"
            errs, out = [], {}

            def enc():
                try:
                    out["enc"] = ks.encrypt("other", 1, b"another leaf", [tok])
                except Exception as e:
                    errs.append(("encrypt", repr(e)))

            def cleanup():
                try:
                    out["cleanup"] = ks.destroy_exact_version("orphan", 2)
                except Exception as e:
                    errs.append(("destroy_exact_version", repr(e)))

            a = threading.Thread(target=enc)
            a.start()
            self.assertTrue(proxy.paused.wait(10), "encrypt did not reach its INSERT")
            b = threading.Thread(target=cleanup)
            b.start()
            for _ in range(1000):   # until the cleanup either waits for the lock or reaches the connection
                if ks._lock.waiting or proxy.overlaps or errs or not b.is_alive():
                    break
                threading.Event().wait(0.005)
            overlapped = list(proxy.overlaps)
            proxy.resume.set()
            a.join(10)
            b.join(10)
            self.assertEqual(overlapped, [], "the cleanup used the connection inside another thread's transaction")
            self.assertEqual(errs, [])
            self.assertEqual(len(out.get("cleanup", [])), 1)
            self.assertFalse(ks.has_key("orphan", 2))
            self.assertTrue(ks.has_key("other", 1))
            proxy._db.close()


if __name__ == "__main__":
    unittest.main()
