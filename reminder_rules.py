"""Pure reminder selection rules; no network, file access, or third-party imports.

Input records use the workbench's existing Chinese column names. All outputs
are JSON-serializable. Dates are interpreted as calendar dates in the caller's
timezone; the caller supplies ``today`` when a particular timezone matters.

Only explicit pending delivery states can produce a deadline reminder. Neither
an existing video URL nor its original publication date proves this contract
has been delivered (the contract may be for a new link in an old video).
"""

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


PENDING_DELIVERY_STATES = frozenset(("待交付", "制作中", "待发布"))
FINISHED_DELIVERY_STATES = frozenset(("已交付", "已验收", "已取消", "终止合作"))
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _text(value: Any) -> str:
    """Normalize empty CSV cells without requiring pandas."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "null", "<na>") else text


def _first(row: Mapping[str, Any], *columns: str) -> str:
    for column in columns:
        value = _text(row.get(column))
        if value:
            return value
    return ""


def _hash_identity(parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mail_identity(row: Mapping[str, Any]) -> str:
    message_id = _text(row.get("邮件Message-ID"))
    if message_id:
        return "reply:" + _hash_identity(("message-id", message_id))
    return "reply:" + _hash_identity((
        "legacy", _text(row.get("邮箱")).lower(),
        _text(row.get("合作产品")), _text(row.get("发送时间")),
    ))


def _effect_identity(row: Mapping[str, Any], index: int) -> str:
    cooperation_id = _text(row.get("合作ID"))
    if cooperation_id:
        return "delivery:" + _hash_identity(("cooperation-id", cooperation_id))
    # Exclude mutable status, deadline, links, and performance metrics. The row
    # index distinguishes separate contracts even when all old fields match.
    # Persisting a cooperation ID is required for stability across row reorders.
    return "delivery:" + _hash_identity((
        "legacy-row", index, _first(row, "达人名", "频道名称"),
        _text(row.get("邮箱")).lower(), _text(row.get("合作产品")),
        _text(row.get("合作类型")), _text(row.get("合作日期")),
        _text(row.get("记录时间")),
    ))


def _base(row: Mapping[str, Any], reminder_id: str) -> Dict[str, Any]:
    return {
        "id": reminder_id,
        "name": _first(row, "频道名称", "达人名"),
        "email": _text(row.get("邮箱")),
        "product": _text(row.get("合作产品")) or "待补充",
        "homepage": _first(row, "主页链接", "频道链接"),
    }


def _reply_time(row: Mapping[str, Any]) -> str:
    return _first(row, "最近回信时间", "最后回信时间", "回信时间")


def _timestamp_order(value: str) -> Tuple[int, str]:
    """Sort common ISO timestamps; unknown formats remain deterministic."""
    if not value:
        return (0, "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Workbench CSV times are local and usually naive. ISO ordering is
        # sufficient to choose between duplicate rows from the same log.
        return (2, parsed.isoformat())
    except ValueError:
        return (1, value)


def _collect_replies(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    threads: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        if _text(row.get("状态")) not in ("", "成功"):
            continue
        if not _text(row.get("回信状态")).startswith("已回信待处理"):
            continue
        threads.setdefault(_mail_identity(row), []).append(row)

    reminders = []
    for reminder_id, duplicates in threads.items():
        # Prefer a row with the newest recorded reply time, then newest send
        # time, then the last duplicate. Missing display fields can still be
        # supplied by another copy of the same thread.
        ordered = sorted(enumerate(duplicates), key=lambda item: (
            _timestamp_order(_reply_time(item[1])),
            _timestamp_order(_text(item[1].get("发送时间"))), item[0],
        ), reverse=True)
        merged: Dict[str, Any] = {}
        for _, row in ordered:
            for column in (
                "频道名称", "达人名", "邮箱", "合作产品", "主页链接", "频道链接",
                "回信状态", "对方报价", "最近回信时间", "最后回信时间", "回信时间",
            ):
                if not _text(merged.get(column)) and _text(row.get(column)):
                    merged[column] = row[column]
        item = _base(merged, reminder_id)
        detail = _text(merged.get("回信状态"))
        quote = _text(merged.get("对方报价"))
        if quote:
            detail += "；对方报价：" + quote
        item.update(kind="reply", detail=detail)
        last_reply = _reply_time(merged)
        if last_reply:
            item["last_reply"] = last_reply
        reminders.append(item)
    return reminders


def _parse_due(raw: str) -> Optional[date]:
    if not _DATE_PATTERN.fullmatch(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def collect_reminders(
    mail_rows: Iterable[Mapping[str, Any]],
    effect_rows: Iterable[Mapping[str, Any]],
    today: Optional[date] = None,
    lead_days: int = 3,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return reply work, approaching/overdue contracts, and incomplete data.

    Reply identities use Message-ID, or email + product + send time for legacy
    rows. Duplicate copies of a pending reply in the same thread are merged;
    separate invitations to the same email remain separate.

    ``deliveries`` kinds are ``upcoming`` (including today and the inclusive
    lead-days boundary) or ``overdue``. ``missing_due`` holds ``missing_due``
    items for unknown status/missing dates, and ``invalid_due`` for a pending
    contract with a malformed date. Finished/cancelled contracts are omitted.
    Original publication dates and cooperation dates are never used as due
    dates or as automatic evidence of delivery. Input rows are never modified.
    """
    if today is None:
        today = date.today()
    if isinstance(today, datetime):
        today = today.date()
    if not isinstance(today, date):
        raise TypeError("today must be a datetime.date")
    if isinstance(lead_days, bool) or not isinstance(lead_days, int) or lead_days < 0:
        raise ValueError("lead_days must be a non-negative integer")

    output: Dict[str, List[Dict[str, Any]]] = {
        "replies": _collect_replies(mail_rows), "deliveries": [], "missing_due": [],
    }
    last_upcoming_day = today + timedelta(days=lead_days)
    for index, row in enumerate(effect_rows):
        status = _text(row.get("交付状态"))
        if status in FINISHED_DELIVERY_STATES:
            continue
        item = _base(row, _effect_identity(row, index))
        item.update(
            status=status,
            due_date=_text(row.get("约定交付日期")),
            video_url=_text(row.get("视频链接")),
        )
        if status not in PENDING_DELIVERY_STATES:
            item.update(kind="missing_due", detail="需确认交付状态")
            output["missing_due"].append(item)
            continue
        if not item["due_date"]:
            item.update(kind="missing_due", detail="需补充约定交付日期")
            output["missing_due"].append(item)
            continue
        due = _parse_due(item["due_date"])
        if due is None:
            item.update(kind="invalid_due", detail="约定交付日期格式错误，需填写 YYYY-MM-DD")
            output["missing_due"].append(item)
            continue
        days_remaining = (due - today).days
        item["days_remaining"] = days_remaining
        if due < today:
            item.update(
                kind="overdue",
                detail="约定交付日期：%s（已逾期 %s 天）" % (due.isoformat(), -days_remaining),
            )
            output["deliveries"].append(item)
        elif due <= last_upcoming_day:
            suffix = "今日到期" if days_remaining == 0 else "%s 天后到期" % days_remaining
            item.update(kind="upcoming", detail="约定交付日期：%s（%s）" % (due.isoformat(), suffix))
            output["deliveries"].append(item)
    return output
