"""Probe LC-PA: atomic exchange of two file names (Linux renameat2 RENAME_EXCHANGE).

DESIGN 4.4 needs the journal compaction to swap the journal and its spare by
one atomic name exchange. This probe finds a callable path from Python 3.12
(the stdlib has no wrapper), and checks on the filesystem of the directory
given as argv[1]:
  1. the two names swap contents;
  2. a concurrent reader that opens by name never sees a missing name or a
     content other than one of the two (atomicity as seen by readers);
  3. the file sizes do not change (no space freed or allocated);
  4. control: an unsupported flag value is refused, so a "pass" cannot come
     from a stub that ignores flags.
Prints one JSON verdict line.
"""
import ctypes
import ctypes.util
import errno
import json
import os
import platform
import sys
import tempfile
import threading

RENAME_EXCHANGE = 2
AT_FDCWD = -100
SYSCALL_NR = {"x86_64": 316, "aarch64": 276}


def find_exchange():
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    paths = []
    if hasattr(libc, "renameat2"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        paths.append(("libc.renameat2", lambda a, b, fl: fn(AT_FDCWD, a, AT_FDCWD, b, fl)))
    nr = SYSCALL_NR.get(platform.machine())
    if nr is not None:
        sc = libc.syscall
        sc.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        sc.restype = ctypes.c_long
        paths.append(("syscall", lambda a, b, fl: sc(nr, AT_FDCWD, a, AT_FDCWD, b, fl)))
    return paths


def main(base):
    out = {"probe": "LC-PA", "python": platform.python_version(), "machine": platform.machine(),
           "kernel": platform.release(), "paths": {}}
    for name, call in find_exchange():
        d = tempfile.mkdtemp(prefix="lcpa-", dir=base)
        a, b = os.path.join(d, "journal"), os.path.join(d, "spare")
        sa, sb = b"A" * 4096, b"B" * 4096
        for p, c in ((a, sa), (b, sb)):
            with open(p, "wb") as f:
                f.write(c)
                f.flush()
                os.fsync(f.fileno())
        sizes_before = (os.stat(a).st_size, os.stat(b).st_size)
        res = {}
        # 4. control: bogus flag must fail
        rc = call(a.encode(), b.encode(), 0x80000000)
        res["bogus_flag_refused"] = rc != 0 and ctypes.get_errno() in (errno.EINVAL, errno.ENOSYS)
        # 1. plain exchange
        rc = call(a.encode(), b.encode(), RENAME_EXCHANGE)
        err = ctypes.get_errno() if rc != 0 else 0
        res["exchange_rc"] = rc
        res["exchange_errno"] = errno.errorcode.get(err, err) if err else 0
        if rc == 0:
            res["swapped"] = open(a, "rb").read() == sb and open(b, "rb").read() == sa
            res["sizes_unchanged"] = sizes_before == (os.stat(a).st_size, os.stat(b).st_size)
            # 2. concurrent readers
            stop = threading.Event()
            bad = []
            reads = [0]

            def reader():
                while not stop.is_set():
                    try:
                        data = open(a, "rb").read()
                    except OSError as e:
                        bad.append(("missing", str(e)))
                        return
                    reads[0] += 1
                    if data not in (sa, sb):
                        bad.append(("content", len(data)))
                        return

            ts = [threading.Thread(target=reader) for _ in range(4)]
            for t in ts:
                t.start()
            n = 20000
            for _ in range(n):
                if call(a.encode(), b.encode(), RENAME_EXCHANGE) != 0:
                    bad.append(("exchange_failed", 0))
                    break
            stop.set()
            for t in ts:
                t.join()
            res["concurrent_exchanges"] = n
            res["concurrent_reads"] = reads[0]
            res["reader_anomalies"] = bad
            res["ok"] = bool(res["swapped"] and res["sizes_unchanged"] and not bad and res["bogus_flag_refused"])
        else:
            res["ok"] = False
        out["paths"][name] = res
    out["verdict"] = "PASS" if any(v.get("ok") for v in out["paths"].values()) else "FAIL"
    print(json.dumps(out, sort_keys=True))
    return 0 if out["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
