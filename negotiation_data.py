"""Pure, offline preparation of negotiation data (Python 3.9, stdlib only).

DataFrame inputs are duck-typed through to_dict('records'); lists of mappings
are also accepted. The module never imports the workbench, sends messages,
looks up channels, parses reply prose for prices, or modifies its inputs.
"""

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?\Z")
_REJECTED = ("已拒绝", "拒绝", "已取消", "取消", "终止合作", "已终止", "不合作")
_COUNTRIES = {
    "us": "US", "usa": "US", "united states": "US", "united states of america": "US", "美国": "US",
    "uk": "GB", "gb": "GB", "united kingdom": "GB", "英国": "GB",
    "in": "IN", "india": "IN", "印度": "IN", "pk": "PK", "pakistan": "PK", "巴基斯坦": "PK",
    "es": "ES", "spain": "ES", "西班牙": "ES", "br": "BR", "brazil": "BR", "巴西": "BR",
    "de": "DE", "germany": "DE", "德国": "DE", "fr": "FR", "france": "FR", "法国": "FR",
    "jp": "JP", "japan": "JP", "日本": "JP", "tw": "TW", "taiwan": "TW", "台湾": "TW",
    "hk": "HK", "hong kong": "HK", "香港": "HK",
}


def _text(value):
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value.lower() in ("nan", "none", "null", "<na>") else value


def _first(row, *fields):
    for field in fields:
        value = _text(row.get(field))
        if value:
            return value
    return ""


def _records(frame):
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return [dict(row) for row in frame.to_dict("records")]
    return [dict(row) for row in frame]


def _email(value):
    value = _text(value)
    addresses = {match.lower() for match in _EMAIL.findall(value)}
    if len(addresses) > 1:
        return ""  # Multiple recipients are not a unique creator identity.
    return next(iter(addresses)) if addresses else value.lower()


def _numeric(value):
    if isinstance(value, bool):
        return None
    raw = _text(value)
    if not raw or not _NUMBER.fullmatch(raw):
        return None
    number = float(raw.replace(",", ""))
    return number if math.isfinite(number) else None


def _country(value):
    value = _text(value)
    if not value:
        return None
    known = _COUNTRIES.get(value.lower())
    if known:
        return known
    # Unknown countries remain unknown to the pricing engine. In particular,
    # an English-language channel is not evidence that it is in the US.
    return value.upper() if re.fullmatch(r"[a-zA-Z]{2}", value) else value


def _channel(row):
    raw = _first(row, "频道ID", "channel_id")
    if re.fullmatch(r"UC[\w-]{22}", raw):
        return ("id", raw)
    raw = _first(row, "主页链接", "频道链接")
    if re.fullmatch(r"UC[\w-]{22}", raw):
        return ("id", raw)
    if raw.startswith("@") and "/" not in raw:
        return ("handle", raw[1:].lower())
    if not raw:
        return None
    try:
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        return None
    path = parsed.path.rstrip("/")
    match = re.match(r"^/channel/(UC[\w-]{22})(?:/.*)?$", path)
    if match:
        return ("id", match.group(1))
    match = re.match(r"^/@([^/]+)(?:/.*)?$", path)
    if match:
        return ("handle", match.group(1).lower())
    return None


def _identity(row, index):
    address = _email(row.get("邮箱")) or _text(row.get("邮箱")).lower()
    message_id = _text(row.get("邮件Message-ID"))
    if message_id:
        key = ("message-id", address, message_id)
    else:
        key = ("legacy", address, _text(row.get("邀约批次")), _text(row.get("发送时间")),
               _text(row.get("合作产品")), _text(row.get("合作类型")))
        if not address and not _text(row.get("发送时间")):
            key += (index,)
    account = _text(row.get("account_id")) or _text(row.get("发件账号ID"))
    serialized = json.dumps((account, key), ensure_ascii=False, separators=(",", ":"))
    return "invitation:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _timestamp(raw):
    raw = _text(raw)
    if not raw:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return float("-inf")


def _row_order(row, index):
    raw = _first(row, "报价来源时间", "最近回信时间", "最后回信时间", "回信时间", "发送时间")
    return (_timestamp(raw), index)


