"""Local socket server (DESIGN 3, 7.1, 15; BUILD-PLAN Step 5).

Newline-delimited JSON: `{"token": ..., "request": {...}}` per line. Composition only:
bytes are bounded before parsing (admission.read_line), the tenant comes from the session
record, per-tenant slots and connection caps apply (admission.Admission), and every
tenant-bound response goes out through the fenced, queue-capped hand-off (fence.handoff)
whose final check, before EVERY chunk, is the session's epoch and the handler's `send_ok` over
the reply's token and carried pools (POOLS 2.4, 2.5, AC-2: checks 1 to 4 for every pool whose
content the response carries). A failed check aborts the whole response: nothing of it sent
yet, the typed `unavailable` line goes instead; part of it sent, the connection is closed (a
line is never completed after the boundary, and never sent with items dropped). Both, and a
response that misses its send deadline, are counted (INV-11). Unauthenticated connections are
bounded globally and by an idle deadline until their first valid line binds them to a tenant.
"""
from __future__ import annotations

import json
import select
import socket
import threading
import time
from typing import Any, Callable

from ..config import Constants
from . import admission, fence
from .admin import REVOKE_LANE_OPS, lane_allowed
from .handler import Reply
from .sessions import EpochTable, Sessions

REFUSED_LINE = b'{"error": "refused", "ok": false}\n'
UNAVAILABLE_LINE = b'{"error": "unavailable", "ok": false}\n'


