"""
邮件模块：读取 SMTP 配置 + 各产品邮件模板 + 发信函数
"""
import smtplib, ssl, configparser, re, json
from contextlib import contextmanager
from contextvars import ContextVar
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from functools import wraps
from pathlib import Path
from workbench_io import read_csv_file, write_csv_atomic, csv_transaction
from email_quote_parser import extract_quotes, select_quote

CONFIG_FILE = "email_config.ini"
_LAST_SEND = ContextVar("last_account_send_result", default=None)


class MailSendError(RuntimeError):
    def __init__(self, message, send_result):
        super().__init__(message)
        self.send_result = dict(send_result)


def _account_api():
    import mail_accounts
    return mail_accounts


def _resolve_account(account_id=None, required=False):
    api = _account_api()
    account_id = str((api.current_account_id() if account_id is None else account_id) or "").strip()
    if not account_id:
        if required:
            raise ValueError("请先明确选择发件账号；未归属历史记录仅可查看。")
        return None
    if not api.get_store().get_account(account_id):
        raise ValueError("发件账号不存在，请重新选择。")
    return account_id


def _account_bound(function):
    @wraps(function)
    def wrapped(*args, account_id=None, **kwargs):
        account = _resolve_account(account_id, required=True)
        with _account_api().account_scope(account):
            return function(*args, account_id=account, **kwargs)
    return wrapped


def mail_log_path(account_id=None):
    account = _resolve_account(account_id)
    return str(_account_api().get_store().account_path(account, "邮件库.csv")) if account else MAIL_LOG_FILE

# 合作类型选项
COOP_TYPES = ["独立视频", "加链接", "shorts", "插播"]

# 产品列表（对应邮件模板）
PRODUCTS = ["ReiBoot", "iCareFone WhatsApp transfer", "4DDiG", "4uKey for iOS", "4uKey for Android", "iAnyGo"]

# 各产品邮件模板（{name} = 达人名 占位符）
TEMPLATES = {
    ("ReiBoot", "独立视频"): {
        "subject": "Business Partnership Proposal | YouTube",
        "body": """Hi there,

This is Elva from Tenorshare's partnerships team. Tenorshare provides software solutions to millions of users in over 100 countries.

We're currently looking for YouTube creators for a paid dedicated-video campaign. We'd love to sponsor a full video on your channel featuring Tenorshare ReiBoot and provide a sponsorship fee for your work.

Tenorshare ReiBoot is an iOS system repair tool that helps users:

• Fix 150+ iOS and iPadOS system issues, including devices stuck on the Apple logo, black screen, boot loop, and recovery mode

• Enter or exit recovery mode with one click

• Upgrade or downgrade iOS and iPadOS without iTunes or jailbreaking

Product page: https://www.tenorshare.com/products/reiboot.html

We'll provide a free license, key talking points, and technical support. You're welcome to present the product in your own style and tailor the content to your audience.

If you're interested, please reply with:

Your rate for a dedicated video

Your earliest available publishing date

Your typical production timeline

Any questions or collaboration ideas

Looking forward to hearing from you!

Best regards,
Elva""",
    },
    ("ReiBoot", "加链接"): {
        "subject": "Business Partnership Proposal | YouTube",
        "body": """Hi there,

I'm Elva from Tenorshare, a global leading software company.

After watching your video XXX, we'd like to invite you to work with us on a paid partnership.

We'd like to pay for adding a short introduction and product link to the description. No new video production is required.

Tenorshare ReiBoot is a professional iOS system repair tool that helps users:

● Fix common iPhone/iPad system issues without data loss
● Resolve problems such as iPhone stuck on Apple logo, boot loops, or recovery mode
● Repair iOS system issues and restore devices to normal without complicated steps

Product page: https://www.tenorshare.com/products/reiboot.html

Would you be interested? If you're interested, please include the following details in your reply:

Your rate: Price for adding the promotion to the video description

Availability: The earliest date you can add the placement

Placement duration: How long the link can remain live

Other details: Any requirements, suggestions, or collaboration ideas you may have

Looking forward to hearing from you!

Best regards,
Elva
Tenorshare""",
    },
    ("iCareFone WhatsApp transfer", "独立视频"): {
        "subject": "Business Partnership Proposal | YouTube",
        "body": """Hi {name},

I hope you're doing well! I'm reaching out from Tenorshare, the team behind iCareFone WhatsApp Transfer.

We love your YouTube channel and think your audience would benefit from learning how to transfer WhatsApp chats between Android and iPhone without factory reset.

We'd love to invite you to make a video about iCareFone WhatsApp Transfer. What we offer:
- Free full license for you (and codes for your audience)
- Competitive payment for the video
- A dedicated contact to support you throughout

Video requirements:
- Title: include a strong keyword (e.g., "How to Transfer WhatsApp from Android to iPhone Without Factory Reset")
- Length: 5-8 minutes
- Show the problem first (manual transfer fails), then our one-click solution

If you're interested, just reply to this email and I'll send the full details, license code, and payment terms.

Looking forward to working with you!

Best regards,
Ella
Tenorshare Partnership Team""",
    },
    ("4DDiG", "独立视频"): {
        "subject": "Business Partnership Proposal | YouTube",
        "body": """Hi {name},

I hope you're doing well! I'm reaching out from Tenorshare, the team behind 4DDiG Data Recovery.

We love your YouTube channel and think your audience would benefit from learning how to recover deleted files with 4DDiG.

We'd love to invite you to make a video about 4DDiG. What we offer:
- Free full license for you (and codes for your audience)
- Competitive payment for the video
- A dedicated contact to support you throughout

Video requirements:
- Title: include "4DDiG" + your target keyword (e.g., "How to Recover Deleted Files - Tenorshare 4DDiG")
- Length: 5-8 minutes
- Show a free (but limited) recovery method first, then 4DDiG's full solution

If you're interested, just reply to this email and I'll send the full details, license code, and payment terms.

Looking forward to working with you!

Best regards,
Ella
Tenorshare Partnership Team""",
    },
}