def _currency(value):
    value = _text(value)
    if not value:
        return None
    # A bare '$' does not distinguish USD/CAD/AUD. Leave it unconfirmed.
    return {"US$": "USD", "€": "EUR", "£": "GBP"}.get(value.upper(), value.upper())


def _quote_options(row):
    raw = row.get("报价明细")
    present = isinstance(raw, (list, dict)) or bool(_text(raw))
    details = raw
    if present and isinstance(raw, str):
        try:
            details = json.loads(raw)
        except (ValueError, TypeError):
            details = None
    options = []
    if present:
        # An explicit empty/invalid detail list must not resurrect an older
        # price in 对方报价 after the current reply stopped containing a quote.
        if not isinstance(details, list):
            return []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            amount = _numeric(detail.get("amount"))
            lower = _numeric(detail.get("amount_min"))
            upper = _numeric(detail.get("amount_max"))
            valid_amount = amount is not None and amount > 0
            valid_range = (lower is not None and upper is not None
                           and 0 < lower <= upper)
            if not valid_amount and not valid_range:
                continue
            source = _text(detail.get("source")) or "报价明细"
            method = _text(row.get("报价匹配方式"))
            if method and method not in source:
                source += "；" + method
            options.append({
                "amount": amount if valid_amount else None,
                "amount_min": lower if valid_range else None,
                "amount_max": upper if valid_range else None,
                "currency": _currency(detail.get("currency")),
                "coop_type": _text(detail.get("coop_type")) or None,
                "context": _text(detail.get("context")),
                "quantity": detail.get("quantity"),
                "price_basis": detail.get("price_basis"),
                "unit_amount": detail.get("unit_amount"),
                "source": source,
                "source_time": _first(detail, "source_time", "报价来源时间") or _text(row.get("报价来源时间")) or None,
                "source_message_id": _first(detail, "source_message_id", "报价来源邮件Message-ID", "报价来源Message-ID") or _first(row, "报价来源邮件Message-ID", "报价来源Message-ID") or None,
                "source_text": _first(detail, "source_text", "报价来源原文", "报价原文") or _first(row, "报价来源原文", "报价原文") or None,
                "match_method": method or None,
                "parse_status": _text(row.get("报价解析状态")) or None,
                "thread_message_id": _text(row.get("报价线程Message-ID")) or None,
            })
        return options
    amount = _numeric(row.get("对方报价"))
    if amount is None or amount <= 0:
        return []
    explicit_currency = _currency(row.get("报价币种"))
    source = "历史报价字段" if explicit_currency else "历史美元字段待核"
    method = _text(row.get("报价匹配方式"))
    if method:
        source += "；" + method
    return [{
        "amount": amount, "amount_min": None, "amount_max": None,
        "currency": explicit_currency or "USD", "coop_type": None, "context": "",
        "quantity": None, "price_basis": None, "unit_amount": None,
        "source": source,
        "source_time": _text(row.get("报价来源时间")) or None,
        "source_message_id": _first(row, "报价来源邮件Message-ID", "报价来源Message-ID") or None,
        "source_text": _first(row, "报价来源原文", "报价原文") or None,
        "match_method": method or None,
        "parse_status": _text(row.get("报价解析状态")) or None,
        "thread_message_id": _text(row.get("报价线程Message-ID")) or None,
    }]


def _rejected(row):
    return any(_text(row.get(field)).startswith(_REJECTED)
               for field in ("回信状态", "合作状态", "交付状态"))


def _exact_quote(row):
    method = _text(row.get("报价匹配方式"))
    if method.startswith("按邮箱匹配"):
        return False
    if "精确" in method and "线程" in method:
        return True
    original = _text(row.get("邮件Message-ID"))
    thread = _text(row.get("报价线程Message-ID"))
    return bool(original and thread and original == thread)


