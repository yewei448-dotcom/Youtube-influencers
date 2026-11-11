"""Internal action list and delivery tracking. No external messages are sent here."""
from datetime import datetime, date, timezone, timedelta
import hashlib
import json
import re

import pandas as pd
import streamlit as st

import email_module
from reminder_rules import collect_reminders
from reply_sync import ReplySyncWorker
from workbench_io import read_csv_file, write_csv_atomic

DELIVERY_FIELDS = ["合作ID", "约定交付日期", "交付状态", "实际交付日期"]
DELIVERY_STATES = ["待确认", "待交付", "制作中", "待发布", "已交付", "已验收", "已取消"]
BEIJING = timezone(timedelta(hours=8))


def text(value):
    value = str(value).strip()
    return "" if value.lower() in ("", "nan", "none", "<na>") else value


def ensure_delivery_fields(frame):
    frame = frame.copy()
    for column in DELIVERY_FIELDS:
        if column not in frame:
            frame[column] = ""
    for i, row in frame.iterrows():
        if not text(row.get("合作ID", "")):
            identity = [text(row.get(c, "")) for c in
                        ("达人名", "邮箱", "合作日期", "记录时间")]
            identity.append(str(i))
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]
            frame.at[i, "合作ID"] = "legacy-" + digest
        if not text(row.get("交付状态", "")):
            frame.at[i, "交付状态"] = "待确认"
    return frame


@st.cache_resource
def _reply_worker_for_account(account_id):
    return ReplySyncWorker(lambda: email_module.update_reply_status(strict=True, account_id=account_id),
                           account_id=account_id or "")


def reply_worker(account_id=None):
    account = email_module._resolve_account(account_id)
    if account:
        profile = email_module._account_api().get_store().get_account(account)
        if not profile.get("enabled", True) or not profile.get("imap_ready", False):
            return _reply_worker_for_account(None)
    return _reply_worker_for_account(account)


def sync_caption(account_id=None):
    status = reply_worker(account_id).status()
    if status.get("read_only"):
        account = email_module._resolve_account(account_id)
        st.caption("该账号已停用或尚未通过回信验证；当前仅查看已保存记录，不连接邮箱。" if account else
                   "未选择发件账号，未归属历史邮件仅供查看；不会连接邮箱或修改记录。")
    elif status["running"]:
        st.caption("正在后台检查回信，当前清单来自已保存记录。")
    elif status["error"]:
        st.warning("本次回信同步未完成，清单保留上次记录。请稍后重试。")
    elif status["last_success"]:
        stamp = datetime.fromisoformat(status["last_success"]).astimezone(BEIJING)
        st.caption("最近完成同步：" + stamp.strftime("%m-%d %H:%M") + "（北京时间）；点击“刷新待办清单”查看更新。")
    else:
        st.caption("清单来自已保存记录；可检查回信以更新。")


def valid_date(value):
    if not value:
        return True
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@st.fragment(run_every=60)
def render_sync_status(account_id=None):
    reply_worker(account_id).start()
    sync_caption(account_id)


