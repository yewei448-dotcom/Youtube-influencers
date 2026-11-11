"""Explicit local worker entry point. Importing it never starts a process.

Examples after deployment: python3 monitor_worker.py --once / --daemon
No scheduler is installed by this module.
"""

import argparse
import json
import signal
import threading
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from monitor_core import BEIJING, MonitorApiError, MonitorStore, YouTubeMonitorAPI, iso, moment, run_once


def is_due(store, now=None):
    """Return an explicit request or the outstanding local daily schedule."""
    current = moment(now)
    schedule = store.get_schedule(current)
    if schedule["request_pending"]:
        return True
    if not schedule["enabled"]:
        return False
    retry_at = store._get("retry_at")
    if retry_at:
        return current >= moment(retry_at)
    local = current.astimezone(BEIJING)
    if local.strftime("%H:%M") < schedule["time"]:
        return False
    return store._get("last_scheduled_day") != local.date().isoformat()


def record_attempt(store, result, request_id=None, now=None):
    current = moment(now)
    if result.get("status") == "busy":
        return
    local = current.astimezone(BEIJING)
    if local.strftime("%H:%M") >= store.get_schedule(current)["time"]:
        store._set("last_scheduled_day", local.date().isoformat())
    if request_id:
        store.consume_request(request_id)
    kind = result.get("error_kind")
    if kind == "quota":
        pacific = current.astimezone(ZoneInfo("America/Los_Angeles"))
        retry = (pacific + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        store._set("retry_at", iso(retry))
    elif kind in ("invalid_key", "missing_key"):
        store._set("retry_at", None)
    elif kind == "cancelled":
        store._set("retry_at", iso(current))
    elif result.get("status") == "partial":
        try:
            failures = max(0, int(store._get("partial_failure_count", 0))) + 1
        except (TypeError, ValueError, OverflowError):
            failures = 1
        minutes = (5, 10, 20, 40, 60)[min(failures - 1, 4)]
        store._set("partial_failure_count", failures)
        store._set("retry_at", iso(current + timedelta(minutes=minutes)))
    elif result.get("status") == "deferred":
        store._set("retry_at", iso(current + timedelta(minutes=5)))
    else:
        store._set("retry_at", None)
        if result.get("status") == "completed":
            store._set("partial_failure_count", 0)


def serve(store, config_path=None, stop_event=None, poll_seconds=5, runner=run_once):
    """Bounded polling waits are interruptible by SIGTERM/the supplied event."""
    stop_event = stop_event or threading.Event()
    store.touch_worker("idle")
    try:
        while not stop_event.is_set():
            store.touch_worker("idle")
            if is_due(store):
                request_id = store.get_schedule().get("request_id")
                try:
                    api = YouTubeMonitorAPI.from_config(config_path or store.path.parent / "email_config.ini")
                    result = runner(store, api=api, stop=stop_event.is_set)
                    store._set("worker_error", None)
                except MonitorApiError as error:
                    result = {"status": "partial", "error_kind": error.kind, "last_error": str(error)}
                    store._set("worker_error", result["last_error"])
                except Exception:
                    # No raw exception, URL, server text, or credentials escape.
                    result = {"status": "partial", "error_kind": "worker", "last_error": "后台检查未完成，已保留上次数据。"}
                    store._set("worker_error", result["last_error"])
                record_attempt(store, result, request_id)
            stop_event.wait(max(0.05, min(float(poll_seconds), 30)))
    finally:
        store.touch_worker("stopped")


def main(argv=None):
    parser = argparse.ArgumentParser(description="达人工作台独立每日监控")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--daemon", action="store_true")
    parser.add_argument("--db", default=str(Path(__file__).with_name("达人监测.sqlite3")))
    parser.add_argument("--seed", default=str(Path(__file__).with_name("monitor_seed.json")))
    parser.add_argument("--config", default=str(Path(__file__).with_name("email_config.ini")))
    args = parser.parse_args(argv)
    store = MonitorStore(args.db)
    if Path(args.seed).is_file():
        store.seed(args.seed)
    stop_event = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop_event.set())
    if args.daemon:
        serve(store, args.config, stop_event)
        return 0
    request_id = store.get_schedule().get("request_id")
    try:
        api = YouTubeMonitorAPI.from_config(args.config)
        result = run_once(store, api=api, stop=stop_event.is_set)
    except MonitorApiError as error:
        result = {"status": "partial", "error_kind": error.kind, "last_error": str(error)}
        store._set("worker_error", str(error))
    record_attempt(store, result, request_id)
    store.touch_worker("stopped")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
