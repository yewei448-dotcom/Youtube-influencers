"""One background mailbox sync per workbench process; never blocks page rendering."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
import time


class ReplySyncWorker:
    def __init__(self, sync, interval=300, account_id=None, scope_factory=None):
        from mail_accounts import account_scope, current_account_id
        self.sync = sync
        self.account_id = str(current_account_id() if account_id is None else account_id or "")
        self.scope_factory = scope_factory or account_scope
        self.interval = interval
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reply-sync")
        self.lock = Lock()
        self.future = None
        self.last_attempt = 0
        self.last_success = ""
        self.error = False

    def start(self, force=False):
        if not self.account_id:
            return False
        with self.lock:
            if self.future is not None and not self.future.done():
                return False
            if not force and self.last_attempt and time.monotonic() - self.last_attempt < self.interval:
                return False
            self.last_attempt = time.monotonic()
            self.future = self.executor.submit(self._run)
            return True

    def _run(self):
        try:
            with self.scope_factory(self.account_id):
                self.sync()
        except Exception:
            # Never surface IMAP credentials, server text, or private messages.
            with self.lock:
                self.error = True
            return
        with self.lock:
            self.last_success = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.error = False

    def status(self):
        with self.lock:
            return {"running": self.future is not None and not self.future.done(),
                    "last_success": self.last_success, "error": self.error,
                    "account_id": self.account_id or None, "read_only": not bool(self.account_id)}

    def close(self, wait=False):
        self.executor.shutdown(wait=wait, cancel_futures=True)
