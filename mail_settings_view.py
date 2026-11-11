"""Account and personal-template settings only; no contact sending operations."""
import hashlib
import json

import streamlit as st

from template_store import PURPOSES, TemplateError, allowed_variables


ACCOUNT_SELECTION_KEY = "mail_active_account_id"
_PREFIX = "mail_tpl_"


def _safe_account_error(error):
    try:
        from mail_accounts import AccountError
        if isinstance(error, AccountError):
            return str(error)
    except ImportError:
        pass
    return "账号操作未完成，请检查配置后重试；已有记录保留。"


def _status(account):
    if not account.get("enabled", True):
        return "已停用"
    return "发信" + ("可用" if account.get("smtp_ready") else "待验证") + " · 回信" + ("可用" if account.get("imap_ready") else "待验证")


def _notify(kind, text):
    st.session_state["mail_settings_notice"] = (kind, text)


def _show_notice():
    notice = st.session_state.pop("mail_settings_notice", None)
    if notice:
        getattr(st, notice[0])(notice[1])


def _clear_pending_secret():
    key = st.session_state.pop("mail_account_clear_secret", None)
    if key:
        st.session_state.pop(key, None)


def render_account_controls(store):
    """Return the selected safe account ID, or None for unclaimed history.

    Parent owns clearing mail/negotiation state on an ID transition. Contacts
    and their selections must not be cleared by this settings helper.
    """
    _clear_pending_secret()
    accounts = store.list_accounts()
    by_id = {row["id"]: row for row in accounts}
    options = [None] + list(by_id)
    if st.session_state.get(ACCOUNT_SELECTION_KEY) not in options:
        st.session_state[ACCOUNT_SELECTION_KEY] = None
    with st.container(key="wb_account_toolbar"):
        status_area, menu_area = st.columns([4, 1])
        with menu_area:
            with st.popover("邮箱与账号", icon=":material/account_circle:", use_container_width=True):
                selector, manager = st.container(), st.container()
                with selector:
                    selected = st.selectbox("查看邮箱", options, key=ACCOUNT_SELECTION_KEY,
                        format_func=lambda key: "历史记录待归属" if key is None else
                        by_id[key].get("name", "") + " · " + by_id[key]["email"] + ("（已停用）" if not by_id[key].get("enabled", True) else ""))
                with manager:
                    if st.button("管理发件账号", icon=":material/settings:", key="mail_manage_accounts", use_container_width=True):
                        _account_dialog(store)
                if selected is None:
                    st.caption("历史记录未确认归属时只读；切换查看邮箱不会改变已提交发送批次。")
                else:
                    st.caption(_status(by_id[selected]))
        with status_area:
            if selected is None:
                st.caption(":material/mail: 历史邮件工作空间 · 待确认归属")
            else:
                st.caption(":material/mail: " + by_id[selected].get("name", "") + " · " + by_id[selected]["email"])
    _show_notice()
    return selected


@st.dialog("管理发件账号", width="large")
def _account_dialog(store):
    render_account_manager(store)


