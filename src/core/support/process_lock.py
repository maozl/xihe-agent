"""CrossProcessLock — mutual exclusion ACROSS PROCESSES.

The path is just the lock target (its content is irrelevant — an owner may
leave a pid for diagnostics); underneath are OS byte-range locks
(``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX). A lock is
released when its handle closes, so a crashed owner never deadlocks later
ones. Use ``timeout`` to bound waiting; acquisition failure raises
TimeoutError instead of blocking forever.

Zero-dependency by charter: importable at module level from anywhere
(same rule as the rest of core/support).
"""

import os
import time
from pathlib import Path


class CrossProcessLock:
    """Cross-process exclusive lock with two usage shapes:

    - Scoped critical section: ``with CrossProcessLock(path, timeout=10):``
    - Held-for-lifetime ownership (scheduler-style): ``fl = CrossProcessLock(path);
      fl.acquire()`` and simply never release — the OS releases it when the
      process dies.

    Exclusive across processes and across handles within one process.
    Not reentrant: acquiring the same path twice in one thread waits out
    the timeout — Windows byte-range locks do not stack.
    """

    def __init__(self, path, timeout: float = 10.0, poll: float = 0.05):
        self._path = Path(path)
        self._timeout = timeout
        self._poll = poll
        self._fh = None

    def acquire(self) -> "CrossProcessLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+b")
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._try_lock(fh)
                self._fh = fh
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise TimeoutError(
                        f"file lock contention on {self._path} "
                        f"(> {self._timeout}s)")
                time.sleep(self._poll)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        # Best-effort unlock + guaranteed close: the OS releases a byte-range
        # lock when the handle closes (CloseHandle / flock semantics), so an
        # unlock failure is harmless as long as close() runs.
        try:
            self._try_unlock(fh)
        except Exception:
            pass
        finally:
            fh.close()

    def __enter__(self) -> "CrossProcessLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    @staticmethod
    def _try_lock(fh):
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _try_unlock(fh):
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_UN)
