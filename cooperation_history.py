"""Read-only local cooperation history, with conservative creator joins.

Only the caller-supplied effect table establishes a cooperation. Contact,
discovery and invitation tables may establish an unambiguous video -> UC ID
mapping or supply contact provenance; they never establish a deal or a price.
No files, credentials, network clients, or mail-account state are accessed.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re
from urllib.parse import parse_qs, urlsplit


API_STATUS = "尚未接入外部 API，当前仅本地合作记录。"
SOURCE_NAME = "合作效果.csv"
_CID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_VID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_NUMBER = re.compile(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?\Z")
_CHANNEL_FIELDS = ("频道ID", "channel_id", "channelId", "主页链接", "频道链接", "homepage", "channel_url")
_VIDEO_FIELDS = ("视频链接", "代表视频", "代表视频链接", "来源视频", "代表视频集合", "来源视频集合", "videos", "video_url", "video_id", "videoId")
_VIDEO_VALUE_FIELDS = ("链接", "视频链接", "url", "link", "video_url", "video_id", "videoId")


def text(value):
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value.lower() in ("nan", "none", "null", "<na>", "nat") else value


def _first(row, *keys):
    return next((text(row.get(key)) for key in keys if text(row.get(key))), "")


def _rows(frame):
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return deepcopy(frame.to_dict("records"))
    return [deepcopy(dict(row)) for row in frame]


def channel_id(value):
    value = text(value)
    if _CID.fullmatch(value):
        return value
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            return ""
        match = re.fullmatch(r"/channel/(UC[A-Za-z0-9_-]{22})(?:/(?:videos|about|featured|shorts|streams))?/?", parsed.path)
        return match.group(1) if match else ""
    except ValueError:
        return ""


def video_id(value):
    value = text(value)
    if _VID.fullmatch(value):
        return value
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https"):
            return ""
        if parsed.hostname in ("youtu.be", "www.youtu.be"):
            candidate = parsed.path.strip("/")
        elif parsed.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            candidate = parse_qs(parsed.query).get("v", [""])[0] if parsed.path == "/watch" else ""
            if parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
                candidate = parsed.path.split("/")[2]
        else:
            return ""
        return candidate if _VID.fullmatch(candidate) else ""
    except (ValueError, IndexError):
        return ""


def _values(value, depth=0):
    """Read explicit video links/IDs from legacy strings or structured sets.

    Title/name fields are deliberately excluded even when they resemble a URL
    or ID. Mapping-shaped sources are not searched for arbitrary strings.
    """
    if depth > 8:
        return []
    if isinstance(value, dict):
        return [item for key in _VIDEO_VALUE_FIELDS if key in value
                for item in _values(value[key], depth + 1)]
    if isinstance(value, (list, tuple, set)):
        return [item for member in value for item in _values(member, depth + 1)]
    raw = text(value)
    if raw.startswith(("[", "{")):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (list, dict)):
                return _values(parsed, depth + 1)
        except (ValueError, TypeError):
            pass
    return re.split(r"[\n;；]+", raw) if raw else []


def _channels(row):
    return {cid for key in _CHANNEL_FIELDS for cid in [channel_id(row.get(key))] if cid}


def _videos(row):
    return {vid for key in _VIDEO_FIELDS for value in _values(row.get(key)) for vid in [video_id(value)] if vid}


def _amount(value):
    if isinstance(value, bool):
        return None
    raw = text(value)
    if not _NUMBER.fullmatch(raw):
        return None
    number = float(raw.replace(",", ""))
    return number if math.isfinite(number) and number >= 0 else None


def _date_key(value):
    raw = text(value)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OSError):
        return float("-inf")


def _source_contact(row, source, channel=""):
    email = _first(row, "邮箱", "email", "联系方式")
    if not email:
        return None
    return {"email": email, "source": _first(row, "邮箱来源", "联系方式来源", "email_source") or source,
            "recorded_at": _first(row, "邮箱采集时间", "采集时间", "记录时间", "更新时间", "updated_at"),
            "channel_id": channel}


class CooperationHistory:
    def __init__(self, effect_frame, history_frame=None, library_frame=None):
        effects = _rows(effect_frame)
        self._sources = [(row, "达人资源") for row in _rows(history_frame)]
        self._sources += [(row, "建联库") for row in _rows(library_frame)]
        self._video_channels = {}
        for row in effects + [row for row, _ in self._sources]:
            channels = _channels(row)
            for vid in _videos(row):
                self._video_channels.setdefault(vid, set()).update(channels)
        self.records = []
        for position, raw in enumerate(effects):
            explicit = _channels(raw)
            vids = _videos(raw)
            mapped = set().union(*(self._video_channels.get(vid, set()) for vid in vids)) if vids else set()
            ambiguous = len(explicit) > 1 or len(mapped) > 1
            cid = next(iter(explicit)) if len(explicit) == 1 else ""
            basis = "原记录频道 ID" if cid else ""
            if not cid and not ambiguous and len(mapped) == 1:
                cid = next(iter(mapped))
                basis = "精确视频与唯一频道 ID 映射"
            amount = _amount(raw.get("合作金额", raw.get("成交价")))
            currency = _first(raw, "币种", "合作币种", "货币", "currency")
            inferred = not currency
            currency = currency.upper() if currency else "USD"
            digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
            record = {"record_id": "local:%s:%s" % (position + 2, digest), "source": SOURCE_NAME,
                      "source_row": position + 2, "raw": deepcopy(raw),
                      "cooperation_id": _first(raw, "合作ID", "合作 ID", "合作记录ID", "cooperation_id", "deal_id"),
                      "name": _first(raw, "达人名", "达人名称", "name", "频道名称"),
                      "email": _first(raw, "邮箱", "email", "联系方式"),
                      "product": _first(raw, "合作产品", "产品", "product"),
                      "form": _first(raw, "合作类型", "合作形式", "coop_type"),
                      "date": _first(raw, "合作日期", "成交日期", "date"),
                      "recorded_at": _first(raw, "记录时间", "采集时间", "updated_at"),
                      "amount": amount, "amount_raw": text(raw.get("合作金额", raw.get("成交价"))),
                      "currency": currency, "currency_inferred": inferred,
                      "currency_basis": "历史按 USD 旧表口径（原记录未填币种）" if inferred else "原记录币种",
                      "channel_id": cid, "explicit_channel_id": next(iter(explicit)) if len(explicit) == 1 else "",
                      "video_ids": sorted(vids), "video_url": "https://www.youtube.com/watch?v=" + sorted(vids)[0] if vids else "",
                      "homepage": "https://www.youtube.com/channel/" + cid if cid else "",
                      "match_basis": basis, "ambiguous_identity": ambiguous,
                      "contact_source": _first(raw, "邮箱来源", "联系方式来源", "email_source") or "%s 第%s行" % (SOURCE_NAME, position + 2),
                      "duplicate_note": ""}
            self.records.append(record)
        groups = {}
        for record in self.records:
            if record["cooperation_id"]:
                key = ("id", record["cooperation_id"])
            elif record["video_ids"]:
                key = ("legacy", tuple(record["video_ids"]), record["date"], record["email"].lower(), record["product"], record["form"])
            else:
                continue
            groups.setdefault(key, []).append(record)
        for group in groups.values():
            if len(group) > 1:
                for record in group:
                    record["duplicate_note"] = "相同合作 ID 有%s行，保留原始行" % len(group) if record["cooperation_id"] else "相同视频/日期/邮箱/产品/形式有%s行，未自动去重" % len(group)

    def products(self):
        return sorted({record["product"] for record in self.records if record["product"]})

    def filter_records(self, product="", search=""):
        query = text(search).casefold()
        return deepcopy([record for record in self.records
                         if (not product or record["product"] == product)
                         and (not query or query in " ".join([record["name"], record["email"], record["channel_id"], record["homepage"]]).casefold())])

    def channel_summary(self, row, product=None, form=None):
        row = dict(row)
        product = _first(row, "合作产品", "product") if product is None else text(product)
        form = _first(row, "合作类型", "合作形式", "coop_type") if form is None else text(form)
        explicit = _channels(row)
        vids = _videos(row)
        mapped = set().union(*(self._video_channels.get(vid, set()) for vid in vids)) if vids else set()
        ambiguous = len(explicit) > 1 or (not explicit and len(mapped) > 1)
        cid = next(iter(explicit)) if len(explicit) == 1 else (next(iter(mapped)) if len(mapped) == 1 and not explicit else "")
        selected = []
        bases = set()
        if not ambiguous:
            for record in self.records:
                if product and record["product"] != product:
                    continue
                basis = ""
                if cid and record["channel_id"] == cid:
                    basis = "频道 ID 精确匹配" if explicit else "精确视频与唯一频道 ID 映射"
                elif record["channel_id"] and cid and record["channel_id"] != cid:
                    continue
                elif not record["ambiguous_identity"] and vids.intersection(record["video_ids"]):
                    common = vids.intersection(record["video_ids"])
                    if all(len(self._video_channels.get(vid, set())) <= 1 and
                           (not cid or not self._video_channels.get(vid) or self._video_channels[vid] == {cid}) for vid in common):
                        basis = "视频 ID 精确匹配（仅归属该视频的记录）"
                if basis:
                    selected.append(deepcopy(record))
                    bases.add(basis)
        selected.sort(key=lambda record: (_date_key(record["date"]), _date_key(record["recorded_at"]), record["source_row"]), reverse=True)
        prices = [deepcopy(record) for record in selected if product and form and record["product"] == product
                  and record["form"] == form and record["amount"] is not None and record["amount"] > 0]
        contacts = []
        current = _source_contact(row, "当前记录", cid)
        if current:
            contacts.append(current)
        for source_row, source in self._sources:
            channels = _channels(source_row)
            if cid and channels == {cid}:
                contact = _source_contact(source_row, source, cid)
                if contact:
                    contacts.append(contact)
        for record in selected:
            contact = _source_contact(record["raw"], record["contact_source"], record["channel_id"])
            if contact:
                contacts.append(contact)
        unique_contacts = []
        seen = set()
        for contact in contacts:
            identity = (contact["email"].casefold(), contact["source"], contact["recorded_at"])
            if identity not in seen:
                seen.add(identity)
                unique_contacts.append(contact)
        ids = {record["cooperation_id"] for record in selected if record["cooperation_id"]}
        warnings = ["本地记录逐行保留；无合作 ID 的旧行未自动去重，记录数不等同于核实后的合作次数。"]
        if ambiguous:
            warnings.append("当前记录的频道或视频归属有歧义，未关联历史记录。")
        if any(record["duplicate_note"] for record in selected):
            warnings.append("存在重复候选行或重复合作 ID，请按来源行核对，未合并或猜测成交金额。")
        if not selected:
            warnings.append("未找到可可靠关联的本地记录；这不表示该达人从未合作。")
        return {"record_count": len(selected), "identified_cooperation_count": len(ids),
                "unidentified_record_count": sum(not record["cooperation_id"] for record in selected),
                "records": selected, "reference_prices": prices, "match_basis": sorted(bases),
                "channel_id": cid, "product": product, "form": form, "contact_sources": unique_contacts,
                "warnings": warnings, "api_status": API_STATUS}


def build_history(effect_frame, history_frame=None, library_frame=None):
    return CooperationHistory(effect_frame, history_frame, library_frame)


def channel_summary(row, product=None, form=None, effect_frame=None, history_frame=None, library_frame=None, index=None):
    history = index if index is not None else build_history(effect_frame, history_frame, library_frame)
    return history.channel_summary(row, product, form)


def record_display(record):
    amount = record["amount"]
    amount_text = ("%g" % amount) if amount is not None else (record["amount_raw"] or "未记录")
    if amount == 0:
        amount_text += "（不作为参考价）"
    return {"达人": record["name"] or "未填写", "产品": record["product"] or "未填写", "合作形式": record["form"] or "未填写",
            "合作日期": record["date"] or "未记录", "成交金额（原记录）": amount_text,
            "币种/口径": record["currency_basis"] if record["currency_inferred"] else record["currency"],
            "联系方式": record["email"] or "未记录", "联系方式来源": record["contact_source"],
            "记录时间": record["recorded_at"] or "未记录", "主页": record["homepage"], "视频": record["video_url"],
            "合作ID": record["cooperation_id"] or "未记录", "来源行": "%s 第%s行" % (record["source"], record["source_row"]),
            "核对说明": record["duplicate_note"] or ("频道归属有歧义" if record["ambiguous_identity"] else "")}