def render_account_manager(store):
    """Public helper also usable without a modal in offline UI tests."""
    _clear_pending_secret()
    accounts = store.list_accounts()
    by_id = {row["id"]: row for row in accounts}
    if accounts:
        st.dataframe([{"名称": row.get("name", ""), "发件邮箱": row["email"], "状态": _status(row),
                       "最近认证": row.get("verified_at", "") or "尚未认证"} for row in accounts],
                     hide_index=True, use_container_width=True)
    else:
        st.caption("尚未添加发件账号；添加账号不会自动发送邮件。")
    readiness = store.credential_status()
    if not readiness.get("available"):
        st.warning(readiness.get("message") or "本机系统凭据库未就绪，不能保存授权码。")
    try:
        legacy = store.legacy_profile()
    except Exception as error:
        legacy = None
        st.warning(_safe_account_error(error))
    options = [None] + (["legacy"] if legacy else []) + list(by_id)
    edit_key = "mail_account_edit_id"
    if st.session_state.get(edit_key) not in options:
        st.session_state[edit_key] = None
    selected = st.selectbox("添加或编辑账号", options, key=edit_key,
        format_func=lambda key: "添加新账号" if key is None else "复用原工作台账号资料" if key == "legacy" else by_id[key]["email"])
    editing = by_id.get(selected)
    source = editing or (legacy if selected == "legacy" else {}) or {}
    connection_defaults = legacy or {}
    token = str(selected or "new")
    form = "mail_account_form_" + token
    with st.form(form):
        name = st.text_input("账号名称", value=source.get("name", ""), key=form + "_name")
        email = st.text_input("发件邮箱", value=source.get("email", ""), key=form + "_email", disabled=editing is not None)
        auth_code = st.text_input("邮箱授权码", value="", type="password", key=form + "_code",
                                  help="授权码只保存至本机系统凭据库，不回显、不导出；这不是一次性验证码。")
        if editing:
            st.caption("授权码留空会保留已有凭据。已保存的邮箱地址不可更改；换邮箱请添加新账号，旧账号和记录保留。")
        reuse_legacy = False
        if selected == "legacy":
            reuse_legacy = st.checkbox("明确复用本机原账号授权码（不显示、不导出）", key=form + "_reuse")
            st.caption("也可不勾选并填写新授权码；不能同时复用和填写。")
        with st.expander("更多设置", expanded=False):
            st.caption("使用加密直连：发信通常为465端口，回信通常为993端口；暂不支持587端口的STARTTLS方式。")
            signature = st.text_input("对外发件署名", value=source.get("sender_name", ""), key=form + "_signature")
            server = st.text_input("SMTP服务器", value=source.get("server") or connection_defaults.get("server", "smtp.qiye.aliyun.com"), key=form + "_server")
            port = st.number_input("SMTP端口", min_value=1, max_value=65535, value=int(source.get("port") or connection_defaults.get("port", 465)), key=form + "_port")
            imap_server = st.text_input("IMAP服务器", value=source.get("imap_server") or connection_defaults.get("imap_server", "imap.qiye.aliyun.com"), key=form + "_imap")
            imap_port = st.number_input("IMAP端口", min_value=1, max_value=65535, value=int(source.get("imap_port") or connection_defaults.get("imap_port", 993)), key=form + "_imap_port")
            original_cc = source.get("cc") or []
            cc = st.text_input("抄送邮箱", value=", ".join(original_cc) if isinstance(original_cc, list) else str(original_cc), key=form + "_cc")
        save = st.form_submit_button("保存账号")
        verify = st.form_submit_button("保存并验证")
    if save or verify:
        try:
            fields = {"name": name, "email": email, "sender_name": signature or name,
                      "server": server, "port": int(port), "imap_server": imap_server,
                      "imap_port": int(imap_port), "cc": cc}
            account = store.save_account(fields, auth_code, account_id=editing["id"] if editing else None,
                                         reuse_legacy=reuse_legacy)
            if verify:
                with st.spinner("分别验证发信与回信认证，不发送测试邮件..."):
                    result = store.verify_account(account["id"])
                message = "发信" + ("认证通过" if result.get("smtp_ok") else "认证未通过") + "；回信" + ("认证通过" if result.get("imap_ok") else "认证未通过")
                _notify("success" if result.get("smtp_ok") and result.get("imap_ok") else "warning", message)
            else:
                _notify("success", "账号资料已保存；认证状态以分别验证的结果为准。")
            st.session_state["mail_account_clear_secret"] = form + "_code"
            # The password widget disappears on the next app run. Do not copy
            # its value into another session-state key or any notice/log.
            st.rerun()
        except Exception as error:
            st.error(_safe_account_error(error))
    if editing:
        col1, col2 = st.columns(2)
        if col1.button("重新验证发信与回信", key="mail_reverify_" + token):
            try:
                with st.spinner("正在连接认证，不发送邮件..."):
                    result = store.verify_account(editing["id"])
                _notify("success" if result.get("smtp_ok") and result.get("imap_ok") else "warning",
                        "发信" + ("可用" if result.get("smtp_ok") else "认证未通过") + "；回信" + ("可用" if result.get("imap_ok") else "认证未通过"))
                st.rerun()
            except Exception as error:
                st.error(_safe_account_error(error))
        if col2.button("停用此账号" if editing.get("enabled", True) else "重新启用账号", key="mail_enable_" + token):
            try:
                store.set_enabled(editing["id"], not editing.get("enabled", True))
                _notify("success", "账号启用状态已更新，历史记录保留；请核对认证状态。")
                st.rerun()
            except Exception as error:
                st.error(_safe_account_error(error))
    with st.expander("确认旧记录的发件归属", expanded=False):
        st.caption("不能把当前配置邮箱当作历史归属证明；不确定的记录继续保留待归属只读。")
        try:
            metadata = store.legacy_metadata()
            st.caption(metadata.get("message", ""))
            files = metadata.get("files", [])
            if files:
                st.dataframe([{"文件": item.get("name", ""), "记录数": item.get("rows", 0)} for item in files], hide_index=True)
            if metadata.get("claimed_account_id"):
                owner = by_id.get(metadata["claimed_account_id"], {})
                st.info("历史记录已有明确归属：" + owner.get("email", "原已确认账号") + "；不自动移动或重复导入。")
            else:
                eligible = [a for a in accounts if a.get("enabled", True) and a.get("smtp_ready") and a.get("imap_ready")]
                owner_ids = [a["id"] for a in eligible]
                if not owner_ids:
                    st.info("请先保存并完成原账号发信与回信认证，再进行归属确认。")
                else:
                    owner_id = st.selectbox("确知的原发件账号", owner_ids, format_func=lambda key: by_id[key]["email"], key="mail_claim_owner")
                    confirm = st.checkbox("我确认历史邮件由所选原账号发出", key="mail_claim_confirm")
                    if st.button("确认归属并保留原文件", disabled=not confirm or not metadata.get("has_mail_log", False), key="mail_claim_submit"):
                        result = store.claim_legacy(owner_id, "确认历史邮件由该账号发出")
                        _notify("success", result.get("message", "归属已保存，原文件保留。"))
                        st.rerun()
        except Exception as error:
            st.error(_safe_account_error(error))
    _show_notice()


