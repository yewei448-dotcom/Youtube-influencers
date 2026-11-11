"""Explicit, channel-scoped review actions. No email or network operations."""

from copy import deepcopy
from uuid import uuid4

import pandas as pd
import streamlit as st


REASON_LABELS = {"channel": "频道不合适", "unsuitable": "已建联不适合", "no_reply": "多次未回复",
                 "poor": "已合作效果差", "monitor": "已合作持续监测"}
TAG_OPTIONS = list(REASON_LABELS.values())
LANGUAGES = {"en": "英语", "de": "德语", "ja": "日语", "ko": "韩语", "zh-Hant": "繁体中文", "fr": "法语", "es": "西班牙语"}
FORMS = ("独立视频", "加链接", "shorts", "插播")
_ACTIVE_DIALOG = "_resource_review_active_prefix"


def _text(value):
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value.lower() in ("nan", "none", "<na>", "null") else value


def _records(frame):
    if frame is None:
        return []
    if isinstance(frame, pd.DataFrame):
        return frame.to_dict("records")
    if isinstance(frame, dict):
        return [dict(frame)]
    return [dict(row) for row in frame]


def normalize_reason(value):
    value = _text(value)
    return value if value in REASON_LABELS else next((key for key, label in REASON_LABELS.items() if label == value), "")


def normalize_language(value):
    value = _text(value)
    return value if value in LANGUAGES else next((key for key, label in LANGUAGES.items() if label == value), "")


def _context(product, coop_type, language, account_id):
    return {"product": _text(product), "coop_type": _text(coop_type),
            "language": normalize_language(language), "account_id": _text(account_id)}


def review_version(prefix):
    """Append this to the parent's data_editor key to clear temporary tag cells."""
    return int(st.session_state.get(str(prefix) + "_review_ui_version", 0))


def _pending_key(prefix):
    return str(prefix) + "_review_pending"


def _clear_action(prefix, notice=None):
    st.session_state.pop(_pending_key(prefix), None)
    if st.session_state.get(_ACTIVE_DIALOG) == prefix:
        st.session_state.pop(_ACTIVE_DIALOG, None)
    key = str(prefix) + "_review_ui_version"
    st.session_state[key] = review_version(prefix) + 1
    if notice:
        st.session_state[str(prefix) + "_review_notice"] = notice


def _finish(prefix, notice=None):
    _clear_action(prefix, notice)
    st.rerun()


def _dismiss_tag_dialog():
    """Dismissal already triggers a full rerun; clear only this UI action."""
    prefix = st.session_state.get(_ACTIVE_DIALOG)
    if prefix is not None:
        _clear_action(prefix, ("info", "已取消本次标签操作；已有记录保留。"))


def _store(store=None):
    if store is not None:
        return store
    from resource_decisions import get_store
    return get_store()


def _channel_id(row):
    from resource_decisions import channel_identity
    return channel_identity(row)


def _unique_rows(rows):
    unique, invalid = {}, 0
    for row in _records(rows):
        channel_id = _channel_id(row)
        if not channel_id:
            invalid += 1
        elif channel_id not in unique:
            unique[channel_id] = deepcopy(row)
    return list(unique.values()), invalid


def _scope_key(scope):
    # Product aliases are normalized by the store; compare case-insensitively
    # with the explicit compatibility alias used by the current workbench.
    product = " ".join(_text(scope.get("product")).split()).casefold()
    if product == "reiboot for ios":
        product = "reiboot"
    return (product, " ".join(_text(scope.get("coop_type")).split()).casefold(),
            normalize_language(scope.get("language")), _text(scope.get("account_id")),
            bool(scope.get("all_products")))


def _version_at_open(pending, row, reason, scope):
    channel_id = _channel_id(row)
    for record in pending["versions"]:
        if record.get("channel_id") == channel_id and record.get("reason") == reason and _scope_key(record.get("scope", {})) == _scope_key(scope):
            return int(record["version"])
    return 0


def _open_action(rows, reason, context, prefix, store, actor):
    unique, invalid = _unique_rows(rows)
    if invalid:
        st.warning("部分资料缺少规范频道ID，请先补充频道主页后再打标签；不会按名称或邮箱猜测。")
    if not unique:
        return False
    versions = store.list_records(include_restored=True)
    ids = {_channel_id(row) for row in unique}
    st.session_state[_pending_key(prefix)] = {"token": uuid4().hex, "rows": unique, "reason": reason,
        "context": deepcopy(context), "actor": _text(actor),
        "versions": [deepcopy(record) for record in versions if record.get("channel_id") in ids]}
    return True


def _form_selector(context, token):
    options = list(dict.fromkeys(([context["coop_type"]] if context["coop_type"] else []) + list(FORMS)))
    return st.selectbox("本次限定的合作类型", options,
                        index=options.index(context["coop_type"]) if context["coop_type"] else None,
                        placeholder="请选择合作类型", key=token + "_form") or ""