def load_smtp_config(account_id=None):
    account = _resolve_account(account_id, required=True)
    return dict(_account_api().get_store().get_credentials(account))


def _load_imap_config(account_id=None):
    account = _resolve_account(account_id, required=True)
    profile = _account_api().get_store().get_account(account)
    if not profile.get("enabled", True) or profile.get("imap_ready") is False:
        raise ValueError("该账号已停用或尚未通过IMAP验证，未连接邮箱。")
    cfg = load_smtp_config(account)
    if cfg.get("imap_ready") is False:
        raise ValueError("该账号尚未通过IMAP验证。")
    return cfg

# Compatibility only: defaults come exclusively from the selected account.
CC_RECIPIENTS = []

def clean_email(e):
    """清洗邮箱：从 mailto:/http:// 等格式里提取纯邮箱地址"""
    import re
    m = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', str(e))
    return m.group(1) if m else str(e).strip()

@_account_bound
def send_email(to_email, subject, body, cc=None, *, account_id=None, invitation_id=None,
               message_id=None, in_reply_to=None, references=None, template_version=None,
               contact_id=None, template_key=None, purpose="首次邀约", expected_account_revision=None):
    """Send through one explicit account and return a truthy send receipt."""
    account_id = _resolve_account(account_id, required=True)
    _LAST_SEND.set(None)
    cfg = load_smtp_config(account_id)
    if expected_account_revision is not None and str(cfg.get("revision", "")) != str(expected_account_revision):
        raise ValueError("发件账号配置已变化，请重新预览并确认后发送。")
    if cfg.get("smtp_ready") is False:
        raise ValueError("该账号尚未通过SMTP验证，请先验证后发送。")
    sender_name = cfg.get("sender_name", cfg.get("name", ""))
    to_email = clean_email(to_email)
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", to_email):
        raise ValueError("收件邮箱格式无效")
    if not isinstance(subject, str) or not isinstance(body, str) or not subject.strip() or not body.strip():
        raise ValueError("邮件主题和正文不能为空")
    cc = cfg.get("cc", []) if cc is None else cc
    if isinstance(cc, str):
        cc = re.split(r"[,;\s]+", cc)
    cc = list(dict.fromkeys(clean_email(value) for value in (cc or []) if str(value).strip()))
    if any(not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value) for value in cc):
        raise ValueError("抄送邮箱格式无效")
    for header in (subject, sender_name, cfg.get("email", ""), message_id or "", in_reply_to or ""):
        if "\r" in str(header) or "\n" in str(header):
            raise ValueError("邮件头格式无效")
    message_id = message_id or make_msgid(domain=cfg["email"].rsplit("@", 1)[-1])
    if not re.fullmatch(r"<[^<>\s]+>", message_id):
        raise ValueError("Message-ID格式无效")
    if isinstance(references, (list, tuple)):
        references = " ".join(references)
    references = references or in_reply_to or ""
    if "\r" in references or "\n" in references:
        raise ValueError("References格式无效")
    context = ssl.create_default_context()
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, cfg["email"]))
    msg["To"] = to_email
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=False)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if cc:
        msg["Cc"] = ", ".join(cc)
    recipients = list(dict.fromkeys([to_email] + cc))
    receipt = {"account_id": account_id, "from_email": cfg["email"], "to_email": to_email,
               "message_id": message_id, "invitation_id": invitation_id or message_id,
               "contact_id": contact_id or "", "subject": subject, "body": body,
               "in_reply_to": in_reply_to or "", "references": references,
               "template_version": template_version if template_version is not None else "", "template_key": template_key or "",
               "purpose": purpose, "account_revision": cfg.get("revision"), "cc": cc}
    send_started, accepted = False, False
    try:
        with smtplib.SMTP_SSL(cfg["server"], int(cfg["port"]), context=context, timeout=25) as s:
            s.login(cfg["email"], cfg["auth_code"])
            send_started = True
            refused = s.sendmail(cfg["email"], recipients, msg.as_string())
            if to_email in (refused or {}):
                send_started = False
                raise RuntimeError("recipient refused")
            accepted = True
    except Exception as error:
        if not accepted:
            rejected = isinstance(error, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError))
            uncertain = send_started and not rejected
            status = "发送待核" if uncertain else "失败"
            failed_receipt = dict(receipt, status=status)
            _LAST_SEND.set(failed_receipt)
            message = ("邮件发送结果待核，请先核对所选账号发件箱，确认前不要重复发送。" if uncertain else
                       "邮件未发送成功，请检查所选账号配置后再处理。")
            raise MailSendError(message, failed_receipt) from None
    _LAST_SEND.set(dict(receipt, status="成功"))
    return dict(receipt, status="成功")