def _draft_key():
    return _PREFIX + "draft"


def _combo(draft):
    return (draft["product"], draft["coop_type"], draft["purpose"])


def _dirty(draft):
    return draft["subject"] != draft["base_subject"] or draft["body"] != draft["base_body"]


def _load_draft(store, combo):
    saved = store.get_template(*combo)
    saved = saved or {"product": combo[0], "coop_type": combo[1], "purpose": combo[2],
                      "subject": "", "body": "", "version": 0,
                      "product_id": store.get_product(combo[0])["id"], "source": "模板待配置"}
    draft = dict(saved, base_subject=saved["subject"], base_body=saved["body"])
    st.session_state[_draft_key()] = draft
    for field, value in zip(("product", "coop", "purpose"), combo):
        st.session_state[_PREFIX + field] = value
    st.session_state[_PREFIX + "subject"] = draft["subject"]
    st.session_state[_PREFIX + "body"] = draft["body"]
    st.session_state[_PREFIX + "restore_confirm"] = False
    st.session_state[_PREFIX + "preview_amount"] = ""
    st.session_state[_PREFIX + "preview_currency"] = "请选择币种"
    st.session_state.pop(_PREFIX + "pending", None)
    st.session_state.pop(_PREFIX + "preview", None)
    return draft


def _change_combo(store):
    desired = tuple(st.session_state[_PREFIX + key] for key in ("product", "coop", "purpose"))
    current = st.session_state[_draft_key()]
    if desired == _combo(current):
        return
    if _dirty(current):
        st.session_state[_PREFIX + "pending"] = desired
        for key, value in zip(("product", "coop", "purpose"), _combo(current)):
            st.session_state[_PREFIX + key] = value
    else:
        _load_draft(store, desired)


def _sync_draft():
    draft = st.session_state[_draft_key()]
    for key in ("subject", "body"):
        draft[key] = st.session_state.get(_PREFIX + key, draft[key])
    st.session_state.pop(_PREFIX + "preview", None)


def _save_draft(store, switch=False):
    draft = st.session_state[_draft_key()]
    try:
        result = store.save_template(*_combo(draft), subject=draft["subject"], body=draft["body"], expected_version=draft["version"])
        desired = st.session_state.get(_PREFIX + "pending") if switch else _combo(draft)
        _load_draft(store, desired or _combo(draft))
        _notify("success", "个人模板已保存为版本" + str(result["version"]) + "，已发送正文不变。")
    except TemplateError as error:
        _notify("error", str(error))


