"""The per-tenant send fence and the capped hand-off (DESIGN 4.4, 14.3; probe LC-PB).

`SendFence` is a writer-preferring readers-writer lock in gateway memory. Every
hand-off of response bytes to the kernel takes its shared side, re-checks the
principal's epoch (and, later, the tombstone sequence) and makes ONE
non-blocking write of at most 64 KiB minus the connection's queued byte count
(`SIOCOUTQ`), which is what bounds the bytes the kernel holds (the send buffer
size alone does not; docs/PROBES.md). The exclusive side is taken only for
in-memory steps (revoke's epoch flip, an erasure's publication step): no
journal, disk, Elasticsearch or blocking socket I/O happens under either side.
Linux: `SIOCOUTQ` is `TIOCOUTQ`.
"""
from __future__ import annotations

import contextlib
import fcntl
import socket
import struct
import termios
import threading
from typing import Callable, Optional

SEND_BOUND = 64 * 1024
REQUESTED_SNDBUF = 32 * 1024  # an effective 64 KiB on Linux (the kernel doubles it)


class SendFence:
    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    @contextlib.contextmanager
    def shared(self):
        with self._cond:
            while self._writer or self._writers_waiting:  # writer-preferring
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def exclusive(self):
        with self._cond:
            self._writers_waiting += 1
            while self._writer or self._readers:
                self._cond.wait()
            self._writers_waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


def configure(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, REQUESTED_SNDBUF)
    sock.setblocking(False)


def queued(sock: socket.socket) -> int:
    """Bytes the kernel still holds unsent for this connection (SIOCOUTQ)."""
    return struct.unpack("i", fcntl.ioctl(sock.fileno(), termios.TIOCOUTQ, struct.pack("i", 0)))[0]


class Aborted(Exception):
    """The final check failed under the fence: the response is discarded."""


def handoff(sock: socket.socket, data: bytes, fence: SendFence, check: Callable[[], bool], *,
            before_write: Optional[Callable[[], None]] = None, capped: bool = True,
            blocking_timeout: Optional[float] = None) -> int:
    """One hand-off. Returns the bytes written (0 when the kernel queue is full or the data empty).
    Raises Aborted if `check()` fails (epoch flipped, boundary published). `before_write` is the
    test hook that pauses a sender between its final check and its socket write (E8 (d), R1).
    `capped=False` is the broken variant that writes the whole buffer; `blocking_timeout` is the broken variant that writes it with a
    blocking call while holding the shared side (a slow reader then holds up every revoke and erasure boundary)."""
    with fence.shared():
        if not check():
            raise Aborted()
        if before_write:
            before_write()
        if not data:
            return 0
        if blocking_timeout is not None:
            sock.settimeout(blocking_timeout)
            try:
                sock.sendall(data)
                return len(data)
            except OSError:
                return 0
            finally:
                sock.setblocking(False)
        room = SEND_BOUND - queued(sock) if capped else len(data)
        if room <= 0:
            return 0
        try:
            return sock.send(data[:room])
        except BlockingIOError:
            return 0