def _legacy_mirror_key(row):
    """Only proven legacy mirrors share a source-reply identity."""
    if _exact_quote(row):
        return None
    method = _text(row.get("报价匹配方式"))
    if not method.startswith("按邮箱匹配") and _text(row.get("邮件Message-ID")):
        return None
    address = _email(row.get("邮箱"))
    source = _first(row, "报价来源邮件Message-ID", "报价来源Message-ID")
    product = _text(row.get("合作产品"))
    coop_type = _text(row.get("合作类型"))
    if not _EMAIL.fullmatch(address) or not source or not product or not coop_type:
        return None
    return (address, product, coop_type, source)


def _matched_rows(row, sources):
    channel = _channel(row)
    if channel:
        matches = [(source, other) for source, other in sources if _channel(other) == channel]
        if matches:
            return matches, "主页匹配"
    address = _email(row.get("邮箱"))
    if not _EMAIL.fullmatch(address):
        return [], ""
    matches = [(source, other) for source, other in sources if _email(other.get("邮箱")) == address]
    if not matches:
        return [], ""
    channels = {_channel(other) for _, other in matches if _channel(other)}
    if channel and any(other_channel != channel for other_channel in channels):
        return [], "邮箱关联频道冲突"
    # More than one row is safe only when all prove the same channel identity.
    if len(matches) > 1 and (len(channels) != 1 or any(not _channel(other) for _, other in matches)):
        return [], "邮箱关联频道不唯一"
    return matches, "邮箱唯一匹配"


def _view_metric(row, source):
    for field, count_fields in (
        ("近6条长视频均播", ("长视频样本数", "样本数")),
        ("近6条均播", ("近6条样本数", "视频样本数", "样本数", "长视频样本数")),
    ):
        value = _numeric(row.get(field))
        if value is None or value < 0:
            continue
        counts = [_numeric(row.get(column)) for column in count_fields]
        sample = next((number for number in counts if number is not None and number > 0), None)
        explicit_source = _first(row, field + "来源", "均播来源", "views_source", "指标来源")
        if value == 0 and sample is None:
            # An explicitly empty sample cannot prove a zero-view average;
            # free-text provenance must not override a known zero sample.
            if any(number is not None for number in counts) or not explicit_source:
                continue
        return {
            "avg_views": value,
            "views_source": source + "·" + field + ("（" + explicit_source + "）" if explicit_source else ""),
            "views_sample_count": sample,
            "views_time": _first(row, "指标查询时间", "均播查询时间", "数据查询时间") or None,
            "_long": field == "近6条长视频均播",
        }
    return None


def _views(row, matches, match_kind):
    own = _view_metric(row, "邮件库（本邀约）")
    if own is not None:
        own.pop("_long")
        return own
    metrics = []
    for source, other in matches:
        metric = _view_metric(other, source + "（" + match_kind + "）")
        if metric is not None:
            metrics.append(metric)
    empty = {"avg_views": None, "views_source": None, "views_sample_count": None, "views_time": None}
    if not metrics:
        return empty
    if any(metric["_long"] for metric in metrics):
        metrics = [metric for metric in metrics if metric["_long"]]
    latest = max(_timestamp(metric["views_time"]) for metric in metrics)
    if math.isfinite(latest):
        metrics = [metric for metric in metrics if _timestamp(metric["views_time"]) == latest]
    if len({metric["avg_views"] for metric in metrics}) > 1:
        empty["views_source"] = match_kind + "：均播来源冲突待核"
        return empty
    chosen = sorted(metrics, key=lambda metric: (metric["views_source"], -(metric["views_sample_count"] or 0)))[0]
    chosen.pop("_long")
    return chosen


def _unique_value(matches, field, normalize=lambda value: _text(value) or None):
    values = {normalize(row.get(field)) for _, row in matches}
    values.discard(None)
    values.discard("")
    return next(iter(values)) if len(values) == 1 else None


