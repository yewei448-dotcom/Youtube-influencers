"""Concurrent-safe contact imports and optimistic, field-level editor saves.

Reading never migrates the CSV.  ``load_contacts`` adds deterministic IDs only
to its returned copy; a subsequent save persists them with the real change.
This module has no mail, network, configuration, or Streamlit dependency.
"""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pandas as pd

from workbench_io import csv_transaction, write_csv_atomic


CONTACT_ID = "联系人ID"
DELETE_FIELD = "删除"
CHANNEL_ID = "频道ID"
SOURCE_FIELDS = {"来源视频", "来源词", "监测关联", "监测产品"}
IDENTITY_FIELDS = {CHANNEL_ID, "主页链接", "合作产品", "合作类型"}
PROFILE_FIELDS = {CHANNEL_ID, "主页链接", "频道名称", "邮箱", "合作类型"}
_CHANNEL_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_TABS = {"videos", "shorts", "streams", "featured", "community", "playlists", "about"}
_ALIASES = {
    CHANNEL_ID: (CHANNEL_ID, "channel_id", "channelId"),
    "主页链接": ("主页链接", "homepage", "channel_url", "主页", "链接"),
    "频道名称": ("频道名称", "channel_name", "name", "达人名称", "达人名"),
    "邮箱": ("邮箱", "email", "联系方式"),
    "合作产品": ("合作产品", "product"),
    "合作类型": ("合作类型", "coop_type"),
}
_ALIAS_NAMES = {name for names in _ALIASES.values() for name in names}


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple, dict, set)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _records(rows):
    if rows is None:
        return []
    if isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")
    if isinstance(rows, dict):
        rows = [rows]
    return [dict(row) for row in rows]


def _value(row, field):
    for name in _ALIASES.get(field, (field,)):
        value = _text(row.get(name))
        if value:
            return value
    return ""


def canonical_channel(value):
    """Return (canonical URL, public channel ID), without resolving handles."""
    text = _text(value)
    if _CHANNEL_RE.fullmatch(text):
        return "https://www.youtube.com/channel/" + text, text
    if text.startswith("@") and re.fullmatch(r"@[^\s/?#]+", text):
        return "https://www.youtube.com/" + text.casefold(), ""
    if not re.match(r"^https?://", text, re.I):
        if text.lower().startswith(("youtube.com/", "www.youtube.com/", "m.youtube.com/")):
            text = "https://" + text
        else:
            return "", ""
    try:
        url = urlsplit(text)
        if url.scheme.lower() not in {"http", "https"} or url.username or url.password:
            return "", ""
        if url.hostname is None or url.hostname.lower() not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            return "", ""
        if url.port not in (None, 80, 443):
            return "", ""
        parts = [unquote(part) for part in url.path.split("/") if part]
    except (ValueError, UnicodeError):
        return "", ""
    if any(re.search(r"[\s/?#]", part) for part in parts):
        return "", ""
    if len(parts) > 1 and parts[-1].lower() in _TABS:
        parts.pop()
    if len(parts) == 2 and parts[0].lower() == "channel" and _CHANNEL_RE.fullmatch(parts[1]):
        return "https://www.youtube.com/channel/" + parts[1], parts[1]
    if len(parts) == 1 and parts[0].startswith("@") and len(parts[0]) > 1:
        return "https://www.youtube.com/" + parts[0].casefold(), ""
    if len(parts) == 2 and parts[0].lower() in {"c", "user"} and parts[1]:
        return "https://www.youtube.com/" + parts[0].lower() + "/" + parts[1].casefold(), ""
    return "", ""


def _channel(row):
    channel_id = _value(row, CHANNEL_ID)
    if not _CHANNEL_RE.fullmatch(channel_id):
        channel_id = ""
    homepage, url_id = canonical_channel(_value(row, "主页链接"))
    if not channel_id:
        channel_id = url_id
    return channel_id, homepage


def _conflicting_channel_identity(row):
    explicit = _value(row, CHANNEL_ID)
    _, url_id = canonical_channel(_value(row, "主页链接"))
    return bool(_CHANNEL_RE.fullmatch(explicit) and url_id and explicit != url_id)


