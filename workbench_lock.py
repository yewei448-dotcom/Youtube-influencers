"""Stable-file exclusive locks for Windows and POSIX (Python 3.9+).

Lock files are never removed or truncated. Windows always locks byte 0 for
length 1; the byte may initially be beyond EOF. Importing this module does not
import an unavailable platform module. Locks are deliberately not reentrant.
"""

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path


def _backend():
    if os.name == "nt":
        import msvcrt
        return "windows", msvcrt
    import fcntl
    return "posix", fcntl


def _busy(error):
    return (error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}
            or getattr(error, "winerror", None) in {33, 36})


def _unlock(descriptor, kind, api):
    if kind == "windows":
        os.lseek(descriptor, 0, os.SEEK_SET)
        api.locking(descriptor, api.LK_UNLCK, 1)
    else:
        api.flock(descriptor, api.LOCK_UN)


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self._fd, self._kind, self._api = None, None, None

    def acquire(self, blocking=True):
        if self._fd is not None:
            raise RuntimeError("This lock instance is already acquired")
        kind, api = _backend()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags, 0o600)
        acquired = False
        try:
            if kind == "windows":
                while True:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    try:
                        api.locking(descriptor, api.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        if not _busy(error):
                            raise
                        if not blocking:
                            raise BlockingIOError(errno.EAGAIN, "Lock is held by another writer") from None
                        # LK_LOCK itself stops after ten attempts. Repeated
                        # nonblocking calls give POSIX-style blocking behavior.
                        time.sleep(0.05)
            else:
                operation = api.LOCK_EX | (0 if blocking else api.LOCK_NB)
                try:
                    api.flock(descriptor, operation)
                except OSError as error:
                    if not blocking and _busy(error):
                        raise BlockingIOError(errno.EAGAIN, "Lock is held by another writer") from None
                    raise
            acquired = True
            # Initialize only after obtaining the lock. Two contenders cannot
            # race to write this byte while another process has it locked.
            if os.fstat(descriptor).st_size == 0:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            self._fd, self._kind, self._api = descriptor, kind, api
            return self
        except BaseException:
            try:
                if acquired:
                    _unlock(descriptor, kind, api)
            finally:
                os.close(descriptor)
            raise

    def release(self):
        descriptor, kind, api = self._fd, self._kind, self._api
        self._fd, self._kind, self._api = None, None, None
        if descriptor is not None:
            try:
                _unlock(descriptor, kind, api)
            finally:
                os.close(descriptor)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


def acquire(path, blocking=True):
    return FileLock(path).acquire(blocking=blocking)


def release(lock):
    lock.release()


@contextmanager
def exclusive(path, blocking=True):
    lock = acquire(path, blocking=blocking)
    try:
        yield lock
    finally:
        lock.release()