def _discard_switch(store):
    desired = st.session_state.get(_PREFIX + "pending")
    if desired:
        _load_draft(store, desired)


def _cancel_switch():
    st.session_state.pop(_PREFIX + "pending", None)


def _insert_variable(variable, field):
    key = _PREFIX + field
    st.session_state[key] = st.session_state.get(key, "") + "{" + variable + "}"
    _sync_draft()


def _draft_digest(draft):
    return hashlib.sha256(json.dumps([_combo(draft), draft["subject"], draft["body"]], ensure_ascii=False).encode()).hexdigest()


def render_template_manager(store, account_store, active_id):
    """Edit personal templates; account_store supplies safe signature metadata."""
    reload_combo = st.session_state.pop(_PREFIX + "reload_combo", None)
    if reload_combo:
        _load_draft(store, reload_combo)
    st.caption("本机个人产品模板库由本人的多个发件邮箱共用，署名按发件账号生成；其他独立目录不受修改影响。")
    products = store.products()
    if not products:
        st.info("没有启用的产品，请先在下方新增或启用产品。")
    if _draft_key() not in st.session_state and products:
        _load_draft(store, (products[0], store.coop_types()[0], PURPOSES[0]))
    draft = st.session_state.get(_draft_key())
    if draft:
        # Widget keys may be removed when navigating away. The separate draft
        # retains committed edits and restores them when returning to this tab.
        for key, value in (("product", draft["product"]), ("coop", draft["coop_type"]), ("purpose", draft["purpose"]),
                           ("subject", draft["subject"]), ("body", draft["body"])):
            if _PREFIX + key not in st.session_state:
                st.session_state[_PREFIX + key] = value
        product_options = list(dict.fromkeys(products + [draft["product"]]))
        coop_options = list(dict.fromkeys(store.coop_types() + [draft["coop_type"]]))
        columns = st.columns(3)
        columns[0].selectbox("合作产品", product_options, key=_PREFIX + "product", on_change=_change_combo, args=(store,))
        columns[1].selectbox("合作形式", coop_options, key=_PREFIX + "coop", on_change=_change_combo, args=(store,))
        columns[2].selectbox("邮件用途", list(PURPOSES), key=_PREFIX + "purpose", on_change=_change_combo, args=(store,))
        if st.session_state.get(_PREFIX + "pending"):
            st.warning("当前组合有未保存修改，仍保留在原组合；请先决定后再切换。")
            choices = st.columns(3)
            choices[0].button("保存并切换", key=_PREFIX + "save_switch", on_click=_save_draft, args=(store, True))
            choices[1].button("放弃修改并切换", key=_PREFIX + "discard_switch", on_click=_discard_switch, args=(store,))
            choices[2].button("取消切换", key=_PREFIX + "cancel_switch", on_click=_cancel_switch)
        active_product = store.get_product(draft["product"])["active"]
        st.caption("当前组合：" + " / ".join(_combo(draft)) + " · " + draft.get("source", "个人模板") + " · 版本" + str(draft["version"]) + (" · 未保存" if _dirty(draft) else ""))
        if not active_product:
            st.warning("该产品已停用，历史模板保留；启用后才可保存或生成新邮件。")
        st.text_input("邮件主题", key=_PREFIX + "subject", on_change=_sync_draft)
        st.text_area("邮件正文", key=_PREFIX + "body", height=310, on_change=_sync_draft)
        with st.expander("插入可用变量", expanded=False):
            location = st.radio("插入位置", ["正文", "主题"], horizontal=True, key=_PREFIX + "insert_location")
            groups = st.columns(3)
            for index, variable in enumerate(allowed_variables(draft["purpose"])):
                groups[index % 3].button("{" + variable + "}", key=_PREFIX + "insert_" + variable,
                    on_click=_insert_variable, args=(variable, "body" if location == "正文" else "subject"))
            st.caption("首次邀约先让达人报价，不提供内部预算/建议价变量；回复金额仅用本次明确确认的对外金额。")
        preview_values = {"达人名": "示例达人", "视频链接": "https://www.youtube.com/watch?v=DEMO0000000"}
        if draft["purpose"] != "首次邀约":
            left, right = st.columns(2)
            preview_values["对外金额"] = left.text_input("预览用对外金额", key=_PREFIX + "preview_amount")
            currency = right.selectbox("预览用币种", ["请选择币种", "USD", "EUR", "GBP", "JPY", "CNY", "KRW"], key=_PREFIX + "preview_currency")
            preview_values["币种"] = "" if currency == "请选择币种" else currency
        account = account_store.get_account(active_id) if active_id else None
        if account:
            preview_values.update({"发件人姓名": account.get("sender_name") or account.get("name", ""), "发件邮箱": account["email"]})
        controls = st.columns([1, 1, 2])
        if controls[0].button("预览", key=_PREFIX + "preview_button", disabled=not active_product):
            if account is None:
                st.warning("请先在右上角选择发件邮箱，才能预览该账号署名；预览不会发信。")
            else:
                try:
                    rendered = store.render_content(draft, preview_values)
                    st.session_state[_PREFIX + "preview"] = {"account_id": active_id, "revision": account.get("revision"),
                        "digest": _draft_digest(draft), "rendered": rendered, "values": dict(preview_values)}
                except TemplateError as error:
                    st.error(str(error))
        controls[1].button("保存模板", key=_PREFIX + "save", disabled=not active_product, on_click=_save_draft, args=(store,))
        with st.expander("还原当前组合默认模板", expanded=False):
            has_default = store.has_default(*_combo(draft))
            if not has_default:
                st.caption("当前组合没有预置默认模板；现有个人模板不会被清空。")
            confirm = st.checkbox("确认只还原当前组合，放弃当前未保存修改", key=_PREFIX + "restore_confirm")
            if st.button("还原默认", key=_PREFIX + "restore", disabled=not confirm or not has_default or not active_product):
                try:
                    result = store.restore_template(*_combo(draft), expected_version=draft["version"])
                    # Apply on a following callback run, before widget creation.
                    st.session_state[_PREFIX + "reload_combo"] = _combo(draft)
                    _notify("success", "已还原当前组合为版本" + str(result["version"]) + "，历史邮件正文保留。")
                    st.rerun()
                except TemplateError as error:
                    st.error(str(error))
        preview = st.session_state.get(_PREFIX + "preview")
        if preview and (preview["account_id"] != active_id or preview["digest"] != _draft_digest(draft)
                        or account is None or preview["revision"] != account.get("revision")
                        or preview.get("values") != preview_values):
            st.session_state.pop(_PREFIX + "preview", None)
            preview = None
        if preview:
            st.markdown("**预览 发件邮箱：" + account["email"] + "**")
            st.caption("使用示例达人和示例链接；实际收件人、正文和发件账号仍须在发送批次中确认。")
            st.text(preview["rendered"]["subject"])
            st.text(preview["rendered"]["body"])
    _render_product_controls(store)
    _show_notice()


