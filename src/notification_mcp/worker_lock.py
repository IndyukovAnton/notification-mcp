import os
from pathlib import Path


class WorkerLock:
    """OS-owned lock: released even if the process crashes. No stale PID files."""

    def __init__(self, database: Path):
        self.path = database.with_name(database.name + ".worker.lock")
        self._file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None
            raise RuntimeError("Another delivery worker is already using this database") from None
        return self

    def __exit__(self, *_):
        if self._file:
            self._file.close()
            self._file = None