def _product(value):
    value = " ".join(_text(value).split()).casefold()
    return "reiboot" if value == "reiboot for ios" else value


def _kind(value):
    return " ".join(_text(value).split()).casefold()


def _same_channel(left, right):
    left_id, left_url = _channel(left)
    right_id, right_url = _channel(right)
    if left_id and right_id:
        return left_id == right_id
    return bool(left_url and right_url and left_url == right_url)


def _identity_seed(row):
    channel_id, homepage = _channel(row)
    if channel_id or homepage:
        who = channel_id or homepage
    else:
        # Preserve legacy rows with incomplete channel data as separate records.
        # These IDs are stable for the same data; imports never match by name/email.
        who = {k: _text(v) for k, v in row.items() if k not in {CONTACT_ID, DELETE_FIELD}}
    return json.dumps([who, _product(_value(row, "合作产品")),
                       _kind(_value(row, "合作类型"))], ensure_ascii=False, sort_keys=True)


def ensure_contact_ids(frame):
    """Return a copy with stable IDs; never read or write any file."""
    is_frame = isinstance(frame, pd.DataFrame)
    records = _records(frame)
    seen_ids = set()
    occurrences = Counter()
    for row in records:
        seed = _identity_seed(row)
        ordinal = occurrences[seed]
        occurrences[seed] += 1
        contact_id = _text(row.get(CONTACT_ID))
        if not contact_id or contact_id in seen_ids:
            salt = ordinal
            while True:
                digest = hashlib.sha256((seed + "|" + str(salt)).encode("utf-8")).hexdigest()[:28]
                contact_id = "contact_" + digest
                if contact_id not in seen_ids:
                    break
                salt += 1
        row[CONTACT_ID] = contact_id
        seen_ids.add(contact_id)
    result = pd.DataFrame(records)
    if not records and is_frame:
        result = frame.copy()
    if CONTACT_ID not in result.columns:
        result[CONTACT_ID] = pd.Series(dtype=str)
    return result


def load_contacts(path):
    """Read contacts for display/editing without persisting an ID migration."""
    return ensure_contact_ids(_read_contacts(path))


def _read_contacts(path):
    # A quoted business value such as "NA" or "NULL" is text, not missing data.
    # Keep those exact strings when a different column is saved later.  Use the
    # shared workbench lock/atomic writer for every mutation below.
    try:
        return pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def _normalise_incoming(source):
    row = {key: value for key, value in source.items()
           if key not in _ALIAS_NAMES and key not in {CONTACT_ID, DELETE_FIELD}}
    for field in _ALIASES:
        value = _value(source, field)
        if value:
            row[field] = value
    channel_id, homepage = _channel(row)
    if channel_id:
        row[CHANNEL_ID] = channel_id
    if homepage:
        row["主页链接"] = homepage
    elif channel_id:
        row["主页链接"] = "https://www.youtube.com/channel/" + channel_id
    original_product = _value(source, "合作产品")
    if _product(original_product) == "reiboot" and original_product.casefold() == "reiboot for ios":
        row["合作产品"] = "ReiBoot"
        row["监测产品"] = _union_values(row.get("监测产品"), original_product)
    return {key: _text(value) for key, value in row.items()}


def _items(value):
    if value is None or (not isinstance(value, (dict, list, tuple, set)) and not _text(value)):
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, dict):
        return [value]
    value = _text(value)
    try:
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return decoded
        if isinstance(decoded, dict):
            return [decoded]
    except (ValueError, TypeError):
        pass
    return [value]


def _union_values(old, new):
    result, seen = [], set()
    for value in _items(old) + _items(new):
        if isinstance(value, str):
            value = value.strip()
        if value is None or value == "":
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str) if result else ""


def _summary():
    return {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0,
            "skipped": 0, "conflicts": 0, "total": 0,
            "contact_ids": [], "errors": [], "conflict_details": []}