def _render_product_controls(store):
    with st.expander("新增或停用产品", expanded=False):
        with st.form("mail_product_add_form"):
            name = st.text_input("新产品名称", key="mail_product_name")
            url = st.text_input("产品官网链接（可选）", key="mail_product_url")
            submit = st.form_submit_button("新增产品")
        if submit:
            try:
                product = store.add_product(name, url)
                _notify("success" if product["created"] else "info",
                        "产品已新增，请为所需合作形式和用途配置模板。" if product["created"] else
                        "同名产品已存在，请使用已有产品；停用产品需明确重新启用。")
                st.rerun()
            except TemplateError as error:
                st.error(str(error))
        all_products = store.list_products(include_inactive=True)
        by_id = {product["id"]: product for product in all_products}
        if by_id:
            chosen = st.selectbox("管理产品状态", list(by_id), format_func=lambda key: by_id[key]["name"] + ("（已停用）" if not by_id[key]["active"] else ""), key="mail_product_status_id")
            product = by_id[chosen]
            if st.button("停用此产品" if product["active"] else "启用此产品", key="mail_product_enable"):
                store.set_product_active(chosen, not product["active"])
                _notify("success", "产品状态已更新，原有模板版本和历史记录保留。")
                st.rerun()
        st.caption("新增产品不自动继承其他产品的监测词、价格策略或GA4配置。")