def _scope_controls(reason, context, token):
    scope = {"product": context["product"], "coop_type": "", "language": "", "account_id": "", "all_products": False}
    valid = True
    if reason == "channel":
        scope["all_products"] = st.radio("生效产品范围", ["仅本产品", "所有产品"], horizontal=True, key=token + "_products") == "所有产品"
        if scope["all_products"]:
            scope["product"] = ""
            st.warning("此频道会在所有产品的新检索中被过滤；已有资料与业务记录保留。")
    elif reason == "unsuitable":
        options = ["产品 + 合作类型"]
        if context["language"]:
            options += ["产品 + 语言", "产品 + 合作类型 + 语言"]
        else:
            st.caption("当前语言范围未明确，请按产品与合作类型标记；不会从关键词或地区推断语言。")
        dimension = st.radio("不适合的具体范围", options, key=token + "_dimension")
        if "合作类型" in dimension:
            scope["coop_type"] = _form_selector(context, token)
        if "语言" in dimension:
            scope["language"] = context["language"]
            st.caption("本次明确的语言范围：" + LANGUAGES[context["language"]])
        valid = bool(scope["coop_type"] or scope["language"])
    elif reason == "no_reply":
        scope["account_id"] = context["account_id"]
        st.caption("仅对本产品、当前选择的发件账号生效，不影响其他账号。")
        if not scope["account_id"]:
            st.warning("请先选择发件账号，再使用“多次未回复”；不会扩大为全局排除。")
            valid = False
    elif reason == "poor":
        scope["coop_type"] = _form_selector(context, token)
        valid = bool(scope["coop_type"])
    else:
        st.caption("确认后保留持续监测，标签不会删除监控、联系人或邮件记录。")
    if not scope["all_products"] and not scope["product"]:
        st.warning("请先在结果区明确产品，再重新打开这个标签。")
        valid = False
    return scope, valid


@st.dialog("给达人打标签", width="large", on_dismiss=_dismiss_tag_dialog)
def _tag_dialog(prefix, current_context, store, monitor_callback):
    pending = st.session_state.get(_pending_key(prefix))
    if not pending:
        return
    if pending["context"] != current_context:
        st.warning("产品、合作类型、语言或发件账号已变化。为避免标错范围，请关闭后重新打开标签。")
        if st.button("关闭并重新选择", key=pending["token"] + "_reopen"):
            _finish(prefix)
        return
    rows, token = pending["rows"], pending["token"]
    st.text(("本次频道：" if len(rows) == 1 else "本次 %d 个频道：" % len(rows)) +
                "、".join(_text(row.get("频道名称") or row.get("name") or row.get("频道") or _channel_id(row)) for row in rows[:5]) +
                (" 等" if len(rows) > 5 else ""))
    st.caption("产品：" + (current_context["product"] or "未选择") + "。本次仅操作这里列出的频道。")
    reason = st.selectbox("标签类型", list(REASON_LABELS), index=list(REASON_LABELS).index(pending["reason"]),
                          format_func=REASON_LABELS.get, key=token + "_reason")
    scope, valid = _scope_controls(reason, current_context, token)
    note = st.text_area("备注（可选）", max_chars=1000, key=token + "_note")
    until = st.date_input("到期日期（可选，留空为长期）", value=None, format="YYYY-MM-DD", key=token + "_until")
    st.caption("若填写到期日，该日北京时间结束后自动不再过滤。已有资源、监控及邮件不会删除。")
    if reason == "monitor" and monitor_callback is None:
        st.warning("当前入口尚未连接持续监测，请从可执行监测的结果区使用此标签。")
        valid = False
    confirm, cancel = st.columns(2)
    if confirm.button("确认保存标签", type="primary", disabled=not valid, key=token + "_save"):
        try:
            if reason == "monitor" and not monitor_callback(deepcopy(rows), scope["product"]):
                st.warning("持续监测尚未确认，标签未保存；已有监控保留。")
                return
            count = 0
            for row in rows:
                store.save(row, reason, **scope, note=note, until=until.isoformat() if until else "",
                           actor=pending["actor"], expected_version=_version_at_open(pending, row, reason, scope))
                count += 1
        except Exception:
            st.error("本次标签未全部保存，记录可能已被其他操作修改。已保存部分保留，请关闭后刷新核对；不会删除任何业务资料。")
        else:
            _finish(prefix, "已为 %d 个频道保存“%s”；已有业务记录保留。" % (count, REASON_LABELS[reason]))
    if cancel.button("取消", key=token + "_cancel"):
        _finish(prefix)