def _matching_index(records, row):
    candidates = [i for i, current in enumerate(records)
                  if _same_channel(current, row)
                  and _product(_value(current, "合作产品")) == _product(_value(row, "合作产品"))]
    kind = _kind(_value(row, "合作类型"))
    exact = [i for i in candidates if _kind(_value(records[i], "合作类型")) == kind]
    if kind:
        if len(exact) == 1:
            return exact[0], ""
        if len(exact) > 1:
            return None, "existing_duplicate_identity"
        empty = [i for i in candidates if not _kind(_value(records[i], "合作类型"))]
        if len(empty) == 1:
            return empty[0], ""
        if len(empty) > 1:
            return None, "ambiguous_cooperation_type"
        return None, ""
    # A default/blank kind is not a new cooperation.  Link a unique existing
    # kind, but do not guess between several real cooperation records.
    if len(candidates) == 1:
        return candidates[0], ""
    if len(candidates) > 1:
        return None, "ambiguous_cooperation_type"
    return None, ""


def _append_or_merge(records, source, summary):
    row = _normalise_incoming(source)
    if _conflicting_channel_identity(row):
        summary["skipped"] += 1
        summary["errors"].append({"code": "conflicting_channel_identity", "name": _value(row, "频道名称")})
        return False
    if not any(_channel(row)):
        summary["skipped"] += 1
        summary["errors"].append({"code": "missing_channel_identity", "name": _value(row, "频道名称")})
        return False
    index, reason = _matching_index(records, row)
    if reason:
        summary["skipped"] += 1
        summary["errors"].append({"code": reason, "name": _value(row, "频道名称")})
        return False
    if index is None:
        email = _value(row, "邮箱")
        row.setdefault("状态", "待建联" if email else "待补邮箱")
        row.setdefault("发送状态", "待发送" if email else "待补邮箱")
        if not email and _text(row.get("发送状态")) in {"", "待发送"}:
            row["发送状态"] = "待补邮箱"
        for field in SOURCE_FIELDS:
            if field in row:
                row[field] = _union_values(None, row[field])
        row = ensure_contact_ids(pd.DataFrame(records + [row])).to_dict("records")[-1]
        records.append(row)
        summary["added"] += 1
        summary["contact_ids"].append(row[CONTACT_ID])
        return True
    current = records[index]
    changed = False
    for field in PROFILE_FIELDS:
        if not _value(current, field) and _text(row.get(field)):
            current[field] = row[field]
            changed = True
    for field in SOURCE_FIELDS:
        if field in row:
            combined = _union_values(current.get(field), row[field])
            if combined != _text(current.get(field)):
                current[field] = combined
                changed = True
    summary["updated" if changed else "unchanged"] += 1
    summary["contact_ids"].append(current[CONTACT_ID])
    return changed


def _save(records, path):
    frame = pd.DataFrame(records)
    if not records:
        frame = pd.DataFrame(columns=[CONTACT_ID])
    if DELETE_FIELD in frame.columns:
        frame = frame.drop(columns=[DELETE_FIELD])
    write_csv_atomic(frame, path)


def merge_contacts(path, rows):
    """Import contacts, preserving all existing business fields and nonblank email.

    Appends only source evidence; only missing profile fields may be filled.
    ``contact_ids`` contains IDs for added or successfully associated inputs.
    Ambiguous identities are skipped and explained in ``errors``.
    """
    incoming = _records(rows)
    summary = _summary()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with csv_transaction(path):
        raw = _read_contacts(path)
        records = ensure_contact_ids(raw).to_dict("records")
        changed = False
        for row in incoming:
            changed = _append_or_merge(records, row, summary) or changed
        # An explicit successful import is a save operation and can migrate IDs;
        # invalid/empty input alone must not rewrite the original file.
        migrating = bool(summary["contact_ids"]) and (
            CONTACT_ID not in raw.columns
            or any(not _text(value) for value in raw.get(CONTACT_ID, []))
            or raw[CONTACT_ID].duplicated().any())
        if changed or migrating:
            _save(records, path)
        summary["total"] = len(records)
    return summary


def _checked(value):
    if isinstance(value, bool):
        return value
    return _text(value).casefold() in {"true", "1", "yes"}


def _conflict(summary, contact_id, field, reason):
    summary["conflicts"] += 1
    summary["conflict_details"].append({"contact_id": contact_id, "field": field, "reason": reason})