def render_template(product, coop_type, name="", video_link="", *, purpose="首次邀约",
                    account_id=None, values=None, return_metadata=False):
    """Strict product × form × purpose lookup. No cross-form fallback."""
    from template_store import get_template_store
    store = get_template_store()
    template = store.get_template(product, coop_type, purpose=purpose)
    if not template:
        return None if return_metadata else (None, None)
    account = _resolve_account(account_id)
    cfg = _account_api().get_store().get_account(account) if account else {}
    sender_name = cfg.get("sender_name", cfg.get("name", ""))
    fields = {"name": name, "达人名": name, "产品名称": product, "产品名": product, "product": product,
              "合作形式": coop_type, "coop_type": coop_type, "视频链接": video_link,
              "video_link": video_link, "发件人姓名": sender_name,
              "发件邮箱": cfg.get("email", ""), "sender_name": sender_name,
              "sender_email": cfg.get("email", "")}
    fields.update(values or {})
    rendered = store.render(product, coop_type, purpose=purpose, values=fields)
    return rendered if return_metadata else (rendered["subject"], rendered["body"])

# ============ 邮件库（发送记录） ============
import os as _os

MAIL_LOG_FILE = "邮件库.csv"

@_account_bound
def log_sent_email(email, name, product, coop_type, status, error="", *, account_id=None,
                   send_result=None, invitation_id=None, contact_id=None, message_id=None,
                   subject=None, body=None, template_version=None, in_reply_to=None, references=None):
    """把一封已发送的邮件记录到邮件库"""
    import pandas as pd
    from datetime import datetime
    path = mail_log_path(account_id)
    receipt = send_result if send_result is not None else _LAST_SEND.get()
    if receipt is not None:
        if not isinstance(receipt, dict) or receipt.get("account_id") != account_id or clean_email(email).lower() != receipt.get("to_email", "").lower():
            raise ValueError("发送结果与当前账号或收件人不一致，请勿跨账号记录。")
        receipt = dict(receipt)
    else:
        receipt = {}
    if status == "成功" and not (receipt.get("message_id") or message_id):
        raise ValueError("缺少实际发送Message-ID，不能记录为成功。")
    if receipt.get("status") and receipt["status"] != status:
        raise ValueError("发送结果与记录状态不一致。")
    cfg = _account_api().get_store().get_account(account_id)
    row = pd.DataFrame([{
        "发送时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "频道名称": name,
        "邮箱": email,
        "合作产品": product,
        "合作类型": coop_type,
        "状态": status,
        "错误": error,
        "回信次数": 0,
        "回信状态": "从未回信",
        "account_id": account_id, "发件账号ID": account_id,
        "实际发件地址": receipt.get("from_email", cfg["email"]),
        "邀约ID": invitation_id or receipt.get("invitation_id", ""),
        "联系人ID": contact_id or receipt.get("contact_id", ""),
        "邮件Message-ID": message_id or receipt.get("message_id", ""),
        "邮件主题": subject if subject is not None else receipt.get("subject", ""),
        "邮件正文": body if body is not None else receipt.get("body", ""),
        "模板版本": template_version if template_version is not None else receipt.get("template_version", ""),
        "模板键": receipt.get("template_key", ""), "模板用途": receipt.get("purpose", ""),
        "邮件类型": receipt.get("purpose", "首次邀约"),
        "In-Reply-To": in_reply_to if in_reply_to is not None else receipt.get("in_reply_to", ""),
        "References": references if references is not None else receipt.get("references", ""),
        "实际抄送": json.dumps(receipt.get("cc", []), ensure_ascii=False),
    }])
    with csv_transaction(path):
        if _os.path.exists(path):
            existing = load_mail_log(account_id)
            row = pd.concat([existing, row], ignore_index=True)
        write_csv_atomic(row, path)
    _LAST_SEND.set(None)
    return row.iloc[-1].to_dict()

def load_mail_log(account_id=None):
    import pandas as pd
    account = _resolve_account(account_id)
    path = mail_log_path(account if account else "")
    frame = read_csv_file(path) if _os.path.exists(path) else pd.DataFrame()
    if account and not frame.empty:
        for column in ("account_id", "发件账号ID"):
            if column in frame:
                identities = frame[column].fillna("").astype(str).str.strip()
                if not identities.isin(["", account]).all():
                    raise ValueError("邮件记录账号归属不一致，请先核对。")
            frame[column] = account
    return frame


def send_and_log(to_email, subject, body, *, account_id=None, name="", product="", coop_type="", **metadata):
    """Keep the actual SMTP receipt and durable log bound to one account."""
    account = _resolve_account(account_id, required=True)
    with _account_api().account_scope(account):
        try:
            receipt = send_email(to_email, subject, body, account_id=account, **metadata)
        except Exception as error:
            receipt = getattr(error, "send_result", None)
            state = receipt.get("status", "失败") if receipt else "失败"
            log_sent_email(to_email, name, product, coop_type, state,
                           "发送结果待核，请核对发件箱" if state == "发送待核" else "发送失败",
                           account_id=account, send_result=receipt)
            raise
        try:
            log_sent_email(to_email, name, product, coop_type, "成功", account_id=account, send_result=receipt)
        except Exception:
            pending_receipt = dict(receipt, status="发送待核", smtp_accepted=True)
            _LAST_SEND.set(pending_receipt)
            raise MailSendError("邮件已被服务器接收，但记录未保存；请先核对发件箱，不要重复发送。", pending_receipt) from None
        return receipt

