"""Persistent manual channel decisions; no network, mail or CSV/config access.

Scopes are explicit. An absent product/language/account never broadens a local
decision. Expiry dates include the whole selected day in Beijing time.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit
from uuid import uuid4


SCHEMA_VERSION = 1
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
REASONS = {"channel": "频道不合适", "unsuitable": "已建联不适合", "no_reply": "多次未回复",
           "poor": "已合作效果差", "monitor": "已合作持续监测"}
REASON_LABELS = REASONS
LANGUAGES = {"en", "de", "ja", "ko", "zh-Hant", "fr", "es"}
_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}")
_ID_FIELDS = ("频道ID", "channel_id", "channelId")
_HOME_FIELDS = ("主页链接", "频道链接", "homepage", "channel_url")
_STORE_LOCK = threading.Lock()
_STORES = {}


class DecisionError(ValueError):
    pass


class DecisionConflict(DecisionError):
    pass


def _text(value):
    if value is None:
        return ""
    result = str(value).strip()
    return "" if result.lower() in {"nan", "none", "<na>", "nat"} else result


def _name(value):
    return " ".join(_text(value).split())


def _product(value):
    value = _name(value)
    return "ReiBoot" if value.casefold() == "reiboot for ios" else value


def _language(value, strict=False):
    value = _name(value)
    matched = next((code for code in LANGUAGES if code.casefold() == value.casefold()), "")
    if value and not matched and strict:
        raise DecisionError("请选择明确的目标语言：en/de/ja/ko/zh-Hant/fr/es。")
    return matched


def _moment(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise DecisionError("时间格式不正确。") from None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if not isinstance(value, datetime):
        raise DecisionError("时间格式不正确。")
    return (value if value.tzinfo is not None else value.replace(tzinfo=BEIJING)).astimezone(timezone.utc)


def _iso(value):
    return _moment(value).isoformat(timespec="microseconds")


def _channel_url(value):
    raw = _text(value)
    if _CHANNEL.fullmatch(raw):
        return raw
    try:
        url = urlsplit(raw if "://" in raw else "https://" + raw)
    except ValueError:
        return None
    if url.scheme not in {"http", "https"} or (url.hostname or "").lower() not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return None
    match = re.fullmatch(r"/channel/(UC[A-Za-z0-9_-]{22})(?:/(?:videos|shorts|streams|featured|community|playlists|about))?/?", url.path)
    return match.group(1) if match else None


def channel_identity(row):
    """Return a proven UC ID or None; names/emails/handles are not identities."""
    if isinstance(row, str):
        return _channel_url(row)
    if not hasattr(row, "get"):
        return None
    found = set()
    for field in _ID_FIELDS:
        value = _text(row.get(field))
        if value:
            if not _CHANNEL.fullmatch(value):
                return None
            found.add(value)
    for field in _HOME_FIELDS:
        value = _channel_url(row.get(field))
        if value:
            found.add(value)
    return next(iter(found)) if len(found) == 1 else None


def _reason(reason):
    if reason in REASONS:
        return reason
    key = next((key for key, label in REASONS.items() if label == reason), None)
    if key is None:
        raise DecisionError("请选择有效的人工处理分类。")
    return key


def _scope(reason, product, coop_type, language, account_id, all_products):
    if not isinstance(all_products, bool):
        raise DecisionError("全部产品范围必须明确选择。")
    result = {"product": _product(product), "coop_type": _name(coop_type),
              "language": _language(language, strict=True), "account_id": _text(account_id),
              "all_products": all_products}
    if all_products and reason != "channel":
        raise DecisionError("只有“频道不合适”可以明确选择全部产品。")
    if reason == "channel":
        if result["coop_type"] or result["language"] or result["account_id"]:
            raise DecisionError("频道不合适按产品或全部产品生效，不按合作形式、语言或账号限制。")
        if all_products:
            result["product"] = ""
    elif reason == "unsuitable":
        if result["account_id"]:
            raise DecisionError("已建联不适合不使用账号范围，请按产品、合作形式或目标语言记录。")
        if not result["coop_type"] and not result["language"]:
            raise DecisionError("请明确不适合的合作形式或目标语言，避免扩大排除。")
    elif reason == "no_reply":
        if not result["account_id"]:
            raise DecisionError("多次未回复必须明确发件账号。")
        if result["coop_type"] or result["language"]:
            raise DecisionError("多次未回复按产品和账号生效，不扩大为其他形式或语言规则。")
    elif reason == "poor":
        if not result["coop_type"]:
            raise DecisionError("合作效果差必须明确合作形式。")
        if result["language"] or result["account_id"]:
            raise DecisionError("合作效果差按产品和合作形式生效，不按语言或账号限制。")
    elif reason == "monitor":
        if result["coop_type"] or result["language"] or result["account_id"]:
            raise DecisionError("持续监测按产品生效，不按合作形式、语言或账号限制。")
    if not all_products and not result["product"]:
        raise DecisionError("请明确本次处理对应的产品。")
    for key, value in result.items():
        if isinstance(value, str) and (len(value) > 300 or "\0" in value):
            raise DecisionError("处理范围字段过长或格式不正确。")
    return result


def _scope_key(channel_id, reason, scope):
    values = [channel_id, reason, scope["product"].casefold(), scope["coop_type"].casefold(),
              scope["language"], scope["account_id"], scope["all_products"]]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


def _rows(frame):
    return frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)


def _take(frame, positions):
    if hasattr(frame, "iloc"):
        return frame.iloc[positions].copy(deep=True)
    return [deepcopy(frame[index]) for index in positions]


class DecisionStore:
    def __init__(self, path=None, *, root=None, clock=None):
        if path is not None and root is not None:
            raise DecisionError("请指定数据库路径或工作台目录，不要同时指定。")
        if path is None:
            path = Path(root if root is not None else Path.cwd()) / "resource_decisions.sqlite3"
        else:
            path = Path(path)
            if path.is_dir() or not path.suffix:
                path = path / "resource_decisions.sqlite3"
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise DecisionError("分类数据库版本不兼容，请联系维护者；未修改原记录。")
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
             id TEXT PRIMARY KEY, scope_key TEXT NOT NULL UNIQUE, channel_id TEXT NOT NULL,
             name TEXT NOT NULL, homepage TEXT NOT NULL, reason TEXT NOT NULL,
             product TEXT NOT NULL, coop_type TEXT NOT NULL, language TEXT NOT NULL,
             account_id TEXT NOT NULL, all_products INTEGER NOT NULL, note TEXT NOT NULL,
             until_date TEXT NOT NULL, expires_at TEXT, actor TEXT NOT NULL,
             version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             restored_at TEXT, restored_by TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS decision_channel ON decisions(channel_id);
            CREATE TABLE IF NOT EXISTS decision_audit (
             id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL,
             action TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL,
             before_json TEXT, after_json TEXT NOT NULL);
            """)
            connection.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _now(self, now=None):
        return _moment(now if now is not None else self.clock())

    def _record(self, row, now=None):
        if row is None:
            return None
        record = dict(row)
        record.pop("scope_key", None)
        record["all_products"] = bool(record["all_products"])
        record["until"] = record.pop("until_date")
        expired = bool(record["expires_at"] and self._now(now) >= _moment(record["expires_at"]))
        record["active"] = not record["restored_at"] and not expired
        record["state"] = "已恢复" if record["restored_at"] else "到期" if expired else "正常"
        record["reason_label"] = REASONS[record["reason"]]
        record["scope"] = {key: record[key] for key in ("product", "coop_type", "language", "account_id", "all_products")}
        record["schema_version"] = SCHEMA_VERSION
        return record

    def save(self, row, reason, product, coop_type="", language="", account_id="", note="",
             all_products=False, until="", actor="", expected_version=None):
        channel_id = channel_identity(row)
        if channel_id is None:
            raise DecisionError("缺少可靠频道身份或ID有冲突。请补充UC频道ID或规范 /channel/ 主页，不能仅用名称、邮箱或handle归类。")
        reason = _reason(reason)
        scope = _scope(reason, product, coop_type, language, account_id, all_products)
        until, actor, note = _text(until), _text(actor), _text(note)
        if len(actor) > 200 or len(note) > 4000 or "\0" in note + actor:
            raise DecisionError("备注或操作人字段过长或格式不正确。")
        now = self._now()
        expiry = None
        if until:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
                    raise ValueError()
                end = date.fromisoformat(until)
                if end < now.astimezone(BEIJING).date():
                    raise ValueError()
                expiry = _iso(datetime.combine(end + timedelta(days=1), time.min, tzinfo=BEIJING))
            except (ValueError, OverflowError):
                raise DecisionError("截止日期须为不早于今天的 YYYY-MM-DD；该日北京时间结束后自动恢复搜索可见。") from None
        if expected_version is not None and (isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0):
            raise DecisionError("记录版本不正确，请刷新后重试。")
        name = next((_text(row.get(field)) for field in ("频道名称", "name", "channel_name") if hasattr(row, "get") and _text(row.get(field))), "")
        values = dict(channel_id=channel_id, name=name, homepage="https://www.youtube.com/channel/" + channel_id,
                      reason=reason, note=note, until_date=until, expires_at=expiry, actor=actor,
                      restored_at=None, restored_by="", **scope)
        values["all_products"] = int(values["all_products"])
        scope_key = _scope_key(channel_id, reason, scope)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT * FROM decisions WHERE scope_key=?", (scope_key,)).fetchone()
            previous = dict(previous) if previous else None
            current_version = previous["version"] if previous else 0
            if expected_version is not None and expected_version != current_version:
                raise DecisionConflict("分类记录已有新修改，请刷新后确认；没有覆盖其他人的修改。")
            if previous and all(previous[key] == value for key, value in values.items()):
                return self._record(previous, now)
            record = dict(values, id=previous["id"] if previous else "decision_" + uuid4().hex,
                          scope_key=scope_key, version=current_version + 1,
                          created_at=previous["created_at"] if previous else _iso(now), updated_at=_iso(now))
            if previous:
                fields = [key for key in record if key != "id"]
                connection.execute("UPDATE decisions SET " + ",".join(field + "=?" for field in fields) + " WHERE id=?", [record[key] for key in fields] + [record["id"]])
            else:
                fields = list(record)
                connection.execute("INSERT INTO decisions(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")", [record[key] for key in fields])
            action = "reapply" if previous and previous["restored_at"] else "update" if previous else "add"
            self._audit(connection, record["id"], action, actor, now, previous, record)
            return self._record(record, now)

    add = save

    @staticmethod
    def _audit(connection, identity, action, actor, now, previous, record):
        connection.execute("INSERT INTO decision_audit(decision_id,action,actor,at,before_json,after_json) VALUES(?,?,?,?,?,?)",
                           (identity, action, actor, _iso(now), json.dumps(previous, ensure_ascii=False) if previous else None,
                            json.dumps(record, ensure_ascii=False)))

    def get_record(self, identity, now=None):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM decisions WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise DecisionError("分类记录不存在，请刷新后重试。")
        return self._record(row, now)

    def list_records(self, include_restored=False, *, product=None, reason=None, account_id=None,
                     channel_id=None, active_only=False, now=None):
        identity = channel_identity(channel_id) if channel_id is not None else None
        if channel_id is not None and identity is None:
            return []
        query = "SELECT * FROM decisions"
        parameters = ()
        if identity is not None:
            query += " WHERE channel_id=?"
            parameters = (identity,)
        query += " ORDER BY updated_at DESC,id"
        with self._connect() as connection:
            records = [self._record(row, now) for row in connection.execute(query, parameters)]
        if reason is not None:
            reason = _reason(reason)
        return [row for row in records if (include_restored or not row["restored_at"])
                and (product is None or row["all_products"] or row["product"].casefold() == _product(product).casefold())
                and (reason is None or row["reason"] == reason)
                and (account_id is None or row["account_id"] in ("", _text(account_id)))
                and (not active_only or row["active"])]

    def restore(self, identity, expected_version=None, actor=""):
        now, actor = self._now(), _text(actor)
        if len(actor) > 200 or "\0" in actor:
            raise DecisionError("操作人字段过长或格式不正确。")
        if expected_version is not None and (isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0):
            raise DecisionError("记录版本不正确，请刷新后重试。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT * FROM decisions WHERE id=?", (identity,)).fetchone()
            if previous is None:
                raise DecisionError("分类记录不存在，请刷新后重试。")
            previous = dict(previous)
            if expected_version is not None and expected_version != previous["version"]:
                raise DecisionConflict("分类记录已有新修改，请刷新后再恢复。")
            if previous["restored_at"]:
                return self._record(previous, now)
            record = dict(previous, restored_at=_iso(now), restored_by=actor, actor=actor,
                          updated_at=_iso(now), version=previous["version"] + 1)
            connection.execute("UPDATE decisions SET restored_at=?,restored_by=?,actor=?,updated_at=?,version=? WHERE id=?",
                               (record["restored_at"], actor, actor, record["updated_at"], record["version"], identity))
            self._audit(connection, identity, "restore", actor, now, previous, record)
            return self._record(record, now)

    def audit_history(self, identity):
        with self._connect() as connection:
            result = [dict(row) for row in connection.execute("SELECT * FROM decision_audit WHERE decision_id=? ORDER BY id", (identity,))]
        for row in result:
            before = row.pop("before_json")
            row["before"] = json.loads(before) if before else None
            row["after"] = json.loads(row.pop("after_json"))
        return result

    @staticmethod
    def _matches(record, product, form, language, account_id):
        return (record["active"] and (record["all_products"] or bool(product) and record["product"].casefold() == _product(product).casefold())
                and (not record["coop_type"] or bool(form) and record["coop_type"].casefold() == _name(form).casefold())
                and (not record["language"] or record["language"] == _language(language))
                and (not record["account_id"] or record["account_id"] == _text(account_id)))

    def active_for(self, row, product, form="", language="", account_id="", now=None, *, coop_type=None):
        identity = channel_identity(row)
        if identity is None:
            return []
        form = form if coop_type is None else coop_type
        return [record for record in self.list_records(channel_id=identity, active_only=True, now=now)
                if self._matches(record, product, form, language, account_id)]

    def matching_records(self, product, form="", language="", account_id="", now=None, *, coop_type=None):
        """Return applicable decisions for all channels using one database read.

        Missing scope values stay unknown; they do not broaden a scoped rule.
        The order and record dictionaries match ``list_records``.
        """
        form = form if coop_type is None else coop_type
        return [record for record in self.list_records(active_only=True, now=now)
                if self._matches(record, product, form, language, account_id)]

    def filter_frame(self, frame, context=None, include_processed=False, *, product=None, coop_type=None,
                     form=None, language=None, account_id=None, now=None):
        context = dict(context or {})
        product = context.get("product", "") if product is None else product
        form = (context.get("form", context.get("coop_type", "")) if form is None else form) if coop_type is None else coop_type
        language = context.get("language", "") if language is None else language
        account_id = context.get("account_id", "") if account_id is None else account_id
        by_channel = {}
        for record in self.matching_records(product, form, language, account_id, now):
            by_channel.setdefault(record["channel_id"], []).append(record)
        records = _rows(frame)
        hidden = [index for index, row in enumerate(records) if channel_identity(row) in by_channel]
        hidden_positions = set(hidden)
        shown = list(range(len(records))) if include_processed else [index for index in range(len(records)) if index not in hidden_positions]
        return _take(frame, shown), _take(frame, hidden)

    dedupe_channels = staticmethod(lambda frame: dedupe_channels(frame))