def build_negotiation_candidates(mail_df, history_df, library_df):
    """Return one inspectable candidate per successful, priced invitation.

    Rejected/cancelled invitations and outgoing negotiation replies are omitted.
    Empty send status is not proof of success. The selected invitation's own
    usable metadata always wins; ambiguous channel/email matches stay missing.
    Multi-option prices are only preselected when there is one valid option or
    one exact cooperation-type match. Unknown amounts/currencies stay None.
    """
    sources = [("达人库", row) for row in _records(history_df)]
    sources += [("建联库", row) for row in _records(library_df)]
    invitations = {}
    for index, row in enumerate(_records(mail_df)):
        purpose = _first(row, "邮件类型", "模板用途")
        if (_text(row.get("状态")) != "成功" or _text(row.get("合作类型")) == "议价回复"
                or purpose in ("议价回复", "接受报价")):
            continue
        identity = _identity(row, index)
        current = invitations.get(identity)
        if current is None or _row_order(row, index) > _row_order(current[1], current[0]):
            invitations[identity] = (index, row)
    mirrors = {}
    for identity, (index, row) in invitations.items():
        key = _legacy_mirror_key(row)
        if key is None:
            continue
        order = (_timestamp(row.get("发送时间")), index)
        if key not in mirrors or order > mirrors[key][0]:
            mirrors[key] = (order, identity)
    invitations = {
        identity: (index, row) for identity, (index, row) in invitations.items()
        if _legacy_mirror_key(row) is None
        or mirrors[_legacy_mirror_key(row)][1] == identity
    }
    exact_rows = [row for _, row in invitations.values()
                  if _exact_quote(row) and not _rejected(row) and _quote_options(row)]
    exact_products = {(_email(row.get("邮箱")), _text(row.get("合作产品")))
                      for row in exact_rows if _email(row.get("邮箱")) and _text(row.get("合作产品"))}
    exact_sources = {(_email(row.get("邮箱")), _first(row, "报价来源邮件Message-ID", "报价来源Message-ID"))
                     for row in exact_rows if _email(row.get("邮箱"))
                     and _first(row, "报价来源邮件Message-ID", "报价来源Message-ID")}
    candidates = []
    for identity, (_, row) in invitations.items():
        if _rejected(row):
            continue
        if _text(row.get("报价匹配方式")).startswith("按邮箱匹配"):
            product_key = (_email(row.get("邮箱")), _text(row.get("合作产品")))
            source_key = (_email(row.get("邮箱")), _first(row, "报价来源邮件Message-ID", "报价来源Message-ID"))
            if product_key in exact_products or source_key in exact_sources:
                continue
        options = _quote_options(row)
        if not options:
            continue
        matches, match_kind = _matched_rows(row, sources)
        invitation_type = _text(row.get("合作类型"))
        coop_type = invitation_type or _unique_value(matches, "合作类型")
        default = None
        if len(options) == 1:
            default = options[0]
        elif invitation_type:
            exact = [option for option in options if option["coop_type"] == invitation_type]
            if len(exact) == 1:
                default = exact[0]
        homepage = _first(row, "主页链接", "频道链接")
        if not homepage:
            homepages = {_first(other, "主页链接", "频道链接") for _, other in matches}
            homepages.discard("")
            if len(homepages) == 1:
                homepage = next(iter(homepages))
        item = {
            "id": identity,
            "name": _first(row, "频道名称", "达人名"),
            "email": _email(row.get("邮箱")),
            "product": _text(row.get("合作产品")) or None,
            "coop_type": coop_type or None,
            "country": _country(row.get("国家")) or _unique_value(matches, "国家", _country),
            "homepage": homepage or None,
            "quote_options": options,
            "quote_amount": default["amount"] if default else None,
            "currency": default["currency"] if default else None,
            "quote_source": default["source"] if default else (_text(row.get("报价匹配方式")) or "报价明细（需选择方案）"),
            "quote_coop_type": default["coop_type"] if default else None,
            "quote_type_conflict": bool(default and default["coop_type"] and invitation_type
                                        and default["coop_type"] != invitation_type),
            "quote_match_method": _text(row.get("报价匹配方式")) or None,
            "quote_parse_status": _text(row.get("报价解析状态")) or None,
            "quote_thread_message_id": _text(row.get("报价线程Message-ID")) or None,
            "reply_body": _first(row, "最近回信正文", "回信正文", "最新回信正文", "最近回信摘要") or None,
            "reply_time": _first(row, "最近回信时间", "最后回信时间", "回信时间") or None,
            "raw_row": dict(row),
        }
        item.update(_views(row, matches, match_kind))
        candidates.append(item)
    return candidates