def persist_editor_changes(path, before, after):
    """Apply only explicit editor changes against the latest CSV under one lock.

    Keep CONTACT_ID in both snapshots, hidden/read-only in the editor.  A row
    marked 删除=True is an explicit deletion; omitted rows are *not* deletions.
    A concurrent change to the same field wins and is reported as a conflict.
    New fields/rows and concurrent unseen rows are preserved.  Evidence fields
    remain append-only.  No-op reruns neither write the CSV nor migrate IDs.
    """
    baseline = ensure_contact_ids(before).to_dict("records")
    edited = _records(after)
    baseline_by_id = {row[CONTACT_ID]: row for row in baseline}
    summary = _summary()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with csv_transaction(path):
        records = ensure_contact_ids(_read_contacts(path)).to_dict("records")
        seen_after = set()
        changed = False
        for row in edited:
            contact_id = _text(row.get(CONTACT_ID))
            if not contact_id:
                if _checked(row.get(DELETE_FIELD)):
                    summary["skipped"] += 1
                    continue
                changed = _append_or_merge(records, row, summary) or changed
                continue
            if contact_id in seen_after:
                _conflict(summary, contact_id, CONTACT_ID, "duplicate_editor_id")
                continue
            seen_after.add(contact_id)
            original = baseline_by_id.get(contact_id)
            if original is None:
                _conflict(summary, contact_id, CONTACT_ID, "id_not_in_editor_baseline")
                continue
            index = next((i for i, current in enumerate(records) if current[CONTACT_ID] == contact_id), None)
            fields = [field for field in row if field not in {CONTACT_ID, DELETE_FIELD}
                      and _text(row.get(field)) != _text(original.get(field))]
            deleting = _checked(row.get(DELETE_FIELD))
            if index is None:
                if fields or deleting:
                    _conflict(summary, contact_id, CONTACT_ID, "contact_removed_since_edit")
                else:
                    summary["unchanged"] += 1
                continue
            current = records[index]
            if deleting:
                concurrently_changed = any(
                    _text(current.get(field)) != _text(value)
                    for field, value in original.items()
                    if field not in {CONTACT_ID, DELETE_FIELD})
                if concurrently_changed:
                    _conflict(summary, contact_id, DELETE_FIELD, "row_changed_before_delete")
                    continue
                records.pop(index)
                summary["deleted"] += 1
                changed = True
                continue
            if not fields:
                summary["unchanged"] += 1
                continue
            updated = dict(current)
            for field in fields:
                wanted = _text(row.get(field))
                if field in SOURCE_FIELDS:
                    updated[field] = _union_values(current.get(field), wanted)
                elif _text(current.get(field)) in {_text(original.get(field)), wanted}:
                    updated[field] = wanted
                else:
                    _conflict(summary, contact_id, field, "field_changed_since_edit")
            identity_changed = any(_text(updated.get(field)) != _text(current.get(field))
                                   for field in IDENTITY_FIELDS)
            if identity_changed:
                conflicting_id = _conflicting_channel_identity(updated)
                collision = any(other[CONTACT_ID] != contact_id and _same_channel(other, updated)
                                and _product(_value(other, "合作产品")) == _product(_value(updated, "合作产品"))
                                and _kind(_value(other, "合作类型")) == _kind(_value(updated, "合作类型"))
                                for other in records)
                if collision or conflicting_id:
                    for field in IDENTITY_FIELDS:
                        if field in updated or field in current:
                            updated[field] = current.get(field, "")
                    _conflict(summary, contact_id, "身份",
                              "conflicting_channel_identity" if conflicting_id else "identity_already_exists")
            if any(_text(updated.get(field)) != _text(current.get(field)) for field in updated):
                records[index] = updated
                summary["updated"] += 1
                summary["contact_ids"].append(contact_id)
                changed = True
            else:
                summary["unchanged"] += 1
        if changed:
            _save(records, path)
        summary["total"] = len(records)
    return summary


def save_full(path, after, before=None):
    """Compatibility wrapper requiring an explicit editor baseline."""
    if before is None:
        raise ValueError("save_full requires before; blind full-table replacement is disabled")
    return persist_editor_changes(path, before, after)