def render_tag_actions(frame, product="", coop_type="", language="", account_id=None,
                       prefix="resource", selected=None, pending_tags=None, monitor_callback=None,
                       store=None, actor=""):
    """Parent table owns its visible per-row tag selectbox and resource edits.

    ``pending_tags`` accepts {row: dict, requested_reason: label/code}, or a
    flat row carrying requested_reason. ``selected`` is only for batch actions.
    Parent should append review_version(prefix) to its data_editor key.
    """
    store = _store(store)
    context = _context(product, coop_type, language, account_id)
    notice = st.session_state.pop(str(prefix) + "_review_notice", None)
    if notice:
        kind, message = notice if isinstance(notice, (list, tuple)) else ("success", notice)
        getattr(st, kind)(message)
    selected_rows, invalid = _unique_rows(selected)
    label, action = st.columns([2, 2])
    reason = label.selectbox("批量标签类型", list(REASON_LABELS), index=None,
                            placeholder="选择标签类型", format_func=REASON_LABELS.get,
                            key=str(prefix) + "_batch_reason_" + str(review_version(prefix)))
    batch_allowed = bool(selected_rows and reason and not (reason == "no_reply" and not context["account_id"]))
    if reason == "no_reply" and not context["account_id"]:
        st.caption("“多次未回复”需要先选择发件账号。")
    if action.button("给勾选达人打标签（%d）" % len(selected_rows), disabled=not batch_allowed,
                     key=str(prefix) + "_batch_tag_" + str(review_version(prefix))):
        _open_action(selected_rows, reason, context, prefix, store, actor)
    if invalid:
        st.caption("%d 条所选资料缺少规范频道ID，未纳入批量操作。" % invalid)
    requested = _records(pending_tags)
    if requested and not st.session_state.get(_pending_key(prefix)):
        item = requested[0]
        value = item.get("requested_reason", item.get("reason", item.get("打标签")))
        requested_reason = normalize_reason(value)
        if requested_reason:
            if not _open_action([item.get("row", item)], requested_reason, context, prefix, store, actor):
                _finish(prefix, ("warning", "此资料缺少规范频道ID，未保存标签；请补充频道主页后重试。"))
        if len(requested) > 1:
            st.caption("本次先处理第一条行内标签；批量处理请使用上方批量操作。")
    if st.session_state.get(_pending_key(prefix)):
        st.session_state[_ACTIVE_DIALOG] = prefix
        _tag_dialog(prefix, context, store, monitor_callback)


def _scope_text(record):
    scope = record.get("scope", {})
    parts = ["所有产品" if scope.get("all_products") else _text(scope.get("product")) or "产品待核"]
    if scope.get("coop_type"):
        parts.append(_text(scope["coop_type"]))
    if scope.get("language"):
        parts.append(LANGUAGES.get(scope["language"], _text(scope["language"])))
    if scope.get("account_id"):
        parts.append("仅标记时的发件账号")
    return " · ".join(parts)


def render_review_pool(product=None, account_id=None, prefix="review_pool", store=None, actor=""):
    store = _store(store)
    st.subheader("人工筛选池")
    st.caption("这里保留人工标签和作用范围；恢复只取消标签过滤，不删除或停用监控、联系人、邮件。")
    notice = st.session_state.pop(str(prefix) + "_review_notice", None)
    if notice:
        kind, message = notice if isinstance(notice, (list, tuple)) else ("success", notice)
        getattr(st, kind)(message)
    col1, col2 = st.columns([2, 2])
    reason = col1.selectbox("筛选标签", [None] + list(REASON_LABELS), format_func=lambda value: REASON_LABELS.get(value, "全部标签"), key=str(prefix) + "_reason")
    restored = col2.checkbox("显示已恢复记录", key=str(prefix) + "_restored")
    rows = store.list_records(include_restored=restored, product=product, reason=reason,
                              account_id=account_id or None, active_only=False)
    if not rows:
        st.info("当前范围没有人工标签记录。")
        return
    size = 10
    pages = (len(rows) + size - 1) // size
    page_key = str(prefix) + "_page"
    if st.session_state.get(page_key, 1) not in range(1, pages + 1):
        st.session_state[page_key] = 1
    page = st.selectbox("标签记录页", range(1, pages + 1), key=page_key) if pages > 1 else 1
    st.caption("共 %d 条，当前第 %d / %d 页" % (len(rows), page, pages))
    for record in rows[(page - 1) * size:page * size]:
        with st.container(border=True):
            info, action = st.columns([5, 1])
            info.text(_text(record.get("name") or record.get("channel_id")) + " · " + REASON_LABELS.get(record.get("reason"), _text(record.get("reason_label"))))
            info.caption("范围：" + _scope_text(record) + "｜状态：" + _text(record.get("state")))
            info.caption("操作者：" + (_text(record.get("actor")) or "未记录") + "｜时间：" + _text(record.get("updated_at") or record.get("created_at")))
            info.caption("备注：" + (_text(record.get("note")) or "无") + "｜到期：" + (_text(record.get("until")) or "长期"))
            cid = record.get("channel_id")
            if cid:
                info.link_button("频道主页", "https://www.youtube.com/channel/" + cid)
            if record.get("state") == "已恢复":
                action.caption("已恢复")
            elif action.button("恢复显示", key=str(prefix) + "_restore_" + str(record["id"]) + "_" + str(record["version"])):
                try:
                    store.restore(record["id"], expected_version=record["version"], actor=actor)
                except Exception:
                    st.error("记录已变化或暂未保存，请刷新后核对；已有业务记录保留。")
                else:
                    _finish(prefix, "标签已恢复；监控、联系人和邮件记录保留。")
