"""Probe LC-PB: effective SO_SNDBUF and the bytes the kernel holds for a connection.

DESIGN 4.4 bounds the response bytes the kernel still holds for a connection
at the flip or boundary by the connection's effective send buffer, "at most
64 KiB". Linux doubles the value passed to setsockopt(SO_SNDBUF) and reports
the doubled value, so the design's "effective size the kernel reports" must be
read from getsockopt, not assumed from what was set.

For AF_INET loopback and AF_UNIX stream sockets this probe sets several
requested sizes on the accepted socket, reads back the effective size, then
writes non-blocking to a peer that never reads until EAGAIN, and records the
bytes the sender got accepted and the unsent bytes still queued at the sender
(SIOCOUTQ; for AF_UNIX the figure is not available and is reported as null).
The bytes sitting in the peer's receive queue are outside the design's bound.
Prints one JSON verdict line.
"""
import fcntl
import json
import platform
import socket
import struct
import sys
import termios

LIMIT = 64 * 1024


def fill(sock):
    sock.setblocking(False)
    total = 0
    chunk = b"x" * 4096
    while True:
        try:
            total += sock.send(chunk)
        except BlockingIOError:
            return total


def outq(sock):
    try:
        buf = fcntl.ioctl(sock.fileno(), termios.TIOCOUTQ, struct.pack("i", 0))
        return struct.unpack("i", buf)[0]
    except OSError:
        return None


def fill_capped(sock, chunk=4096):
    """Write only while the unsent queue plus the chunk stays within LIMIT."""
    sock.setblocking(False)
    total = 0
    peak = 0
    data = b"x" * chunk
    while True:
        q = outq(sock)
        if q is None or q + chunk > LIMIT:
            return total, peak
        try:
            total += sock.send(data)
        except BlockingIOError:
            return total, peak
        peak = max(peak, outq(sock) or 0)


def run(family, requested):
    srv = socket.socket(family, socket.SOCK_STREAM)
    if family == socket.AF_INET:
        srv.bind(("127.0.0.1", 0))
        addr = srv.getsockname()
    else:
        import os
        import tempfile
        d = tempfile.mkdtemp(prefix="lcpb-")
        addr = os.path.join(d, "s")
        srv.bind(addr)
    srv.listen(1)
    cli = socket.socket(family, socket.SOCK_STREAM)
    cli.connect(addr)
    conn, _ = srv.accept()
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, requested)
    effective = conn.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
    accepted = fill(conn)
    q = outq(conn)
    for s_ in (conn, cli, srv):
        s_.close()
    srv = socket.socket(family, socket.SOCK_STREAM)
    if family == socket.AF_INET:
        srv.bind(("127.0.0.1", 0))
        addr = srv.getsockname()
    else:
        addr = addr + "2"
        srv.bind(addr)
    srv.listen(1)
    cli = socket.socket(family, socket.SOCK_STREAM)
    cli.connect(addr)
    conn, _ = srv.accept()
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, requested)
    capped_total, capped_peak = fill_capped(conn)
    res = {"family": "inet" if family == socket.AF_INET else "unix", "requested": requested,
           "effective": effective, "effective_within_64KiB": effective <= LIMIT,
           "bytes_accepted_until_eagain": accepted, "unsent_in_sender_queue": q,
           "queue_exceeds_64KiB_without_cap": (q or 0) > LIMIT,
           "gateway_capped_peak_queue": capped_peak,
           "gateway_capped_peak_within_64KiB": capped_peak <= LIMIT}
    for s in (conn, cli, srv):
        s.close()
    return res


def main():
    rows = []
    for fam in (socket.AF_INET, socket.AF_UNIX):
        for req in (4096, 32 * 1024, 64 * 1024, 128 * 1024):
            rows.append(run(fam, req))
    best = {}
    for r in rows:
        if r["effective_within_64KiB"]:
            best.setdefault(r["family"], r["requested"])
            best[r["family"]] = max(best[r["family"]], r["requested"])
    naive = [r for r in rows if r["requested"] == LIMIT]
    out = {"probe": "LC-PB", "kernel": platform.release(), "rows": rows,
           "largest_request_with_effective_le_64KiB": best,
           "naive_request_64KiB_violates_bound": any(not r["effective_within_64KiB"] for r in naive),
           "verdict": "PASS_WITH_HALVED_REQUEST" if best else "FAIL"}
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main()
