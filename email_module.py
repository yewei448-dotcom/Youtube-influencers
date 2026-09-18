"""
邮件模块：读取 SMTP 配置 + 各产品邮件模板 + 发信函数
"""
import smtplib, ssl, configparser
from email.mime.text import MIMEText

CONFIG_FILE = "email_config.ini"

# 合作类型选项
COOP_TYPES = ["独立视频", "加链接", "shorts", "插播"]

# 产品列表（对应邮件模板）
PRODUCTS = ["ReiBoot", "iCareFone WhatsApp transfer", "4DDiG"]

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

def load_smtp_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    return {
        "server": config.get("smtp", "server"),
        "port": config.getint("smtp", "port"),
        "email": config.get("smtp", "email"),
        "auth_code": config.get("smtp", "auth_code"),
    }

# 每封邮件都抄送给这两个地址
CC_RECIPIENTS = ["sophie20202020ut@gmail.com", "heyumei@tenorshare.cn"]

def clean_email(e):
    """清洗邮箱：从 mailto:/http:// 等格式里提取纯邮箱地址"""
    import re
    m = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', str(e))
    return m.group(1) if m else str(e).strip()

def send_email(to_email, subject, body, cc=None):
    """发送一封邮件（默认抄送 CC_RECIPIENTS），成功返回 True，失败抛出异常"""
    if cc is None:
        cc = CC_RECIPIENTS
    cfg = load_smtp_config()
    context = ssl.create_default_context()
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["email"]
    msg["To"] = to_email
    if cc:
        msg["Cc"] = ", ".join(cc)
    recipients = [to_email] + list(cc)
    with smtplib.SMTP_SSL(cfg["server"], cfg["port"], context=context, timeout=25) as s:
        s.login(cfg["email"], cfg["auth_code"])
        s.sendmail(cfg["email"], recipients, msg.as_string())
    return True

def render_template(product, coop_type, name="", video_link=""):
    """按「产品 × 合作类型」取模板，替换 {name} 和 XXX（视频链接）占位符"""
    t = TEMPLATES.get((product, coop_type))
    if not t:
        # 兜底：同产品任意类型
        for (p, ct), v in TEMPLATES.items():
            if p == product:
                t = v
                break
    if not t:
        return None, None
    body = t["body"].replace("{name}", name)
    if video_link:
        body = body.replace("XXX", video_link)
    return t["subject"], body

# ============ 邮件库（发送记录） ============
import os as _os

MAIL_LOG_FILE = "邮件库.csv"

def log_sent_email(email, name, product, coop_type, status, error=""):
    """把一封已发送的邮件记录到邮件库"""
    import pandas as pd
    from datetime import datetime
    row = pd.DataFrame([{
        "发送时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "频道名称": name,
        "邮箱": email,
        "合作产品": product,
        "合作类型": coop_type,
        "状态": status,
        "错误": error,
    }])
    if _os.path.exists(MAIL_LOG_FILE):
        existing = pd.read_csv(MAIL_LOG_FILE, dtype=str)
        row = pd.concat([existing, row], ignore_index=True)
    row.to_csv(MAIL_LOG_FILE, index=False, encoding="utf-8-sig")

def load_mail_log():
    import pandas as pd
    if _os.path.exists(MAIL_LOG_FILE):
        return pd.read_csv(MAIL_LOG_FILE, dtype=str)
    return pd.DataFrame()
