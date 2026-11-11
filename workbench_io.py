"""UTF-8 CSV storage for the local workbench."""

import os
from contextlib import contextmanager
from pathlib import Path
import stat
import tempfile
import time

import pandas as pd
from workbench_lock import exclusive


@contextmanager
def csv_transaction(filename):
    """Serialize same-file read/modify/write operations across threads/processes.

    Keep the lock file in place: unlinking it could give concurrent writers
    different lock inodes. Network work must finish before entering this scope.
    """
    target = Path(filename)
    lock_path = target.with_name(f".{target.name}.lock")
    with exclusive(lock_path):
        yield


def read_csv_file(filename):
    """Read saved records; an absent or genuinely empty file has no records."""
    for attempt in range(3):
        try:
            return pd.read_csv(filename, dtype=str, encoding="utf-8-sig")
        except FileNotFoundError:
            return pd.DataFrame()
        except pd.errors.EmptyDataError:
            # Older running sessions may still be finishing a non-atomic save.
            if attempt == 2:
                return pd.DataFrame()
            time.sleep(0.05)


def write_csv_atomic(frame, filename):
    """Keep the previous complete CSV visible until its replacement is ready."""
    target = Path(filename)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            frame.to_csv(stream, index=False)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
