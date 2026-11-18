"""Single-host process claims that are released by the OS after a crash."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


class ClaimBusy(RuntimeError):
    """Another live process or thread owns this local operation."""


@contextmanager
def exclusive_file_claim(path: Path):
    """No timer can steal a claim from a slow side effect. Not a multi-host lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    locked = False
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ClaimBusy("operation is already owned by another local process") from None
        locked = True
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()