def render_reminder_center(effect_file, account_id=None):
    account_id = email_module._resolve_account(account_id)
    account_key = account_id or "legacy-unassigned"
    snapshot_key = "reminder_reply_snapshot_" + account_key
    st.subheader("待办提醒")
    st.caption("汇总需要回复的达人、临近交付和逾期事项。企微尚未接通，当前不会向外发送提醒。")
    st.caption("邮件回信跟随当前选择的发件账号；交付事项来自共用合作记录，不按达人邮箱推断归属。")
    worker = reply_worker(account_id)
    worker.start()
    if st.button("检查最新回信", key="reminder_sync_" + account_key,
                 disabled=not account_id or bool(worker.status().get("read_only"))):
        worker.start(force=True)
    render_sync_status(account_id)
    refresh = st.button("刷新待办清单", key="reminder_refresh_" + account_key)
    lead_days = st.number_input("提前几天提醒交付", min_value=0, max_value=30, value=3,
                                key="delivery_lead_days")
    mails = email_module.load_mail_log(account_id).fillna("")
    if not mails.empty:
        mails = mails[~mails.apply(email_module._is_outgoing_followup, axis=1)].copy()
    effects = ensure_delivery_fields(read_csv_file(effect_file).fillna(""))
    pending = mails[mails.get("回信状态", pd.Series("", index=mails.index)).astype(str)
                    .str.startswith("已回信待处理")].copy()
    if "状态" in pending:
        pending = pending[pending["状态"].isin(["成功", ""])]
    # Preserve exactly the rows/counts shown while the user ticks checkboxes.
    # A background sync must not acknowledge a new reply they haven't seen.
    if refresh or snapshot_key not in st.session_state:
        st.session_state[snapshot_key] = pending.copy()
    pending = st.session_state[snapshot_key].copy()
    reminders = collect_reminders(pending.to_dict("records"), effects.to_dict("records"),
                                 today=datetime.now(BEIJING).date(), lead_days=int(lead_days))
    a, b, c = st.columns(3)
    a.metric("待回复记录", len(reminders["replies"]))
    b.metric("交付将到期 / 已逾期", len(reminders["deliveries"]))
    c.metric("交付信息待补充", len(reminders["missing_due"]))

    st.markdown("**需要回复**")
    st.caption("回信清单保持当前快照，后台同步完成后点击“刷新待办清单”查看新回信。")
    if pending.empty:
        st.info("已保存记录中没有待处理回信。")
    else:
        shown = pd.DataFrame({"已处理": False, "达人": pending.get("频道名称", ""),
                              "合作产品": pending.get("合作产品", ""),
                              "邮箱": pending.get("邮箱", ""),
                              "回信状态": pending["回信状态"],
                              "最近回信时间": pending.get("最近回信时间", ""),
                              "最近回信摘要": pending.get("最近回信摘要", "")})
        edited = st.data_editor(shown, hide_index=True, use_container_width=True,
                                disabled=[col for col in shown if col != "已处理"] if account_id else True,
                                key="reminder_reply_editor_" + account_key)
        if st.button("将勾选回信标记为已处理", key="mark_reminder_handled_" + account_key, disabled=not account_id):
            chosen = pending.loc[edited.index[edited["已处理"].fillna(False)]]
            if chosen.empty:
                st.info("请先勾选已处理的回信。")
            else:
                keys = [email_module._invitation_row_key(row) for _, row in chosen.iterrows()]
                counts = {email_module._invitation_row_key(row): row.get("回信次数", 0)
                          for _, row in chosen.iterrows()}
                email_module.mark_replies_handled(keys, seen_counts=counts, account_id=account_id)
                del st.session_state[snapshot_key]
                st.success("已处理；对方再次回信会重新进入待办。")
                st.rerun()

    st.markdown("**需要提醒交付**")
    delivery_rows = reminders["deliveries"]
    if delivery_rows:
        st.dataframe(pd.DataFrame([{"达人": x["name"], "产品": x["product"],
                                   "事项": x["detail"]} for x in delivery_rows]),
                     hide_index=True, use_container_width=True)
    else:
        st.info("没有已确认且即将到期或逾期的交付事项。")
    if reminders["missing_due"]:
        st.caption("以下合作需要先补充或确认信息，暂不判断逾期：")
        st.dataframe(pd.DataFrame([{"达人": x["name"], "产品": x["product"],
                                   "待补充": x["detail"]} for x in reminders["missing_due"]]),
                     hide_index=True, use_container_width=True)

    with st.expander("维护交付记录", expanded=False):
        st.caption("填写双方约定的日期；同一达人每次合作分别记录。确认交付后选择“已交付”或“已验收”，提醒会停止。")
        if effects.empty:
            st.info("先在建联库标记已合作，或在数据分析中添加合作记录。")
        else:
            options = effects["合作ID"].tolist()
            labels = {row["合作ID"]: " · ".join([text(row.get("达人名", "")),
                       text(row.get("合作日期", "")), str(i + 1)])
                      for i, row in effects.iterrows()}
            selected = st.selectbox("选择本次合作", options, format_func=labels.get,
                                    key="delivery_record")
            row = effects[effects["合作ID"] == selected].iloc[0]
            source_account = text(row.get("发件账号ID", "")) or text(row.get("account_id", ""))
            st.caption("该合作发件账号ID：" + source_account if source_account else
                       "该合作未记录发件账号（历史共用记录，未自动归属）。")
            defaults = {"product": text(row.get("合作产品", "")),
                        "email": text(row.get("邮箱", "")),
                        "due": text(row["约定交付日期"]), "state": text(row["交付状态"]),
                        "actual": text(row["实际交付日期"]), "link": text(row.get("视频链接", ""))}
            field_keys = {name: "delivery_" + name + "_" + selected for name in defaults}
            for name, value in defaults.items():
                if field_keys[name] not in st.session_state:
                    st.session_state[field_keys[name]] = value
            with st.form("delivery_edit_" + selected):
                product = st.text_input("合作产品", key=field_keys["product"])
                email = st.text_input("联系邮箱", key=field_keys["email"])
                due = st.text_input("约定交付日期", key=field_keys["due"],
                                    placeholder="YYYY-MM-DD；未约定可留空")
                current = text(row["交付状态"])
                choices = DELIVERY_STATES + ([current] if current not in DELIVERY_STATES else [])
                state = st.selectbox("交付状态", choices, key=field_keys["state"])
                actual = st.text_input("实际交付日期", key=field_keys["actual"], placeholder="YYYY-MM-DD")
                link = st.text_input("交付视频链接", key=field_keys["link"])
                saved = st.form_submit_button("保存交付记录")
            if saved:
                due, actual = due.strip(), actual.strip()
                if not valid_date(due) or not valid_date(actual):
                    st.error("日期请填写有效的 YYYY-MM-DD，例如 2026-10-30。")
                else:
                    latest = ensure_delivery_fields(read_csv_file(effect_file).fillna(""))
                    mask = latest["合作ID"] == selected
                    if int(mask.sum()) != 1:
                        st.error("这条合作记录已变化，请刷新后重试。")
                    else:
                        for field, value in {"合作产品": product.strip(), "邮箱": email.strip(),
                                             "约定交付日期": due, "交付状态": state,
                                             "实际交付日期": actual, "视频链接": link.strip()}.items():
                            latest.loc[mask, field] = value
                        write_csv_atomic(latest, effect_file)
                        st.success("交付记录已保存。")
                        st.info("点击“刷新待办清单”更新上方提醒。")

    with st.expander("企微通知内容预览"):
        st.caption("目前仅预览。接通后可按确认的时间推送；电脑或后台服务需要保持运行。")
        lines = ["达人工作台待办", "待回复：%d 条；交付将到期/逾期：%d 条" %
                 (len(reminders["replies"]), len(reminders["deliveries"]))]
        for item in reminders["replies"] + reminders["deliveries"]:
            lines.append("%s｜%s｜%s" % (item["name"], item["product"], item["detail"]))
        st.text("\n".join(lines))
