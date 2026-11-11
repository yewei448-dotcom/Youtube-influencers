"""Personal product/template overrides with immutable versions and exact lookup.

Legacy Python constants are read with ast.literal_eval, never imported.  This
module does not read configuration, CSV records, credentials or mailboxes.
"""
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
import string
import uuid


PURPOSES = ("首次邀约", "接受报价", "议价回复")
DEFAULT_PRODUCTS = ("ReiBoot", "iCareFone WhatsApp transfer", "4DDiG",
                    "4uKey for iOS", "4uKey for Android", "iAnyGo")
COOP_TYPES = ("独立视频", "加链接", "shorts", "插播", "包月加链接")
COMMON_VARIABLES = ("达人名", "视频链接", "发件人姓名", "发件邮箱", "产品名", "产品链接", "合作形式")
REPLY_VARIABLES = ("对外金额", "币种")
ALIASES = {"name": "达人名", "video_link": "视频链接", "sender_name": "发件人姓名",
           "sender_email": "发件邮箱", "product": "产品名", "product_url": "产品链接",
           "coop_type": "合作形式", "amount": "对外金额", "currency": "币种"}


class TemplateError(ValueError):
    pass


class TemplateConflict(TemplateError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _name(value):
    return " ".join(str(value or "").split())


def _purpose(value):
    value = {"首封邀约": "首次邀约", "接受": "接受报价", "砍价": "议价回复"}.get(value, value)
    if value not in PURPOSES:
        raise TemplateError("请选择首次邀约、接受报价或议价回复")
    return value


def allowed_variables(purpose="首次邀约"):
    return list(COMMON_VARIABLES + (() if _purpose(purpose) == "首次邀约" else REPLY_VARIABLES))


def _constant(path, name, default):
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return ast.literal_eval(node.value)
    except (OSError, SyntaxError, ValueError, TypeError):
        pass
    return default


def _normalise_text(text, seed=False):
    text = str(text or "")
    # Compatibility is explicit and bounded to these legacy placeholders.
    text = text.replace("{name}", "{达人名}")
    text = re.sub(r"\bXXX\b", "{视频链接}", text)
    if seed:
        text = re.sub(r"\b(?:Elva|Ella)\b", "{发件人姓名}", text)
    return text


def _fields(text, purpose):
    fields = []
    allowed = set(allowed_variables(purpose)) | {
        key for key, target in ALIASES.items() if target in allowed_variables(purpose)}
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        raise TemplateError("模板花括号不完整，请检查变量占位符") from None
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in allowed or spec or conversion:
            if purpose == "首次邀约" and field in {"对外金额", "币种", "amount", "currency", "最佳价格", "budget", "建议价", "内部预算"}:
                raise TemplateError("首次邀约不能使用金额、预算或内部建议价变量，请先让达人报价")
            raise TemplateError("模板含未知或不支持的变量，请使用页面提供的变量")
        fields.append(field)
    return fields


def _validated_template(subject, body, purpose):
    subject, body = _normalise_text(subject), _normalise_text(body)
    if not subject.strip() or not body.strip():
        raise TemplateError("请填写邮件主题和正文")
    if "\r" in subject or "\n" in subject or "\x00" in subject or "\x00" in body:
        raise TemplateError("主题不能换行，模板不能包含无效控制字符")
    if len(subject) > 500 or len(body) > 50000:
        raise TemplateError("模板过长，请缩短后保存")
    _fields(subject, purpose)
    _fields(body, purpose)
    return subject, body


def _baseline_templates():
    source = Path(__file__).resolve().parent
    invitations = _constant(source / "email_module.py", "TEMPLATES", {})
    replies = _constant(source / "pricing.py", "议价模板", {})
    defaults = {}
    for key, value in invitations.items():
        if not isinstance(key, tuple) or len(key) != 2 or key[0] not in DEFAULT_PRODUCTS or not isinstance(value, dict):
            continue
        product, coop_type = key
        subject = _normalise_text(value.get("subject"), seed=True)
        body = _normalise_text(value.get("body"), seed=True)
        defaults[(product, coop_type, "首次邀约")] = _validated_template(subject, body, "首次邀约")
        # Pricing is a generic reply baseline, only attached to combinations
        # already present in the legacy invitation constants. New combinations
        # and new products remain explicitly unconfigured.
        for source_key, purpose in (("接受", "接受报价"), ("砍价", "议价回复")):
            reply = replies.get(source_key)
            if not isinstance(reply, dict):
                continue
            subject = _normalise_text(reply.get("subject"), seed=True)
            body = _normalise_text(reply.get("body"), seed=True)
            body = body.replace("The rate you quoted works for us.",
                                "We can confirm {币种} {对外金额} for this collaboration.")
            body = body.replace(
                "Regarding the rate, our budget for this first video is ${最佳价格}. I know your channel is worth more, but here's how we like to work: this doesn't have to be a one-off.",
                "For this collaboration, we can offer {币种} {对外金额}. Would that work for you?")
            body = body.replace("${最佳价格}", "{币种} {对外金额}").replace("{最佳价格}", "{对外金额}")
            body = body.replace("this first video", "this collaboration").replace("this video", "this collaboration")
            defaults[(product, coop_type, purpose)] = _validated_template(subject, body, purpose)
    return defaults


class TemplateStore:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path.cwd()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "产品模板.sqlite3"
        self._baseline = _baseline_templates()
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS products (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
                    url TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                    baseline_name TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS template_versions (
                    product_id TEXT NOT NULL, coop_type TEXT NOT NULL, purpose TEXT NOT NULL,
                    version INTEGER NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                    source TEXT NOT NULL, saved_at TEXT NOT NULL,
                    PRIMARY KEY(product_id,coop_type,purpose,version),
                    FOREIGN KEY(product_id) REFERENCES products(id)
                );
            """)
            for name in DEFAULT_PRODUCTS:
                connection.execute("INSERT OR IGNORE INTO products VALUES (?,?,?,'',1,?,?,?)",
                                   (self._id(name), name, name.casefold(), name, _now(), _now()))

    @staticmethod
    def _id(name):
        return "product_" + uuid.uuid5(uuid.NAMESPACE_URL, "creator-workbench/product/" + _name(name).casefold()).hex

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def list_products(self, include_inactive=False):
        with self._connect() as connection:
            query = "SELECT * FROM products" + ("" if include_inactive else " WHERE active=1") + " ORDER BY rowid"
            return [dict(row, active=bool(row["active"])) for row in connection.execute(query)]

    def products(self):
        return [row["name"] for row in self.list_products()]

    def coop_types(self):
        return list(COOP_TYPES)

    def get_product(self, product):
        with self._connect() as connection:
            return self._product(connection, product)

    def _product(self, connection, product):
        name = _name(product)
        alias = {"reiboot for ios": "ReiBoot", "icarefone": "iCareFone WhatsApp transfer"}.get(name.casefold(), name)
        row = connection.execute("SELECT * FROM products WHERE id=? OR name_key=?", (name, alias.casefold())).fetchone()
        if row is None:
            raise TemplateError("产品不存在，请先新增产品")
        return dict(row, active=bool(row["active"]))

    def add_product(self, name, url=""):
        name = _name(name)
        if not name or len(name) > 100 or any(ord(char) < 32 for char in str(name)):
            raise TemplateError("请输入有效的产品名称，最多100字")
        url = str(url or "").strip()
        if url:
            from urllib.parse import urlsplit
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in ("https", "http") and bool(parsed.hostname) and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if not valid:
                raise TemplateError("产品链接须为有效的http或https网页")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM products WHERE name_key=?", (name.casefold(),)).fetchone()
            if current is not None:
                return dict(current, active=bool(current["active"]), created=False)
            product_id = self._id(name)
            connection.execute("INSERT INTO products VALUES (?,?,?,?,1,NULL,?,?)",
                               (product_id, name, name.casefold(), url, _now(), _now()))
            return dict(self._product(connection, product_id), created=True)

    def set_product_active(self, product, active):
        with self._connect() as connection:
            current = self._product(connection, product)
            connection.execute("UPDATE products SET active=?,updated_at=? WHERE id=?", (int(bool(active)), _now(), current["id"]))
            return self._product(connection, current["id"])

    def _get(self, connection, product, coop_type, purpose):
        current = self._product(connection, product)
        coop_type = _name(coop_type)
        purpose = _purpose(purpose)
        row = connection.execute("SELECT * FROM template_versions WHERE product_id=? AND coop_type=? AND purpose=? ORDER BY version DESC LIMIT 1",
                                 (current["id"], coop_type, purpose)).fetchone()
        if row is not None:
            subject, body, version, source = row["subject"], row["body"], row["version"], row["source"]
        else:
            baseline = self._baseline.get((current.get("baseline_name"), coop_type, purpose))
            if baseline is None:
                return None
            subject, body = baseline
            version, source = 0, "默认模板"
        return {"product_id": current["id"], "product": current["name"], "coop_type": coop_type,
                "purpose": purpose, "subject": subject, "body": body, "version": version,
                "source": source, "active": current["active"],
                "template_key": current["id"] + "|" + coop_type + "|" + purpose}

    def get_template(self, product, coop_type, purpose="首次邀约"):
        with self._connect() as connection:
            return self._get(connection, product, coop_type, purpose)

    def save_template(self, product, coop_type, purpose="首次邀约", subject="", body="", expected_version=None):
        purpose, coop_type = _purpose(purpose), _name(coop_type)
        if not coop_type or len(coop_type) > 80:
            raise TemplateError("请选择有效的合作形式")
        subject, body = _validated_template(subject, body, purpose)
        return self._write(product, coop_type, purpose, subject, body, "个人模板", expected_version)

    def _write(self, product, coop_type, purpose, subject, body, source, expected_version):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_product = self._product(connection, product)
            if not current_product["active"]:
                raise TemplateError("产品已停用，请先启用再修改模板")
            previous = self._get(connection, product, coop_type, purpose)
            version = previous["version"] if previous else 0
            if expected_version is not None and version != expected_version:
                raise TemplateConflict("模板已有较新版本，请重新载入后核对")
            version += 1
            connection.execute("INSERT INTO template_versions VALUES (?,?,?,?,?,?,?,?)",
                               (current_product["id"], coop_type, purpose, version, subject, body, source, _now()))
            return self._get(connection, product, coop_type, purpose)

    def restore_template(self, product, coop_type, purpose="首次邀约", expected_version=None):
        purpose, coop_type = _purpose(purpose), _name(coop_type)
        current = self.get_product(product)
        baseline = self._baseline.get((current.get("baseline_name"), coop_type, purpose))
        if baseline is None:
            raise TemplateError("此组合没有预置默认模板，无法还原")
        return self._write(product, coop_type, purpose, baseline[0], baseline[1], "还原默认", expected_version)

    def has_default(self, product, coop_type, purpose="首次邀约"):
        current = self.get_product(product)
        return (current.get("baseline_name"), _name(coop_type), _purpose(purpose)) in self._baseline

    def template_history(self, product, coop_type, purpose="首次邀约"):
        with self._connect() as connection:
            current = self._product(connection, product)
            return [dict(row) for row in connection.execute(
                "SELECT * FROM template_versions WHERE product_id=? AND coop_type=? AND purpose=? ORDER BY version",
                (current["id"], _name(coop_type), _purpose(purpose)))]

    def render(self, product, coop_type, purpose="首次邀约", values=None):
        template = self.get_template(product, coop_type, purpose)
        if template is None:
            raise TemplateError("此产品、合作形式和用途尚未配置模板")
        if not template["active"]:
            raise TemplateError("产品已停用，不能生成新邮件")
        return self.render_content(template, values)

    def render_content(self, template, values=None):
        """Validate/render unsaved editor content without persisting it."""
        result = dict(template)
        purpose = _purpose(result.get("purpose", "首次邀约"))
        subject, body = _validated_template(result.get("subject"), result.get("body"), purpose)
        supplied = dict(values or {})
        for alias, canonical in ALIASES.items():
            if canonical not in supplied and alias in supplied:
                supplied[canonical] = supplied[alias]
        supplied.setdefault("产品名", result.get("product", ""))
        supplied.setdefault("合作形式", result.get("coop_type", ""))
        if result.get("product_id"):
            supplied.setdefault("产品链接", self.get_product(result["product_id"])["url"])
        if purpose != "首次邀约":
            try:
                raw_amount = supplied.get("对外金额")
                if isinstance(raw_amount, bool):
                    raise ValueError()
                amount = Decimal(str(raw_amount))
                if not amount.is_finite() or amount <= 0:
                    raise ValueError()
            except (InvalidOperation, TypeError, ValueError):
                raise TemplateError("请明确确认本次对外金额，不能自动使用内部预算或建议价") from None
            currency = str(supplied.get("币种", "")).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise TemplateError("请明确本次对外金额的三字母币种，不自动默认美元")
            supplied["对外金额"] = format(amount, "f").rstrip("0").rstrip(".") if "." in format(amount, "f") else format(amount, "f")
            supplied["币种"] = currency
        used = _fields(subject, purpose) + _fields(body, purpose)
        rendered_values = {}
        for field in used:
            key = ALIASES.get(field, field)
            value = supplied.get(key)
            if value is None or isinstance(value, (dict, list, tuple, set, bool)) or not str(value).strip():
                raise TemplateError("模板变量待补齐：" + key)
            rendered_values[field] = str(value).strip()
        result["subject"] = subject.format_map(rendered_values)
        result["body"] = body.format_map(rendered_values)
        if "\r" in result["subject"] or "\n" in result["subject"] or "\x00" in result["body"]:
            raise TemplateError("变量包含无效的邮件内容，请核对")
        result["purpose"] = purpose
        return result


def get_template_store(root=None):
    return TemplateStore(root)