def _jsonable(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return _text(value) or None


def dedupe_channels(frame):
    """One row per proven UC channel, retaining every original row and video.

    The first row supplies representative scalars; empty fields may be filled
    from later rows. Conflicting values are preserved in 合并原始记录, never
    forced into numeric/email scalar columns. Unknown IDs remain separate.
    """
    records = _rows(frame)
    groups, order = {}, []
    for index, row in enumerate(records):
        identity = channel_identity(row)
        key = ("channel", identity) if identity else ("unknown-row", index)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((index, row))
    if len(order) == len(records):
        return frame.copy(deep=True) if hasattr(frame, "iloc") else deepcopy(records)
    combined, indices = [], []
    for key in order:
        members = groups[key]
        representative = deepcopy(dict(members[0][1]))
        indices.append(frame.index[members[0][0]] if hasattr(frame, "iloc") else members[0][0])
        if len(members) > 1:
            for _, row in members[1:]:
                for column, value in row.items():
                    if not _text(representative.get(column)) and _text(value):
                        representative[column] = deepcopy(value)
            originals = [_jsonable(dict(row)) for _, row in members]
            videos, sources, seen_video, seen_source = [], [], set(), set()
            for _, row in members:
                try:
                    previous_videos = json.loads(row.get("代表视频集合", "[]")) if isinstance(row.get("代表视频集合"), str) else row.get("代表视频集合", [])
                    previous_sources = json.loads(row.get("来源集合", "[]")) if isinstance(row.get("来源集合"), str) else row.get("来源集合", [])
                except (ValueError, TypeError):
                    previous_videos, previous_sources = [], []
                for video in previous_videos if isinstance(previous_videos, list) else []:
                    if not isinstance(video, dict):
                        continue
                    marker = json.dumps(_jsonable(video), ensure_ascii=False, sort_keys=True)
                    if marker not in seen_video:
                        seen_video.add(marker)
                        videos.append(_jsonable(video))
                for source in previous_sources if isinstance(previous_sources, list) else []:
                    source = _text(source)
                    if source and source not in seen_source:
                        seen_source.add(source)
                        sources.append(source)
                link = next((_text(row.get(field)) for field in ("代表视频链接", "视频链接", "video_url") if _text(row.get(field))), "")
                title = next((_text(row.get(field)) for field in ("代表视频标题", "视频标题", "title") if _text(row.get(field))), "")
                video = {"标题": title, "链接": link}
                video_key = json.dumps(video, ensure_ascii=False, sort_keys=True)
                if (link or title) and video_key not in seen_video:
                    seen_video.add(video_key)
                    videos.append(video)
                for field in ("来源", "命中关键词", "来源词"):
                    value = row.get(field)
                    values = value if isinstance(value, (list, tuple)) else _text(value).split(" | ")
                    for item in values:
                        item = _text(item)
                        if item and item not in seen_source:
                            seen_source.add(item)
                            sources.append(item)
            representative["合并原始记录"] = json.dumps(originals, ensure_ascii=False, allow_nan=False)
            representative["代表视频集合"] = json.dumps(videos, ensure_ascii=False)
            representative["来源集合"] = json.dumps(sources, ensure_ascii=False)
            representative["合并记录数"] = len(members)
        combined.append(representative)
    return frame.__class__(combined, index=indices) if hasattr(frame, "iloc") else combined


def get_store(root=None):
    key = str(Path(root if root is not None else Path.cwd()).expanduser().resolve())
    with _STORE_LOCK:
        if key not in _STORES:
            _STORES[key] = DecisionStore(root=key)
        return _STORES[key]