class Server:
    def __init__(self, handler: Any, sessions: Sessions, epochs: EpochTable,
                 fence_for: Callable[[str], fence.SendFence], adm: admission.Admission | None = None,
                 consts: Constants | None = None, host: str = "127.0.0.1", port: int = 0,
                 idle_s: float = 2.0, max_unauth: int = 64, broken_revoke_in_general_lane: bool = False,
                 broken_lane_from_body: bool = False):
        self.consts = consts or Constants()
        self.handler, self.sessions, self.epochs, self.fence_for = handler, sessions, epochs, fence_for
        self.adm = adm or admission.Admission(self.consts)
        self.idle_s, self.max_unauth = idle_s, max_unauth
        self._general_lane = broken_revoke_in_general_lane   # BROKEN variant: a revoke waits in the tenant's general queue
        self._lane_from_body = broken_lane_from_body         # BROKEN variant: the lane is chosen from the body, before any role check
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(128)
        self.address = self._srv.getsockname()
        self._unauth = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.bytes_read = 0  # test counter: request bytes taken from sockets
        self.aborted = 0     # responses aborted by a failed hand-off check (INV-11)
        self.expired = 0     # responses that missed the send deadline with bytes unsent (INV-11)
        self.before_write: Callable[[], None] | None = None   # test seam: runs under the shared fence between the check and the write
        self.send_deadline_s = 10.0

    def start(self) -> "Server":
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._srv.close()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with self._lock:
                if self._unauth >= self.max_unauth:
                    conn.close()
                    continue
                self._unauth += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        tenant, unauth = None, True
        f = conn.makefile("rb")
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, fence.REQUESTED_SNDBUF)
            conn.settimeout(self.idle_s)
            while True:
                try:
                    raw = admission.read_line(f, self.consts.max_line_bytes)
                except admission.Refused:
                    self._plain(conn, REFUSED_LINE)
                    return  # an over-long line ends the connection
                except (socket.timeout, OSError):
                    return
                if not raw:
                    return
                self.bytes_read += len(raw)
                try:
                    with self.adm.parse_semaphore:  # one semaphore around parsing only (LAB-R5)
                        msg = admission.parse(raw, max_structures=1000)
                    mint = msg.get("mint") if isinstance(msg, dict) and set(msg) == {"mint"} else None
                    token, req = (None, None) if mint is not None else (msg["token"], msg["request"])
                except (admission.Refused, KeyError, TypeError):
                    self._plain(conn, REFUSED_LINE)
                    continue
                if mint is not None:
                    # the socket's mint (POOLS 2.3, 2.6): verified by the tenant's identity adapter; the connection is still
                    # unauthenticated (bounded globally and by the idle deadline) until the minted token binds it to its tenant
                    resp, tok = self.handler.mint_line(mint)
                    rec = self.sessions.resolve(tok) if tok else None
                    if rec is None:
                        self._plain(conn, UNAVAILABLE_LINE)
                        continue
                else:
                    rec = self.sessions.resolve(token)
                    if rec is None:
                        self._plain(conn, UNAVAILABLE_LINE)
                        continue
                if tenant is None:
                    try:
                        self.adm.open_connection(rec.tenant)
                    except admission.Refused:
                        self._plain(conn, REFUSED_LINE)
                        return
                    tenant = rec.tenant
                    with self._lock:
                        self._unauth -= 1
                    unauth = False
                    conn.settimeout(30)
                elif rec.tenant != tenant:
                    self._plain(conn, REFUSED_LINE)
                    return
                if mint is not None:
                    if not self._fenced(conn, (json.dumps(resp, sort_keys=True) + "\n").encode(), rec, Reply(resp, tok, {})):
                        return
                    continue
                try:
                    op = req.get("op") if isinstance(req, dict) else None
                    # the lane is granted only to a resolved session whose role admits the operation (DESIGN 4.4, 15)
                    lane = (not self._general_lane and op in REVOKE_LANE_OPS and
                            (self._lane_from_body or lane_allowed(op, self.sessions.principal(rec))))
                    release = self.adm.acquire(tenant, timeout=2.0, revoke=lane)
                except admission.Refused:
                    if not self._fenced(conn, REFUSED_LINE, rec, Reply(None, token, {})):
                        return
                    continue
                try:
                    reply = self._reply(token, req)  # Elasticsearch calls never hold the global semaphore
                finally:
                    release()
                if not self._fenced(conn, (json.dumps(reply.body, sort_keys=True) + "\n").encode(), rec, reply):
                    return  # aborted after part of it was sent, or past its deadline: the connection ends
        finally:
            try:
                f.close()
                conn.close()
            except OSError:
                pass
            if tenant is not None:
                self.adm.close_connection(tenant)
            if unauth:
                with self._lock:
                    self._unauth -= 1

    @staticmethod
    def _plain(conn, data: bytes):
        """A typed refusal that carries no tenant data needs no fence."""
        try:
            conn.sendall(data)
        except OSError:
            pass

    def _reply(self, token, req) -> Reply:
        serve = getattr(self.handler, "serve", None)
        if serve is not None:
            return serve(token, req)
        # a handler with `handle` only (the tests' stand-ins): its answer carries no pool; the hand-off still re-checks the token
        return Reply(self.handler.handle(token, req), token, {})

    def _check(self, rec, reply: Reply) -> Callable[[], bool]:
        """The final check of every hand-off, run under the shared side of the tenant's send fence: the session's epoch (as before),
        then the handler's `send_ok(reply)` (checks 1 to 4 over the reply's token and pools). A handler without `send_ok` gets the
        token check (`Sessions.resolve`) in its place, never the epoch alone."""
        send_ok = getattr(self.handler, "send_ok", None)
        if send_ok is None:
            send_ok = lambda r: r.token is None or self.sessions.resolve(r.token) is not None

        def check() -> bool:
            try:
                return self.epochs.current(rec.tenant, rec.principal_token, rec.epoch) is True and send_ok(reply) is True
            except Exception:
                return False
        return check

    def _fenced(self, conn, data: bytes, rec, reply: Reply) -> bool:
        """Hand `data` to the kernel in fenced, capped chunks, re-checking before each one. Returns False when the connection must end:
        the check failed after part of the response was sent, or the deadline passed with bytes unsent (the next response would
        otherwise follow a partial line). A check that fails before any byte went out sends the typed `unavailable` line instead."""
        f = self.fence_for(rec.tenant)
        check = self._check(rec, reply)
        deadline = time.time() + self.send_deadline_s
        sent = 0
        conn.setblocking(False)
        try:
            while data:
                if time.time() >= deadline:
                    with self._lock:
                        self.expired += 1
                    return False
                try:
                    n = fence.handoff(conn, data, f, check, before_write=self.before_write)
                except fence.Aborted:
                    with self._lock:
                        self.aborted += 1   # the unsent bytes are discarded with `data`
                    if sent:
                        return False
                    conn.settimeout(30)
                    self._plain(conn, UNAVAILABLE_LINE)   # carries no tenant data: no fence needed
                    return True
                except OSError:
                    return False
                sent += n
                data = data[n:]
                if data:
                    select.select([], [conn], [], 0.05)
            return True
        finally:
            try:
                conn.settimeout(30)
            except OSError:
                pass
