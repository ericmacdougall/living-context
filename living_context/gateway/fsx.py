"""Atomic exchange of two file names (Linux `renameat2` with `RENAME_EXCHANGE`).

The standard library has no wrapper; probe LC-PA showed that `libc.renameat2`
(and the raw syscall) work through `ctypes` on ext4. Used by the epoch journal
compaction (DESIGN 4.4) and by the provisioning check that refuses a tenant
whose journal filesystem cannot do it. Linux only: on other systems
`supported()` is False and `exchange` raises.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform

RENAME_EXCHANGE = 2
AT_FDCWD = -100
_SYSCALL_NR = {"x86_64": 316, "aarch64": 276}


class ExchangeUnsupported(OSError):
    pass


def _libc():
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def exchange(a: str, b: str) -> None:
    """Swap the contents behind names `a` and `b` in one atomic step."""
    if platform.system() != "Linux":
        raise ExchangeUnsupported(errno.ENOSYS, "renameat2 is Linux only")
    libc = _libc()
    if hasattr(libc, "renameat2"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        rc = fn(AT_FDCWD, os.fsencode(a), AT_FDCWD, os.fsencode(b), RENAME_EXCHANGE)
    else:
        nr = _SYSCALL_NR.get(platform.machine())
        if nr is None:
            raise ExchangeUnsupported(errno.ENOSYS, "no renameat2")
        sc = libc.syscall
        sc.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        sc.restype = ctypes.c_long
        rc = sc(nr, AT_FDCWD, os.fsencode(a), AT_FDCWD, os.fsencode(b), RENAME_EXCHANGE)
    if rc != 0:
        e = ctypes.get_errno()
        raise ExchangeUnsupported(e, os.strerror(e))


def supported(directory: str) -> bool:
    """True if two files in `directory` can be exchanged atomically (provisioning check)."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory(dir=directory) as d:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            from pathlib import Path
            for p, c in ((a, b"A"), (b, b"B")):
                Path(p).write_bytes(c)
            exchange(a, b)
            from pathlib import Path
            return Path(a).read_bytes() == b"B" and Path(b).read_bytes() == b"A"
    except OSError:
        return False