# ============ 回信同步（IMAP） ============
IMAP_TIMEOUT = 30
HANDLED_REPLY_STATUSES = frozenset({"已处理", "已回复", "已拒绝", "已合作"})


def _reply_count(value):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@_account_bound
def mark_replies_handled(row_keys, status="已处理", seen_counts=None, account_id=None):
    """Acknowledge only replies the operator saw, using stable invitation keys.

    Reload before writing so rows added by a send/sync are retained. If the UI
    supplied a count and a later reply is now present, that reply stays pending.
    """
    if status not in HANDLED_REPLY_STATUSES:
        raise ValueError("Unsupported handled reply status")
    if isinstance(row_keys, dict) and seen_counts is None:
        seen_counts = row_keys
    keys = {tuple(key) for key in row_keys}
    if not keys:
        return 0
    path = mail_log_path(account_id)
    with csv_transaction(path):
        log = load_mail_log()
        changed = 0
        for i, row in log.iterrows():
            key = _invitation_row_key(row)
            if key not in keys:
                continue
            current = _reply_count(row.get("回信次数", 0))
            seen = _reply_count(seen_counts[key]) if seen_counts is not None and key in seen_counts else current
            baseline = max(_reply_count(row.get("已处理回信次数", 0)), seen)
            log.at[i, "已处理回信次数"] = baseline
            log.at[i, "回信处理时间"] = _utc_now()
            log.at[i, "回信状态"] = f"已回信待处理（第{current}次）" if current > baseline else status
            changed += 1
        if changed:
            write_csv_atomic(log, path)
        return changed


def _apply_reply_state(log, index, row, count, empty_status, concurrent_manual=False):
    state = _invitation_value(row.get("回信状态", ""))
    if state in HANDLED_REPLY_STATUSES:
        raw_baseline = _invitation_value(row.get("已处理回信次数", ""))
        baseline = _reply_count(raw_baseline) if raw_baseline else _reply_count(row.get("回信次数", 0))
        # Older UIs have no explicit seen watermark. Preserve an operator action
        # made during this scan; newer UIs supply the exact count they saw.
        if not raw_baseline and concurrent_manual:
            baseline = max(baseline, count)
        log.at[index, "已处理回信次数"] = baseline
        if count <= baseline:
            return
    log.at[index, "回信状态"] = f"已回信待处理（第{count}次）" if count else empty_status

