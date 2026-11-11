"""Independent public-upload monitoring. No Streamlit, email, or CSV mutations.

Network clients are injectable; importing this module never reads credentials.
All saved instants are UTC. Python 3.9+; standard library only.
"""

import configparser
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo
from workbench_lock import FileLock


CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BEIJING = ZoneInfo("Asia/Shanghai")
TERMINAL_API_ERRORS = frozenset(("quota", "missing_key", "invalid_key"))


def moment(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("需要有效的时间")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def iso(value=None):
    return moment(value).isoformat(timespec="seconds")


def _parsed(value):
    try:
        return moment(value) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def _channel_id(value):
    if isinstance(value, str) and CHANNEL_ID.fullmatch(value):
        return value
    raise ValueError("请使用规范的 YouTube 频道 ID")


def _public_email(value):
    if not isinstance(value, str):
        return ""
    excluded = {"example.com", "test.com", "domain.com", "email.com", "youtube.com"}
    for found in EMAIL.findall(value):
        if found.rsplit("@", 1)[-1].casefold() not in excluded:
            return found
    return ""


class MonitorApiError(Exception):
    """Only fixed public messages cross the API boundary."""
    MESSAGES = {
        "missing_key": "尚未配置 YouTube API Key。",
        "invalid_key": "YouTube API Key 无效或访问限制不匹配。",
        "quota": "YouTube API 配额或速率限制已达到。",
        "network": "无法连接 YouTube API，请稍后重试。",
        "http": "YouTube API 请求未成功。",
        "invalid_response": "YouTube API 响应格式异常，本次数据未视为成功。",
        "unavailable": "频道或视频的公开资料暂不可用。",
        "cancelled": "检查已停止，未完成进度已保留。",
    }

    def __init__(self, kind, status=None):
        self.kind = kind if kind in self.MESSAGES else "http"
        self.status = status
        super().__init__(self.MESSAGES[self.kind])

    def as_dict(self):
        return {"kind": self.kind, "message": str(self), "http_status": self.status}


class YouTubeMonitorAPI:
    BASE = "https://www.googleapis.com/youtube/v3/"

    def __init__(self, api_key, transport=None, timeout=15):
        self._key = api_key.strip() if isinstance(api_key, str) else ""
        self.transport, self.timeout = transport, timeout

    @classmethod
    def from_config(cls, path=None, **kwargs):
        """Called by an explicit job only; never prints config or request URLs."""
        key = os.environ.get("YOUTUBE_API_KEY", "")
        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read(str(path or Path(__file__).with_name("email_config.ini")), encoding="utf-8")
            key = config.get("youtube", "api_key", fallback=key)
        except (configparser.Error, OSError, UnicodeError):
            raise MonitorApiError("missing_key") from None
        return cls(key, **kwargs)

    def call(self, endpoint, params):
        if endpoint not in ("channels", "playlistItems", "videos"):
            raise ValueError("Unsupported monitoring endpoint")
        if not self._key:
            raise MonitorApiError("missing_key")
        if self._key in ("YOUR_API_KEY", "你的YouTube_API_key", "在这里填你的YouTube_API_key"):
            raise MonitorApiError("invalid_key")
        request_params = dict(params, key=self._key)
        if self.transport is not None:
            try:
                response = self.transport.get(self.BASE + endpoint, params=request_params, timeout=self.timeout)
                status = response.status_code
            except Exception:
                raise MonitorApiError("network") from None
            try:
                payload = response.json()
            except Exception:
                raise MonitorApiError("invalid_response" if status == 200 else "http", status) from None
        else:
            try:
                url = self.BASE + endpoint + "?" + urllib.parse.urlencode(request_params)
                try:
                    with urllib.request.urlopen(url, timeout=self.timeout) as response:
                        status, raw = response.status, response.read()
                except urllib.error.HTTPError as error:
                    status, raw = error.code, error.read()
            except Exception:
                raise MonitorApiError("network") from None
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                raise MonitorApiError("invalid_response" if status == 200 else "http", status) from None
        if not isinstance(payload, dict):
            raise MonitorApiError("invalid_response", status)
        if status != 200 or "error" in payload:
            error = payload.get("error", {})
            error = error if isinstance(error, dict) else {}
            values = error.get("errors", []) + error.get("details", []) if isinstance(error.get("errors", []), list) and isinstance(error.get("details", []), list) else []
            reasons = {str(item.get("reason", "")) for item in values if isinstance(item, dict)} | {str(error.get("status", ""))}
            if status == 429 or reasons.intersection({"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded", "RESOURCE_EXHAUSTED"}):
                raise MonitorApiError("quota", status)
            if reasons.intersection({"keyInvalid", "API_KEY_INVALID", "ipRefererBlocked", "refererBlocked", "API_KEY_SERVICE_BLOCKED"}):
                raise MonitorApiError("invalid_key", status)
            raise MonitorApiError("http", status)
        if not isinstance(payload.get("items"), list):
            raise MonitorApiError("invalid_response", status)
        return payload


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products (product TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS channels (
 channel_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', homepage TEXT NOT NULL,
 email TEXT NOT NULL DEFAULT '', email_source TEXT NOT NULL DEFAULT '',
 uploads_id TEXT NOT NULL DEFAULT '', metadata_at TEXT);
CREATE TABLE IF NOT EXISTS watches (
 watch_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(channel_id),
 product TEXT NOT NULL REFERENCES products(product), enabled INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, baseline_complete INTEGER NOT NULL DEFAULT 0, removed_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS current_watch ON watches(channel_id,product) WHERE removed_at IS NULL;
CREATE TABLE IF NOT EXISTS keywords (
 product TEXT NOT NULL REFERENCES products(product), normalized TEXT NOT NULL, keyword TEXT NOT NULL,
 priorities TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT '', PRIMARY KEY(product,normalized));
CREATE TABLE IF NOT EXISTS videos (
 video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(channel_id),
 title TEXT NOT NULL DEFAULT '', published_at TEXT, first_seen_at TEXT NOT NULL,
 latest_seen_at TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 0,
 detail_pending INTEGER NOT NULL DEFAULT 1, live_status TEXT NOT NULL DEFAULT 'none');
CREATE INDEX IF NOT EXISTS videos_channel ON videos(channel_id);
CREATE TABLE IF NOT EXISTS watch_videos (
 watch_id TEXT NOT NULL REFERENCES watches(watch_id), video_id TEXT NOT NULL REFERENCES videos(video_id),
 kind TEXT NOT NULL, discovered_at TEXT NOT NULL, PRIMARY KEY(watch_id,video_id));
CREATE TABLE IF NOT EXISTS snapshots (
 video_id TEXT NOT NULL REFERENCES videos(video_id), observed_at TEXT NOT NULL, views INTEGER NOT NULL,
 PRIMARY KEY(video_id,observed_at));
CREATE TABLE IF NOT EXISTS checks (
 channel_id TEXT PRIMARY KEY REFERENCES channels(channel_id), last_attempt TEXT, last_success TEXT,
 last_stats_success TEXT,
 covered_through TEXT, last_full_success TEXT, last_error TEXT, error_kind TEXT,
 status TEXT NOT NULL DEFAULT 'uninitialized', cursor TEXT, pass_started TEXT,
 pass_mode TEXT, pass_boundary TEXT, baseline_complete INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS scan_seen (
 channel_id TEXT NOT NULL, pass_started TEXT NOT NULL, video_id TEXT NOT NULL,
 PRIMARY KEY(channel_id,pass_started,video_id));
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
 checked_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0,
 pages INTEGER NOT NULL DEFAULT 0, error_kind TEXT, last_error TEXT);
"""


class MonitorStore:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            if "last_stats_success" not in {row[1] for row in connection.execute("PRAGMA table_info(checks)")}:
                connection.execute("ALTER TABLE checks ADD COLUMN last_stats_success TEXT")
            connection.execute("INSERT OR IGNORE INTO meta VALUES ('schedule',?)", (json.dumps({"time": "09:00", "enabled": True, "timezone": "Asia/Shanghai"}),))

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _put(connection, key, value):
        connection.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def _get(self, key, default=None):
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value):
        with self.connection() as connection:
            self._put(connection, key, value)

    def seed(self, seed_path, now=None):
        """One atomic import. Existing user rules never get reset on rerender."""
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        with self.connection() as connection:
            if connection.execute("SELECT 1 FROM meta WHERE key='seed_imported'").fetchone():
                return False
            stamp = iso(now)
            for item in data.get("configured_keywords", []):
                product, keyword = str(item.get("product", "")).strip(), str(item.get("keyword", "")).strip()
                if not product or not keyword:
                    continue
                connection.execute("INSERT OR IGNORE INTO products VALUES (?)", (product,))
                connection.execute("INSERT OR IGNORE INTO keywords VALUES (?,?,?,?,?)",
                                   (product, keyword.casefold(), keyword, json.dumps(item.get("priorities", [])), "导入当前配置"))
            for item in data.get("channels", []):
                cid = _channel_id(item.get("channel_id"))
                product = str(item.get("product", "")).strip()
                if not product:
                    continue
                connection.execute("INSERT OR IGNORE INTO products VALUES (?)", (product,))
                connection.execute("INSERT OR IGNORE INTO channels(channel_id,name,homepage,email,email_source) VALUES (?,?,?,?,?)",
                                   (cid, str(item.get("name", "")), "https://www.youtube.com/channel/" + cid,
                                    _public_email(item.get("email_source")), "导入名单（未核验）" if _public_email(item.get("email_source")) else ""))
                connection.execute("INSERT OR IGNORE INTO watches(watch_id,channel_id,product,created_at) VALUES (?,?,?,?)",
                                   (str(uuid4()), cid, product, stamp))
            self._put(connection, "seed_imported", {"at": stamp, "source": str(data.get("source", ""))})
        return True

    def products(self):
        with self.connection() as connection:
            return [row[0] for row in connection.execute("SELECT product FROM products ORDER BY rowid")]

    def get_schedule(self, now=None):
        settings = self._get("schedule", {"time": "09:00", "enabled": True, "timezone": "Asia/Shanghai"})
        request = self._get("request", {})
        settings.update(request_pending=bool(request), requested_at=request.get("at"), request_id=request.get("id"))
        local = moment(now).astimezone(BEIJING)
        hour, minute = map(int, settings["time"].split(":"))
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= local:
            due += timedelta(days=1)
        settings["next_run_at"] = due.isoformat() if settings["enabled"] else None
        return settings

    def set_schedule(self, time="09:00", enabled=True):
        if not isinstance(time, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", time):
            raise ValueError("检查时间请填写 HH:MM")
        self._set("schedule", {"time": time, "enabled": bool(enabled), "timezone": "Asia/Shanghai"})

    def request_check(self, now=None):
        request = {"id": str(uuid4()), "at": iso(now)}
        self._set("request", request)
        return request

    request_run = request_check

    def consume_request(self, request_id):
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key='request'").fetchone()
            if row and json.loads(row[0]).get("id") == request_id:
                connection.execute("DELETE FROM meta WHERE key='request'")

    def touch_worker(self, status="idle", now=None):
        self._set("worker", {"at": iso(now), "status": status, "pid": os.getpid()})

    def ensure_product(self, product):
        """Register a product without borrowing keywords or changing existing rules."""
        product = str(product).strip()
        if not product:
            raise ValueError("请指定产品")
        with self.connection() as connection:
            connection.execute("INSERT OR IGNORE INTO products VALUES (?)", (product,))

    def set_keywords(self, product, keywords):
        product = str(product).strip()
        if not product:
            raise ValueError("请指定产品")
        values = list(dict.fromkeys(str(word).strip() for word in keywords if str(word).strip()))
        with self.connection() as connection:
            connection.execute("INSERT OR IGNORE INTO products VALUES (?)", (product,))
            previous = {row["normalized"]: dict(row) for row in connection.execute("SELECT * FROM keywords WHERE product=?", (product,))}
            connection.execute("DELETE FROM keywords WHERE product=?", (product,))
            for value in values:
                old = previous.get(value.casefold(), {})
                connection.execute("INSERT OR IGNORE INTO keywords VALUES (?,?,?,?,?)", (product, value.casefold(), value,
                                   old.get("priorities", "[]"), old.get("source", "用户配置")))

    def set_watch(self, channel_id, product, enabled=True, channel=None, now=None):
        cid, product = _channel_id(channel_id), str(product).strip()
        if not product:
            raise ValueError("请指定产品")
        channel = channel or {}
        stamp = iso(now)
        with self.connection() as connection:
            connection.execute("INSERT OR IGNORE INTO products VALUES (?)", (product,))
            connection.execute("INSERT OR IGNORE INTO channels(channel_id,homepage) VALUES (?,?)", (cid, "https://www.youtube.com/channel/" + cid))
            for field in ("name", "email"):
                if field in channel and isinstance(channel[field], str) and channel[field].strip():
                    value = _public_email(channel[field]) if field == "email" else channel[field].strip()
                    if field == "email" and not value:
                        raise ValueError("邮箱格式无效")
                    connection.execute("UPDATE channels SET " + field + "=? WHERE channel_id=?", (value, cid))
                    if field == "email":
                        connection.execute("UPDATE channels SET email_source='用户录入（未核验）' WHERE channel_id=?", (cid,))
            row = connection.execute("SELECT watch_id FROM watches WHERE channel_id=? AND product=? AND removed_at IS NULL", (cid, product)).fetchone()
            if row:
                watch_id = row[0]
                connection.execute("UPDATE watches SET enabled=? WHERE watch_id=?", (int(bool(enabled)), watch_id))
                if enabled:
                    connection.execute("""INSERT OR IGNORE INTO watch_videos
                        SELECT ?,v.video_id,CASE WHEN v.published_at IS NULL THEN 'pending'
                        WHEN v.live_status='upcoming' THEN 'scheduled' WHEN v.published_at>=w.created_at
                        THEN 'new' ELSE 'history' END,? FROM videos v JOIN watches w ON w.watch_id=?
                        WHERE v.channel_id=?""", (watch_id, stamp, watch_id, cid))
            else:
                watch_id = str(uuid4())
                connection.execute("INSERT INTO watches(watch_id,channel_id,product,enabled,created_at) VALUES (?,?,?,?,?)",
                                   (watch_id, cid, product, int(bool(enabled)), stamp))
                # Every subscription gets its own baseline. Existing events
                # from another product are never copied as new invitations.
                connection.execute("""INSERT OR IGNORE INTO watch_videos SELECT ?,video_id,
                    CASE WHEN published_at IS NULL THEN 'pending' WHEN live_status='upcoming'
                    THEN 'scheduled' ELSE 'history' END,? FROM videos WHERE channel_id=?""", (watch_id, stamp, cid))
        return watch_id

    def remove_watch(self, channel_id, product, now=None):
        with self.connection() as connection:
            connection.execute("UPDATE watches SET enabled=0,removed_at=? WHERE channel_id=? AND product=? AND removed_at IS NULL",
                               (iso(now), _channel_id(channel_id), product))

    def list_channels(self, product=None, include_paused=True):
        query = """SELECT c.*,w.watch_id,w.product,w.enabled,w.created_at,w.baseline_complete,
                   k.last_success,k.last_stats_success,k.last_attempt,k.last_error,k.status,k.cursor
                   FROM watches w JOIN channels c USING(channel_id) LEFT JOIN checks k USING(channel_id)
                   WHERE w.removed_at IS NULL"""
        args = []
        if product is not None:
            query += " AND w.product=?"
            args.append(product)
        if not include_paused:
            query += " AND w.enabled=1"
        with self.connection() as connection:
            rows = [dict(row) for row in connection.execute(query + " ORDER BY w.rowid", args)]
        for row in rows:
            row["enabled"], row["baseline_complete"] = bool(row["enabled"]), bool(row["baseline_complete"])
            row["status"] = row["status"] or "uninitialized"
        return rows

    def get_rules(self, product):
        with self.connection() as connection:
            keywords = [row[0] for row in connection.execute("SELECT keyword FROM keywords WHERE product=? ORDER BY rowid", (product,))]
        return {"product": product, "keywords": keywords, "channels": self.list_channels(product)}

    def list_updates(self, product, include_history=False, matched_only=False, since=None, include_archived=False):
        query = """SELECT v.*,w.product,w.watch_id,w.enabled,w.removed_at,wv.kind,wv.discovered_at,
                   c.name,c.homepage,c.email,c.email_source FROM watch_videos wv
                   JOIN watches w USING(watch_id) JOIN videos v USING(video_id) JOIN channels c ON c.channel_id=v.channel_id
                   WHERE w.product=? AND wv.kind IN ('new','history')"""
        args = [product]
        if not include_history:
            query += " AND wv.kind='new'"
        if not include_archived:
            query += " AND w.removed_at IS NULL"
        if since:
            query += " AND v.published_at>=?"
            args.append(iso(since))
        with self.connection() as connection:
            rows = [dict(row) for row in connection.execute(query + " ORDER BY v.published_at DESC,v.video_id", args)]
            snapshots = {}
            for row in connection.execute("""SELECT video_id,observed_at,views FROM
                (SELECT *,ROW_NUMBER() OVER(PARTITION BY video_id ORDER BY observed_at DESC) AS n FROM snapshots) WHERE n<=2"""):
                snapshots.setdefault(row["video_id"], []).append(dict(row))
        keywords = self.get_rules(product)["keywords"]
        out = []
        for row in rows:
            hits = [word for word in keywords if word.casefold() in row["title"].casefold()]
            if matched_only and not hits:
                continue
            values = snapshots.get(row["video_id"], [])
            latest = values[0] if values else {}
            pub, observed = _parsed(row["published_at"]), _parsed(latest.get("observed_at"))
            hours = (observed - pub).total_seconds() / 3600 if observed and pub else None
            views = latest.get("views")
            lifetime = views / hours if views is not None and hours is not None and hours > 0 else None
            interval, interval_hours, interval_status = None, None, "pending"
            if len(values) > 1:
                previous = values[1]
                interval_hours = (observed - moment(previous["observed_at"])).total_seconds() / 3600
                delta = views - previous["views"]
                if interval_hours > 0 and delta >= 0:
                    interval, interval_status = delta / interval_hours, "ok"
                elif delta < 0:
                    interval_status = "adjustment"
            row.update(video_url="https://www.youtube.com/watch?v=" + row["video_id"],
                       is_new=row["kind"] == "new", matched_keywords=hits, views=views,
                       observed_at=latest.get("observed_at"), lifetime_vph=lifetime, interval_vph=interval,
                       interval_hours=interval_hours, interval_status=interval_status, age_hours=hours,
                       early_sample=bool(hours is not None and 0 < hours < 1), available=bool(row["available"]))
            out.append(row)
        return out

    def overview(self, product=None, now=None):
        with self.connection() as connection:
            last = connection.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
            success = connection.execute("SELECT completed_at FROM runs WHERE status='completed' ORDER BY rowid DESC LIMIT 1").fetchone()
        worker = self._get("worker", {})
        heartbeat = _parsed(worker.get("at"))
        alive = bool(heartbeat and 0 <= (moment(now) - heartbeat).total_seconds() < 90 and worker.get("status") != "stopped")
        run = dict(last) if last else {}
        running = run.get("status") == "running" and alive
        rows = self.list_channels(product)
        return {"running": running, "status": ("interrupted" if run.get("status") == "running" and not alive else run.get("status", "not_run")),
                "last_success": success[0] if success else None, "last_started": run.get("started_at"),
                "last_error": run.get("last_error") or self._get("worker_error"), "worker_alive": alive, "last_heartbeat": worker.get("at"),
                "channel_count": len(rows), "enabled_count": sum(row["enabled"] for row in rows),
                "baseline_pending": sum(not row["baseline_complete"] for row in rows),
                "failed_count": run.get("failed_count", 0), "checked_count": run.get("checked_count", 0),
                "request_pending": self.get_schedule(now)["request_pending"], "run": run}


def _items(api, endpoint, params):
    try:
        result = api.call(endpoint, params)
    except MonitorApiError:
        raise
    except Exception:
        raise MonitorApiError("network") from None
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise MonitorApiError("invalid_response")
    return result


def _details(api, ids):
    found = {}
    for start in range(0, len(ids), 50):
        batch = list(dict.fromkeys(ids[start:start + 50]))
        payload = _items(api, "videos", {"part": "snippet,statistics", "id": ",".join(batch)})
        for item in payload["items"]:
            if not isinstance(item, dict) or item.get("id") not in batch:
                raise MonitorApiError("invalid_response")
            snippet, statistics = item.get("snippet"), item.get("statistics")
            if not isinstance(snippet, dict) or not isinstance(statistics, dict):
                raise MonitorApiError("invalid_response")
            raw = statistics.get("viewCount")
            if isinstance(raw, bool) or not isinstance(raw, (int, str)) or not str(raw).isdigit():
                raise MonitorApiError("invalid_response")
            published = _parsed(snippet.get("publishedAt"))
            if not isinstance(snippet.get("title"), str) or not CHANNEL_ID.fullmatch(str(snippet.get("channelId", ""))) or published is None:
                raise MonitorApiError("invalid_response")
            found[item["id"]] = {"channel_id": snippet["channelId"], "title": snippet["title"],
                                  "published_at": iso(published), "views": int(raw),
                                  "live_status": snippet.get("liveBroadcastContent", "none")}
    return found


def _save_details(store, ids, found, stamp):
    with store.connection() as connection:
        for vid in ids:
            known = connection.execute("SELECT channel_id FROM videos WHERE video_id=?", (vid,)).fetchone()
            if not known:
                continue
            item = found.get(vid)
            if item is None:
                connection.execute("UPDATE videos SET available=0,detail_pending=1 WHERE video_id=?", (vid,))
                continue
            if item["channel_id"] != known[0]:
                raise MonitorApiError("invalid_response")
            connection.execute("UPDATE videos SET title=?,published_at=?,available=1,detail_pending=0,live_status=? WHERE video_id=?",
                               (item["title"], item["published_at"], item["live_status"], vid))
            connection.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?)", (vid, stamp, item["views"]))
            watches = connection.execute("SELECT watch_id,created_at FROM watches WHERE channel_id=? AND enabled=1 AND removed_at IS NULL", (known[0],)).fetchall()
            for watch in watches:
                kind = "new" if moment(item["published_at"]) >= moment(watch["created_at"]) else "history"
                if item["live_status"] == "upcoming" or moment(item["published_at"]) > moment(stamp):
                    kind = "scheduled"
                connection.execute("INSERT OR IGNORE INTO watch_videos VALUES (?,?,?,?)", (watch["watch_id"], vid, kind, stamp))
                connection.execute("UPDATE watch_videos SET kind=? WHERE watch_id=? AND video_id=? AND kind IN ('pending','scheduled')", (kind, watch["watch_id"], vid))


def run_once(store, api=None, now=None, max_pages=100, max_seconds=240, refresh_days=30, stop=None):
    """One bounded sweep. Budget exhaustion remains pending, never success.

    Each pass freezes a date boundary: recent references plus any uncovered
    gap. An old page need not have been seen before. No pass requires all of
    a channel's lifetime history to be downloaded.
    """
    if max_pages < 1 or max_seconds <= 0 or refresh_days < 1:
        raise ValueError("监控预算必须为正数")
    lock_path = store.path.with_name(store.path.name + ".scan.lock")
    lock = FileLock(lock_path)
    try:
        lock.acquire(blocking=False)
    except BlockingIOError:
        return {"status": "busy", "checked_count": 0, "failed_count": 0, "pages": 0}
    try:
        try:
            return _run_locked(store, api, now, max_pages, max_seconds, refresh_days, stop)
        except Exception as error:
            safe = str(error) if isinstance(error, MonitorApiError) else "监控数据未完整保存，已保留之前的有效记录。"
            kind = error.kind if isinstance(error, MonitorApiError) else "storage"
            result = {"status": "partial", "checked_count": 0, "failed_count": 0,
                      "pages": 0, "error_kind": kind, "last_error": safe}
            try:
                with store.connection() as connection:
                    connection.execute("UPDATE runs SET status='partial',completed_at=?,error_kind=?,last_error=? WHERE status='running'", (iso(now), kind, safe))
                store.touch_worker("idle", now)
            except Exception:
                pass
            return result
    finally:
        lock.release()


def _run_locked(store, api, now, max_pages, max_seconds, refresh_days, stop):
    clock = lambda: moment(now) if now is not None else moment()
    started, began = iso(clock()), time.monotonic()
    run_id = str(uuid4())
    with store.connection() as connection:
        connection.execute("UPDATE runs SET status='interrupted',completed_at=? WHERE status='running'", (started,))
        connection.execute("INSERT INTO runs(run_id,started_at,status) VALUES (?,?,'running')", (run_id, started))
    store.touch_worker("running", clock())
    active_watches = store.list_channels(include_paused=False)
    channels = list(dict.fromkeys(row["channel_id"] for row in active_watches))
    watch_starts = {cid: min(moment(row["created_at"]) for row in active_watches if row["channel_id"] == cid)
                    for cid in channels}
    reference_cutoff = moment(started) - timedelta(days=refresh_days)
    scope_cutoffs = {}
    metadata, metadata_errors = {}, {}
    with store.connection() as connection:
        attempts = {row[0]: row[1] for row in connection.execute("SELECT channel_id,last_attempt FROM checks")}
    channels.sort(key=lambda cid: attempts.get(cid) or "")
    setup_error = None
    try:
        api = api or YouTubeMonitorAPI.from_config(store.path.parent / "email_config.ini")
    except MonitorApiError as error:
        setup_error = error
        metadata_errors = {cid: error for cid in channels}
    for start in range(0, len(channels), 50):
        batch = channels[start:start + 50]
        try:
            if setup_error:
                break
            response = _items(api, "channels", {"part": "snippet,contentDetails", "id": ",".join(batch)})
            for item in response["items"]:
                if not isinstance(item, dict) or item.get("id") not in batch:
                    raise MonitorApiError("invalid_response")
                snippet = item.get("snippet", {})
                uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                if not isinstance(snippet, dict) or not isinstance(uploads, str) or not uploads:
                    raise MonitorApiError("invalid_response")
                metadata[item["id"]] = {"name": str(snippet.get("title", "")), "uploads_id": uploads,
                                        "email": _public_email(snippet.get("description", ""))}
        except MonitorApiError as error:
            for cid in batch:
                metadata_errors[cid] = error
            if error.kind in TERMINAL_API_ERRORS:
                for cid in channels:
                    metadata_errors[cid] = error
                break

    checked = failed = pages = 0
    failed_channels = set()
    sample_failures = set()
    sampled = set()
    channel_page_budget = max(1, max_pages // max(1, len(channels)))
    deferred, fatal, last_error = False, None, None
    for cid in channels:
        if stop and stop():
            deferred, fatal = True, "cancelled"
            break
        if pages >= max_pages or time.monotonic() - began >= max_seconds:
            deferred = True
            break
        stamp = iso(clock())
        with store.connection() as connection:
            connection.execute("INSERT OR IGNORE INTO checks(channel_id) VALUES (?)", (cid,))
            connection.execute("UPDATE checks SET last_attempt=?,status='running',last_error=NULL,error_kind=NULL WHERE channel_id=?", (stamp, cid))
        try:
            if cid in metadata_errors:
                raise metadata_errors[cid]
            if cid not in metadata:
                raise MonitorApiError("unavailable")
            info = metadata[cid]
            with store.connection() as connection:
                connection.execute("UPDATE channels SET name=?,uploads_id=?,metadata_at=? WHERE channel_id=?", (info["name"], info["uploads_id"], stamp, cid))
                if info["email"]:
                    connection.execute("UPDATE channels SET email=?,email_source='频道公开简介（未核验）' WHERE channel_id=? AND email=''", (info["email"], cid))
                check = dict(connection.execute("SELECT * FROM checks WHERE channel_id=?", (cid,)).fetchone())
                if not check["pass_started"]:
                    full_due = not check["baseline_complete"] or not check["last_full_success"] or (clock() - moment(check["last_full_success"])).total_seconds() >= 7 * 86400
                    anchor = _parsed(check.get("covered_through")) or watch_starts[cid]
                    cutoff = min(reference_cutoff, anchor - timedelta(days=2))
                    check.update(pass_started=stamp, cursor=None, pass_mode="full" if full_due else "incremental",
                                 pass_boundary=iso(cutoff))
                    connection.execute("UPDATE checks SET pass_started=?,cursor=NULL,pass_mode=?,pass_boundary=? WHERE channel_id=?",
                                       (stamp, check["pass_mode"], check["pass_boundary"], cid))
                scope_cutoffs[cid] = _parsed(check["pass_boundary"]) or reference_cutoff
            seen_tokens, completed, channel_pages = set(), False, 0
            while True:
                if stop and stop():
                    raise MonitorApiError("cancelled")
                if pages >= max_pages or channel_pages >= channel_page_budget or time.monotonic() - began >= max_seconds:
                    deferred = True
                    break
                token = check["cursor"]
                if token in seen_tokens:
                    raise MonitorApiError("invalid_response")
                seen_tokens.add(token)
                params = {"part": "contentDetails", "playlistId": info["uploads_id"], "maxResults": 50}
                if token:
                    params["pageToken"] = token
                payload = _items(api, "playlistItems", params)
                pages += 1
                channel_pages += 1
                raw_next = payload.get("nextPageToken")
                if raw_next is not None and not isinstance(raw_next, str):
                    raise MonitorApiError("invalid_response")
                next_token = raw_next or None
                if len(payload["items"]) > 50 or (next_token is not None and next_token in seen_tokens) or (next_token and not payload["items"]):
                    raise MonitorApiError("invalid_response")
                ids = []
                for item in payload["items"]:
                    details = item.get("contentDetails") if isinstance(item, dict) else None
                    vid = details.get("videoId") if isinstance(details, dict) else None
                    if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid):
                        raise MonitorApiError("invalid_response")
                    ids.append(vid)
                ids = list(dict.fromkeys(ids))
                page_stamp = iso(clock())
                with store.connection() as connection:
                    previous = {row["video_id"]: dict(row) for row in connection.execute("SELECT video_id,published_at,detail_pending FROM videos WHERE channel_id=?", (cid,))}
                    pass_seen = {row[0] for row in connection.execute("SELECT video_id FROM scan_seen WHERE channel_id=? AND pass_started=?", (cid, check["pass_started"]))}
                    if next_token and ids and all(vid in pass_seen for vid in ids):
                        raise MonitorApiError("invalid_response")
                    for vid in ids:
                        connection.execute("INSERT OR IGNORE INTO videos(video_id,channel_id,first_seen_at,latest_seen_at) VALUES (?,?,?,?)", (vid, cid, page_stamp, page_stamp))
                        existing = connection.execute("SELECT channel_id FROM videos WHERE video_id=?", (vid,)).fetchone()
                        if existing[0] != cid:
                            raise MonitorApiError("invalid_response")
                        connection.execute("UPDATE videos SET latest_seen_at=? WHERE video_id=?", (page_stamp, vid))
                # Persist discovered IDs first. If details fail, the same
                # page is retried and the discovered IDs remain available.
                details = _details(api, ids) if ids else {}
                _save_details(store, ids, details, page_stamp)
                sampled.update(details)
                boundary = _parsed(check["pass_boundary"])
                dated = {vid: details.get(vid, {}).get("published_at") or previous.get(vid, {}).get("published_at") for vid in ids}
                old_page = bool(ids and boundary and all(dated[vid] and moment(dated[vid]) < boundary for vid in ids))
                known_head = bool(ids and all(vid in previous for vid in ids))
                completed = next_token is None or old_page or (check["pass_mode"] == "head" and known_head)
                followup_head = completed and check["pass_mode"] != "head" and check["pass_started"] < started
                with store.connection() as connection:
                    for vid in ids:
                        connection.execute("INSERT OR IGNORE INTO scan_seen VALUES (?,?,?)", (cid, check["pass_started"], vid))
                    if completed:
                        connection.execute("UPDATE checks SET cursor=NULL,pass_started=NULL,pass_mode=NULL,pass_boundary=NULL,status='completed',last_success=?,covered_through=?,baseline_complete=1,last_error=NULL,error_kind=NULL WHERE channel_id=?",
                                           (check["last_success"] if followup_head else page_stamp, check["pass_started"], cid))
                        if check["pass_mode"] == "full":
                            connection.execute("UPDATE checks SET last_full_success=? WHERE channel_id=?", (page_stamp, cid))
                        connection.execute("UPDATE watches SET baseline_complete=1 WHERE channel_id=? AND enabled=1 AND removed_at IS NULL", (cid,))
                        connection.execute("DELETE FROM scan_seen WHERE channel_id=?", (cid,))
                        if followup_head:
                            # A cursor resumed from an earlier run has not
                            # observed the head since that earlier instant.
                            # Queue one bounded head pass before calling the
                            # overall catch-up complete.
                            connection.execute("UPDATE checks SET status='pending',pass_started=?,pass_mode='head',pass_boundary=? WHERE channel_id=?",
                                               (page_stamp, check["pass_started"], cid))
                    else:
                        connection.execute("UPDATE checks SET cursor=?,status='pending' WHERE channel_id=?", (next_token, cid))
                check["cursor"] = next_token
                store.touch_worker("running", clock())
                if completed:
                    checked += 1
                    if followup_head:
                        deferred = True
                    break
            if not completed:
                with store.connection() as connection:
                    connection.execute("UPDATE checks SET status='pending' WHERE channel_id=?", (cid,))
        except MonitorApiError as error:
            failed_channels.add(cid)
            last_error = str(error)
            with store.connection() as connection:
                connection.execute("UPDATE checks SET status='failed',last_error=?,error_kind=? WHERE channel_id=?", (str(error), error.kind, cid))
                if (error.status == 400 and error.kind == "http") or error.kind == "invalid_response":
                    # An invalid saved page token can be safely restarted;
                    # IDs/events already committed remain deduplicated.
                    connection.execute("UPDATE checks SET cursor=NULL,pass_started=NULL WHERE channel_id=?", (cid,))
                    connection.execute("DELETE FROM scan_seen WHERE channel_id=?", (cid,))
            if error.kind in TERMINAL_API_ERRORS or error.kind == "cancelled":
                fatal = error.kind
                deferred = True
                break

    # Recent dynamic videos continue accumulating samples even when no new
    # upload was found. Missing/private originals retain their last good data.
    if fatal is None and not (stop and stop()) and time.monotonic() - began < max_seconds:
        with store.connection() as connection:
            candidates = [dict(row) for row in connection.execute("""SELECT DISTINCT v.video_id,v.channel_id,v.published_at,v.detail_pending FROM videos v JOIN watches w USING(channel_id)
                WHERE w.enabled=1 AND w.removed_at IS NULL AND (v.published_at>=? OR v.detail_pending=1)""", (iso(reference_cutoff),))]
        # Keep unresolved times retryable; a known old historical omission
        # outside this pass's scope must not trigger perpetual retries.
        recent = [row["video_id"] for row in candidates if not row["published_at"] or
                  moment(row["published_at"]) >= reference_cutoff or
                  (row["detail_pending"] and moment(row["published_at"]) >= scope_cutoffs.get(row["channel_id"], reference_cutoff))]
        recent = [vid for vid in recent if vid not in sampled]
        for start in range(0, len(recent), 50):
            if time.monotonic() - began >= max_seconds or (stop and stop()):
                deferred = True
                break
            batch = recent[start:start + 50]
            try:
                found = _details(api, batch)
                _save_details(store, batch, found, iso(clock()))
                sampled.update(found)
            except MonitorApiError as error:
                last_error = str(error)
                with store.connection() as connection:
                    affected = {row[0] for vid in batch for row in connection.execute("SELECT channel_id FROM videos WHERE video_id=?", (vid,))}
                    for affected_cid in affected:
                        connection.execute("UPDATE checks SET status='partial',last_error=?,error_kind=? WHERE channel_id=? AND status='completed'",
                                           (str(error), error.kind, affected_cid))
                sample_failures.update(affected)
                failed_channels.update(affected)
                if error.kind in TERMINAL_API_ERRORS:
                    fatal, deferred = error.kind, True
                    break
    with store.connection() as connection:
        for cid in channels:
            unresolved = connection.execute("SELECT published_at FROM videos WHERE channel_id=? AND detail_pending=1", (cid,)).fetchall()
            detail_boundary = min(reference_cutoff, scope_cutoffs.get(cid, reference_cutoff))
            unknown = any(not row[0] or moment(row[0]) >= detail_boundary for row in unresolved)
            check = connection.execute("SELECT status FROM checks WHERE channel_id=?", (cid,)).fetchone()
            if check and check[0] == "completed" and unknown:
                failed_channels.add(cid)
                last_error = MonitorApiError.MESSAGES["unavailable"]
                connection.execute("UPDATE checks SET status='partial',last_error=?,error_kind='unavailable' WHERE channel_id=?", (last_error, cid))
            elif check and check[0] == "completed" and cid not in sample_failures:
                connection.execute("UPDATE checks SET last_stats_success=? WHERE channel_id=?", (iso(clock()), cid))
    failed = len(failed_channels)
    status = "deferred" if deferred else ("partial" if failed else "completed")
    result = {"run_id": run_id, "status": status, "checked_count": checked, "failed_count": failed,
              "pages": pages, "error_kind": fatal, "last_error": last_error}
    with store.connection() as connection:
        connection.execute("UPDATE runs SET completed_at=?,status=?,checked_count=?,failed_count=?,pages=?,error_kind=?,last_error=? WHERE run_id=?",
                           (iso(clock()), status, checked, failed, pages, fatal, last_error, run_id))
    store.touch_worker("idle", clock())
    return result