@_account_bound
def sync_replies(account_id=None):
    """连接 IMAP，统计每个已联系达人的回信次数，返回 {邮箱: 次数}"""
    import imaplib, re
    from email import message_from_bytes
    import pandas as pd

    cfg = _load_imap_config(account_id)
    log = load_mail_log()
    if log.empty or "邮箱" not in log.columns:
        return {}
    target_emails = set(str(e).strip().lower() for e in log["邮箱"].dropna() if str(e).strip())
    if not target_emails:
        return {}

    counts = {e: 0 for e in target_emails}
    imap = None
    try:
        imap = imaplib.IMAP4_SSL(cfg["imap_server"], int(cfg.get("imap_port", 993)), timeout=IMAP_TIMEOUT)
        imap.login(cfg["email"], cfg["auth_code"])
        imap.select("INBOX", readonly=True)
        status, data = imap.search(None, "ALL")
        if status == "OK" and data and data[0]:
            ids = data[0].split()
            for i in range(0, len(ids), 50):
                batch = ids[i:i+50]
                idset = ",".join(b.decode() for b in batch)
                typ, msgs = imap.fetch(idset, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                if typ != "OK":
                    continue
                for resp in msgs:
                    if isinstance(resp, tuple):
                        msg = message_from_bytes(resp[1])
                        frm = msg.get("From", "")
                        m = re.search(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', frm)
                        if m:
                            addr = m.group(0).lower()
                            if addr in target_emails:
                                counts[addr] += 1
    except Exception as e:
        print(f"IMAP 同步失败（{type(e).__name__}）")
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass
    return counts


@_account_bound
def update_reply_status(strict=False, account_id=None):
    """同步回信，更新邮件库的「回信次数」「回信状态」「对方报价」，返回同步的达人数"""
    import pandas as pd
    log = load_mail_log()
    if "状态" in log:
        log = log[log["状态"].fillna("").isin(["", "成功"])].copy()
    if log.empty:
        return 0
    scan_rows = {_invitation_row_key(row): row.copy() for _, row in log.iterrows()}
    threaded = log.apply(_is_threaded_invitation, axis=1)
    # 旧记录保留按邮箱汇总的行为；新批次绝不能回退到旧线程。
    legacy_failed = False
    # This scope is lazy: mocked helpers do not connect, real helpers share one
    # search/header snapshot and body cache. Never hold the CSV lock over IMAP.
    with _using_mailbox_snapshot(log["邮箱"].tolist()):
        try:
            details = (extract_reply_quotes(raise_on_error=True) if strict else extract_reply_quotes()) if (~threaded).any() else {}
        except Exception:
            legacy_failed = True
            details = {}
        thread_details = extract_thread_reply_quotes(log[threaded]) if threaded.any() else {}
    thread_failed = threaded.any() and thread_details is None
    if not details and not threaded.any():
        if strict and legacy_failed:
            raise RuntimeError("回信同步未完成") from None
        return 0
    # 网络扫描期间发信进程可能追加记录；结果先绑定到邀约身份，再读最新日志。
    thread_by_invitation = {
        _invitation_row_key(row): thread_details[i]
        for i, row in log[threaded].iterrows()
        if thread_details is not None and i in thread_details
    }
    path = mail_log_path(account_id)
    with csv_transaction(path):
        log = load_mail_log()
        if log.empty:
            if strict and (legacy_failed or thread_failed):
                raise RuntimeError("回信同步未完成") from None
            return 0
        threaded = log.apply(_is_threaded_invitation, axis=1)
        if "回信次数" not in log.columns:
            log["回信次数"] = ""
        if "回信状态" not in log.columns:
            log["回信状态"] = ""
        if "对方报价" not in log.columns:
            log["对方报价"] = ""
        for i, row in log.iterrows():
            if _is_outgoing_followup(row):
                continue
            if threaded.at[i]:
                if thread_details is None:  # 同步失败时不覆盖已确认的新线程结果。
                    continue
                key = _invitation_row_key(row)
                if key not in thread_by_invitation:
                    continue  # 本次扫描开始后新增的邀约完全保留，不当作未回信。
                d = thread_by_invitation[key]
                n, q = _reply_count(d["次数"]), d["报价"]
                previous = scan_rows.get(key, {})
                concurrent_manual = str(row.get("回信状态", "")) != str(previous.get("回信状态", ""))
                _apply_reply_state(log, i, row, n, "本轮待回信", concurrent_manual)
                log.at[i, "回信次数"] = n
                metadata = getattr(thread_details, "metadata", {}).get(key, {})
                _write_reply_metadata(log, i, row, q, metadata, exact_thread=True)
                continue
            if not details:
                continue
            e = str(row.get("邮箱", "")).strip().lower()
            key = _invitation_row_key(row)
            if key not in scan_rows:
                continue
            d = details.get(e, {"次数": 0, "报价": None})
            n = _reply_count(d["次数"])
            q = d["报价"]
            concurrent_manual = str(row.get("回信状态", "")) != str(scan_rows[key].get("回信状态", ""))
            _apply_reply_state(log, i, row, n, "从未回信", concurrent_manual)
            log.at[i, "回信次数"] = n
            metadata = getattr(details, "metadata", {}).get(e, {})
            _write_reply_metadata(log, i, row, q, metadata, exact_thread=False)
        write_csv_atomic(log, path)
        if strict and (legacy_failed or thread_failed):
            raise RuntimeError("回信同步未完成") from None
        return len(details) + len(thread_details or {})


def synchronize_latest_replies(strict=True, account_id=None):
    """Explicit refresh entry point; existing UI/worker can keep using update."""
    return update_reply_status(strict=strict, account_id=account_id)


def _write_reply_metadata(log, index, previous, scalar_quote, metadata, exact_thread):
    proposals = None
    quote_detail = metadata.get("报价明细", metadata.get("报价明细JSON"))
    if quote_detail is not None:
        try:
            proposals = json.loads(quote_detail)
        except (ValueError, TypeError):
            proposals = []
    selected = select_quote(proposals, _invitation_value(previous.get("合作类型", ""))) if proposals else None
    if proposals is not None:
        scalar_quote = selected.get("amount") if selected else None
    confirmed_same_thread = bool(
        exact_thread and _invitation_value(previous.get("报价来源邮件Message-ID", ""))
        and _invitation_value(previous.get("报价线程Message-ID", "")) == _invitation_value(previous.get("邮件Message-ID", ""))
        and _invitation_value(previous.get("邮件Message-ID", "")))
    keep_confirmed = confirmed_same_thread and not proposals and scalar_quote is None
    for column, value in metadata.items():
        if keep_confirmed and column.startswith("报价") and column != "报价匹配方式":
            continue
        log.at[index, "报价明细" if column == "报价明细JSON" else column] = value
    if scalar_quote is not None:
        log.at[index, "对方报价"] = scalar_quote
        log.at[index, "报价币种"] = selected.get("currency", "USD") if selected else "USD"
        if selected:
            log.at[index, "报价合作类型"] = selected.get("coop_type", "")
    elif keep_confirmed:
        log.at[index, "报价解析状态"] = "保留本线程已确认报价；本次未发现新报价"
    elif proposals:
        log.at[index, "对方报价"] = ""
        log.at[index, "报价币种"] = ""
    elif exact_thread:
        log.at[index, "对方报价"] = ""
    elif _invitation_value(previous.get("对方报价", "")):
        log.at[index, "报价解析状态"] = "保留历史报价（本次未解析，来源未核）"


def _invitation_value(value):
    text = str(value).strip()
    return "" if text.lower() in ("", "nan", "none", "<na>") else text


def _is_threaded_invitation(row):
    return bool(_invitation_value(row.get("邮件Message-ID", ""))
                or _invitation_value(row.get("邀约批次", "")))


def _is_outgoing_followup(row):
    return any(_invitation_value(row.get(field, "")) in {"议价回复", "接受报价"}
               for field in ("邮件类型", "模板用途", "合作类型"))


def _invitation_row_key(row, account_id=None):
    account = (_invitation_value(row.get("account_id", "")) or
               _invitation_value(row.get("发件账号ID", "")) or account_id or
               _account_api().current_account_id() or "legacy-unassigned")
    address = _invitation_value(row.get("邮箱", "")).lower()
    message_id = _invitation_value(row.get("邮件Message-ID", ""))
    if message_id:
        return ("account", account, "message-id", address, message_id)
    return ("account", account, "batch-only", address, _invitation_value(row.get("邀约批次", "")),
            _invitation_value(row.get("发送时间", "")))


def _message_id_tokens(value):
    """按完整 Message-ID 匹配，不做子串或按发件人降级匹配。"""
    return set(re.findall(r"<[^<>\s]+>", str(value or "")))


def _current_reply_body(msg):
    """只解析当前回信正文；遇到引用/转发边界即停止，宁缺报价勿读旧报价。"""
    from html import unescape
    parts = [p for p in msg.walk() if not p.is_multipart()
             and p.get_content_disposition() != "attachment"]
    part = next((p for p in parts if p.get_content_type() == "text/plain"), None)
    if part is None:
        part = next((p for p in parts if p.get_content_type() == "text/html"), None)
    if part is None:
        return ""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    try:
        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        body = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        # Gmail / Outlook / Yahoo / Thunderbird 常见的引用块起点。
        boundary = re.search(
            r"<blockquote\b|<(?:div|span|section)\b[^>]*(?:gmail_quote|gmail_attr|"
            r"yahoo_quoted|divRplyFwdMsg|moz-cite-prefix)", body, re.I)
        if boundary:
            body = body[:boundary.start()]
        body = re.sub(r"<br\s*/?>|</(?:p|div|li|tr)\s*>", "\n", body, flags=re.I)
        body = unescape(re.sub(r"<[^>]+>", "", body))
    # 某些客户端将 On ... wrote: 折行，引用正文也可能不带 >。
    boundary = re.search(r"^[ \t]*(?:On[ \t]+.{0,600}?\bwrote[ \t]*:|"
                         r"在.{0,600}?写道[：:])", body, re.I | re.M | re.S)
    if boundary:
        body = body[:boundary.start()]
    lines = []
    for line in body.splitlines():
        if re.match(r"^\s*(?:>|On\b.*\bwrote\s*:|在.*写道[：:]|"
                    r"[-_]{2,}\s*(?:Original Message|Forwarded message|原始邮件|转发邮件)|"
                    r"(?:From|Sent|发件人|发送时间)\s*[：:])", line, re.I):
            break
        lines.append(line)
    return "\n".join(lines).strip()


class _ThreadReplyResults(dict):
    """Keep existing count/quote results, carrying task metadata by stable key."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.metadata = {}


_ACTIVE_MAILBOX_SNAPSHOT = ContextVar("active_mailbox_snapshot", default=None)


class _MailboxSnapshot:
    """One read-only IMAP session, filtered headers and lazy cached MIME bodies."""
    HEADER_QUERY = "(BODY.PEEK[HEADER.FIELDS (FROM DATE IN-REPLY-TO REFERENCES SUBJECT MESSAGE-ID)])"

    def __init__(self, addresses, account_id=None):
        self.account_id = _resolve_account(account_id, required=True)
        from email.utils import parseaddr
        self.addresses = sorted({parseaddr(str(a))[1].lower().strip() for a in addresses if parseaddr(str(a))[1]})
        self.imap = None
        self._headers = None
        self._bodies = {}
        self._failed = False
        self.used_all_fallback = False

    def close(self):
        if self.imap is not None:
            try:
                self.imap.logout()
            except Exception:
                pass
            self.imap = None

    def _connect(self):
        import imaplib
        if self.imap is None:
            cfg = _load_imap_config(self.account_id)
            self.imap = imaplib.IMAP4_SSL(cfg["imap_server"], int(cfg.get("imap_port", 993)), timeout=IMAP_TIMEOUT)
            self.imap.login(cfg["email"], cfg["auth_code"])
            status, _ = self.imap.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("回信同步未完成")

    @staticmethod
    def _from_query(addresses):
        terms = ["FROM " + json.dumps(a, ensure_ascii=True) for a in addresses]
        query = terms[-1]
        for term in reversed(terms[:-1]):
            query = "OR " + term + " (" + query + ")"
        return query

    def headers(self):
        from email import message_from_bytes
        from email.utils import parseaddr, parsedate_to_datetime
        import imaplib
        if self._failed:
            raise RuntimeError("回信同步未完成")
        if self._headers is not None:
            return self._headers
        try:
            if not self.addresses:
                self._headers = {}
                return self._headers
            self._connect()
            mailbox_ids = set()
            for start in range(0, len(self.addresses), 20):
                query = self._from_query(self.addresses[start:start + 20])
                try:
                    status, data = self.imap.search(None, query)
                except imaplib.IMAP4.error:
                    status, data = "BAD", []
                if status != "OK":
                    self.used_all_fallback = True
                    status, data = self.imap.search(None, "ALL")
                    if status != "OK":
                        raise RuntimeError("回信同步未完成")
                    mailbox_ids = set(data[0].split() if data and data[0] else [])
                    break
                mailbox_ids.update(data[0].split() if data and data[0] else [])
            mailbox_ids = sorted((value.decode("ascii") for value in mailbox_ids), key=int)
            headers = {}
            for start in range(0, len(mailbox_ids), 50):
                batch = mailbox_ids[start:start + 50]
                status, messages = self.imap.fetch(",".join(batch), self.HEADER_QUERY)
                if status != "OK":
                    raise RuntimeError("回信同步未完成")
                received = set()
                for response in messages:
                    if not isinstance(response, tuple) or len(response) < 2:
                        continue
                    mailbox_id = response[0].split()[0].decode("ascii")
                    received.add(mailbox_id)
                    msg = message_from_bytes(response[1])
                    address = parseaddr(msg.get("From", ""))[1].strip().lower()
                    if address not in self.addresses:
                        continue
                    try:
                        date = parsedate_to_datetime(msg.get("Date", ""))
                        timestamp = date.timestamp() if date.tzinfo is not None else 0
                    except (TypeError, ValueError, OverflowError):
                        timestamp = 0
                    headers[mailbox_id] = {"mailbox_id": mailbox_id, "message": msg,
                        "address": address, "timestamp": timestamp,
                        "refs": _message_id_tokens(msg.get("In-Reply-To", "")) | _message_id_tokens(msg.get("References", ""))}
                if set(batch) - received:
                    raise RuntimeError("回信同步未完成")
            self._headers = headers
            return headers
        except Exception:
            self._failed = True
            raise RuntimeError("回信同步未完成") from None

    def body(self, mailbox_id):
        from email import message_from_bytes
        if self._failed:
            raise RuntimeError("回信同步未完成")
        if mailbox_id not in self._bodies:
            try:
                status, messages = self.imap.fetch(mailbox_id, "(BODY.PEEK[])")
                if status != "OK":
                    raise RuntimeError("回信同步未完成")
                msg = next((message_from_bytes(item[1]) for item in messages
                            if isinstance(item, tuple) and len(item) >= 2), None)
                if msg is None:
                    raise RuntimeError("回信同步未完成")
                self._bodies[mailbox_id] = msg
            except Exception:
                self._failed = True
                raise RuntimeError("回信同步未完成") from None
        return self._bodies[mailbox_id]


@contextmanager
def _using_mailbox_snapshot(addresses, snapshot=None, account_id=None):
    account = _resolve_account(account_id, required=True)
    existing = snapshot or _ACTIVE_MAILBOX_SNAPSHOT.get()
    if existing is not None and getattr(existing, "account_id", None) != account:
        raise ValueError("回信快照不属于当前账号，已停止同步。")
    owner = existing is None
    current = existing if existing is not None else _MailboxSnapshot(addresses, account_id=account)
    token = _ACTIVE_MAILBOX_SNAPSHOT.set(current)
    try:
        yield current
    finally:
        _ACTIVE_MAILBOX_SNAPSHOT.reset(token)
        if owner:
            current.close()


def _decoded_subject(msg):
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(msg.get("Subject", ""))))
    except (LookupError, UnicodeError, ValueError):
        return str(msg.get("Subject", ""))


def _timestamp_iso(timestamp):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else ""


def _summarize_matching_replies(matches, snapshot, coop_type="", match_mode="线程精确匹配"):
    ordered = sorted(matches, key=lambda item: (item["timestamp"], int(item["mailbox_id"])), reverse=True)
    if not ordered:
        return None, {}
    latest = ordered[0]
    latest_msg = snapshot.body(latest["mailbox_id"])
    latest_body = _current_reply_body(latest_msg)
    metadata = {"最近回信正文": latest_body, "最近回信摘要": latest_body[:200],
        "最近回信主题": _decoded_subject(latest_msg),
        "最近回信Message-ID": str(latest_msg.get("Message-ID", "")),
        "最近回信时间": _timestamp_iso(latest["timestamp"]), "报价匹配方式": match_mode}
    for record in ordered:
        message = snapshot.body(record["mailbox_id"])
        body = _current_reply_body(message)
        proposals = extract_quotes(body)
        if not proposals:
            continue
        selected = select_quote(proposals, coop_type)
        metadata.update({"报价明细": json.dumps(proposals, ensure_ascii=False),
            "报价来源正文": body,
            "报价来源邮件Message-ID": str(message.get("Message-ID", "")),
            "报价来源时间": _timestamp_iso(record["timestamp"]),
            "报价来源原文": "\n".join(dict.fromkeys(p.get("context", "") for p in proposals)),
            "报价币种": selected.get("currency", "") if selected else "",
            "报价合作类型": selected.get("coop_type", "") if selected else "",
            "报价解析状态": "明确报价" if selected else "多方案、区间或币种用途待核",
            "报价来自最新回信": "是" if record["mailbox_id"] == latest["mailbox_id"] else "否"})
        return selected.get("amount") if selected else None, metadata
    metadata.update({"报价明细": "[]", "报价解析状态": "本次未找到明确报价"})
    return None, metadata


@_account_bound
def extract_thread_reply_quotes(rows, snapshot=None, account_id=None):
    """Exact sender and thread references; optional shared read-only snapshot."""
    originals = rows[~rows.apply(_is_outgoing_followup, axis=1)].copy() if not rows.empty else rows
    result = _ThreadReplyResults({i: {"次数": 0, "报价": None} for i in originals.index})
    targets = {}
    invitation_ids = {}
    for i, row in rows.iterrows():
        row_account = _invitation_value(row.get("account_id", "")) or _invitation_value(row.get("发件账号ID", ""))
        if row_account and row_account != account_id:
            raise ValueError("回信记录不属于当前账号，已停止同步。")
    for i, row in originals.iterrows():
        raw_id = _invitation_value(row.get("邮件Message-ID", ""))
        ids = _message_id_tokens(raw_id)
        if not ids and re.fullmatch(r"[^\s<>]+@[^\s<>]+", raw_id):
            ids = {"<" + raw_id + ">"}
        address = _invitation_value(row.get("邮箱", "")).lower()
        invitation = _invitation_value(row.get("邀约ID", ""))
        if address and invitation:
            invitation_ids.setdefault((address, invitation), set()).add(i)
        for message_id in ids:
            if address:
                targets.setdefault((address, message_id), []).append(i)
    outgoing = [row for _, row in rows.iterrows() if _is_outgoing_followup(row)
                and _invitation_value(row.get("状态", "")) in ("", "成功")]
    # A later reply may reference only our latest outgoing followup. Extend
    # exact identities to its original invitation; never use sender-only links.
    for _ in range(len(outgoing)):
        changed = False
        for row in outgoing:
            address = _invitation_value(row.get("邮箱", "")).lower()
            ids = _message_id_tokens(_invitation_value(row.get("邮件Message-ID", "")))
            refs = _message_id_tokens(row.get("References", "")) | _message_id_tokens(row.get("In-Reply-To", ""))
            exact = {i for ref in refs for i in targets.get((address, ref), [])}
            linked = invitation_ids.get((address, _invitation_value(row.get("邀约ID", ""))), set())
            candidates = exact & linked if exact and linked else exact or linked
            if len(candidates) != 1:
                continue
            for mid in ids:
                key = (address, mid)
                if key not in targets:
                    targets[key] = list(candidates)
                    changed = True
        if not changed:
            break
    if not targets:
        return result
    try:
        with _using_mailbox_snapshot([key[0] for key in targets], snapshot) as mailbox:
            matched = {i: [] for i in originals.index}
            for record in mailbox.headers().values():
                row_ids = {i for ref in record["refs"] for i in targets.get((record["address"], ref), [])}
                for i in row_ids:
                    matched[i].append(record)
            for i, records in matched.items():
                result[i]["次数"] = len(records)
                if not records:
                    continue
                q, metadata = _summarize_matching_replies(records, mailbox, _invitation_value(rows.loc[i].get("合作类型", "")))
                result[i]["报价"] = q
                metadata["报价线程Message-ID"] = _invitation_value(rows.loc[i].get("邮件Message-ID", ""))
                result.metadata[_invitation_row_key(rows.loc[i])] = metadata
        return result
    except Exception as error:
        print(f"新邀约回信同步失败（{type(error).__name__}）")
        return None


def extract_quote(text):
    """Compatibility scalar: only one unambiguous USD proposal, never a range."""
    selected = select_quote(extract_quotes(text or ""))
    return selected.get("amount") if selected and selected.get("currency") == "USD" else None


def _get_body(msg):
    """从 email message 提取纯文本正文（处理 MIME 编码）"""
    import re
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    text = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                    return re.sub(r'<[^>]+>', ' ', text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
    return ""


@_account_bound
def extract_reply_quotes(raise_on_error=False, snapshot=None, account_id=None):
    """Legacy sender-only matching, explicitly marked as historical/unverified."""
    log = load_mail_log()
    if not log.empty:
        log = log[~log.apply(_is_outgoing_followup, axis=1)].copy()
    if log.empty or "邮箱" not in log.columns:
        return _ThreadReplyResults()
    addresses = {_invitation_value(value).lower() for value in log["邮箱"] if _invitation_value(value)}
    result = _ThreadReplyResults({address: {"次数": 0, "报价": None} for address in addresses})
    if not addresses:
        return result
    try:
        with _using_mailbox_snapshot(addresses, snapshot) as mailbox:
            matched = {address: [] for address in addresses}
            for record in mailbox.headers().values():
                if record["address"] in matched:
                    matched[record["address"]].append(record)
            for address, records in matched.items():
                result[address]["次数"] = len(records)
                if not records:
                    continue
                q, metadata = _summarize_matching_replies(records, mailbox,
                    match_mode="按邮箱匹配（历史，仅当前账号，合作产品待核）")
                result[address]["报价"] = q
                metadata["报价线程Message-ID"] = ""
                result.metadata[address] = metadata
        return result
    except Exception as error:
        print(f"提取报价失败（{type(error).__name__}）")
        if raise_on_error:
            raise RuntimeError("回信同步未完成") from None
        # A failed scan must not overwrite confirmed data with zeroes.
        return {}
