"""Account-scoped mail metadata and OS credential storage (Python 3.9+).

Importing/constructing this module never reads a legacy config, contacts a mail
server, or accesses the credential store. Those actions require explicit calls.
"""
import configparser
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
import csv
from datetime import datetime, timezone
import hashlib
import imaplib
import io
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl
import sys
import tempfile
import threading
from uuid import uuid4


LEGACY_CONFIRMATION = "确认历史邮件由该账号发出"
ACCOUNT_FILES = frozenset({"邮件库.csv", "回信同步状态.json", "账号进度.csv"})
_ACCOUNT_ID = re.compile(r"^acct_[a-f0-9]{32}$")
_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_CURRENT_ACCOUNT = ContextVar("workbench_mail_account", default=None)
_STORE_LOCK = threading.Lock()
_STORES = {}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL COLLATE NOCASE UNIQUE,
 server TEXT NOT NULL, port INTEGER NOT NULL, imap_server TEXT NOT NULL,
 imap_port INTEGER NOT NULL, sender_name TEXT NOT NULL DEFAULT '', cc TEXT NOT NULL DEFAULT '[]',
 credential_ref TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 smtp_ready INTEGER NOT NULL DEFAULT 0, imap_ready INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 verified_at TEXT
);
CREATE TABLE IF NOT EXISTS legacy_claim (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_id TEXT NOT NULL REFERENCES accounts(id),
 confirmed_at TEXT NOT NULL, basis TEXT NOT NULL, source_hash TEXT NOT NULL, row_count INTEGER NOT NULL,
 backup_relative TEXT NOT NULL
);
"""


class AccountError(Exception):
    """Only controlled user-facing messages, never remote exception text."""


class CredentialUnavailable(AccountError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_account_id():
    return _CURRENT_ACCOUNT.get()


@contextmanager
def account_scope(account_id):
    if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 100:
        raise AccountError("请选择有效的邮箱账号。")
    token = _CURRENT_ACCOUNT.set(account_id)
    try:
        yield account_id
    finally:
        _CURRENT_ACCOUNT.reset(token)


def _secure_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AccountError("账号数据目录不能是符号链接。")
    if os.name != "nt":
        path.chmod(0o700)


def _write_bytes(path, payload):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".mail-account-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _text(value, field, limit=200):
    value = str(value or "").strip()
    if len(value) > limit or any(character in value for character in "\r\n\0"):
        raise AccountError(field + "格式不正确。")
    return value


def _email(value):
    value = _text(value, "邮箱地址", 254)
    if not _EMAIL.fullmatch(value):
        raise AccountError("请填写完整、有效的邮箱地址。")
    return value


def _host(value, label):
    value = _text(value, label, 253)
    if not value or any(character in value for character in "/\\: @"):
        raise AccountError(label + "请填写服务器主机名，不要填写网址。")
    try:
        return value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise AccountError(label + "格式不正确。") from None


def _port(value, label):
    try:
        if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
            raise ValueError()
        value = int(value)
        if not 1 <= value <= 65535:
            raise ValueError()
        return value
    except (ValueError, TypeError, OverflowError):
        raise AccountError(label + "必须是有效端口数字。") from None


def _cc(value):
    if isinstance(value, str):
        value = re.split(r"[,;，；]", value)
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise AccountError("抄送请填写邮箱地址，多个地址用逗号分隔。")
    result, seen = [], set()
    for item in value:
        if not str(item).strip():
            continue
        address = _email(item)
        if address.lower() not in seen:
            seen.add(address.lower())
            result.append(address)
    return result


def _csv_metadata(path):
    """Read records only to count them; never return mail/body cell values."""
    if not path.exists():
        return {"exists": False, "rows": 0, "columns": []}
    if path.is_symlink() or not path.is_file():
        raise AccountError("历史邮件文件不是普通文件，未读取。")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return _csv_stream_metadata(stream)
    except (OSError, UnicodeError, csv.Error):
        raise AccountError("历史邮件 CSV 无法读取，请保留原文件并检查 UTF-8 格式。") from None


def _csv_stream_metadata(stream):
    csv.field_size_limit(16 * 1024 * 1024)
    reader = csv.reader(stream, strict=True)
    columns = next(reader, [])
    count = sum(1 for row in reader if row and any(cell for cell in row))
    return {"exists": True, "rows": count, "columns": columns}


def _system_backend():
    """Choose an OS-backed keyring; never accept plaintext/alternate file backends."""
    vendor = str(Path(__file__).resolve().parent / "vendor")
    if Path(vendor).is_dir() and vendor not in sys.path:
        sys.path.insert(0, vendor)
    try:
        import keyring
    except ImportError:
        raise CredentialUnavailable("尚未安装系统凭据组件 keyring，请联系维护者安装；不会将授权码保存到普通文件。") from None
    try:
        candidate = keyring.get_keyring()
        candidates = list(candidate.backends) if type(candidate).__module__ == "keyring.backends.chainer" else [candidate]
        for backend in candidates:
            module = type(backend).__module__
            if module in {"keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService", "keyring.backends.kwallet"} and backend.priority > 0:
                return backend
    except Exception:
        pass
    raise CredentialUnavailable("系统安全凭据库当前不可用，请解锁系统凭据库或联系维护者；不会降级为明文保存。")


class AccountStore:
    def __init__(self, root=None, credential_backend=None):
        self.root = Path(root if root is not None else Path.cwd()).expanduser().resolve()
        self.state_dir = self.root / ".mail_accounts"
        self.accounts_dir = self.root / "mail_accounts"
        _secure_directory(self.state_dir)
        _secure_directory(self.accounts_dir)
        self.path = self.state_dir / "accounts.sqlite3"
        if self.path.is_symlink():
            raise AccountError("账号元数据文件不能是符号链接。")
        self._credential_backend = credential_backend
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:24]
        self._service = "CreatorWorkbench.mail." + digest
        with self._connection() as connection:
            connection.executescript(_SCHEMA)
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _backend(self):
        if self._credential_backend is None:
            self._credential_backend = _system_backend()
        return self._credential_backend

    def credential_status(self):
        try:
            self._backend()
            return {"available": True, "message": "系统安全凭据库可用。"}
        except AccountError as error:
            return {"available": False, "message": str(error)}

    @staticmethod
    def _safe(row):
        if row is None:
            return None
        fields = ("id", "name", "email", "server", "port", "imap_server", "imap_port", "sender_name", "created_at", "updated_at", "verified_at", "revision")
        result = {field: row[field] for field in fields}
        result["cc"] = json.loads(row["cc"])
        for field in ("smtp_ready", "imap_ready", "enabled"):
            result[field] = bool(row[field])
        return result

    def list_accounts(self):
        with self._connection() as connection:
            return [self._safe(row) for row in connection.execute("SELECT * FROM accounts ORDER BY created_at,rowid")]

    def _row(self, account_id, connection=None):
        if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
            raise AccountError("邮箱账号不存在，请重新选择。")
        if connection is None:
            with self._connection() as current:
                return self._row(account_id, current)
        row = connection.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise AccountError("邮箱账号不存在，请重新选择。")
        return dict(row)

    def get_account(self, account_id):
        return self._safe(self._row(account_id))

    def _secret(self, credential_ref):
        try:
            code = self._backend().get_password(self._service, credential_ref)
        except AccountError:
            raise
        except Exception:
            raise CredentialUnavailable("无法读取系统凭据，请解锁凭据库或重新填写授权码。") from None
        if not isinstance(code, str) or not code.strip():
            raise CredentialUnavailable("此账号的系统凭据缺失，请重新填写授权码。")
        return code

    def _credentials(self, row):
        result = self._safe(row)
        result["auth_code"] = self._secret(row["credential_ref"])
        result["account_id"] = row["id"]
        return result

    def get_credentials(self, account_id):
        row = self._row(account_id)
        if not row["enabled"]:
            raise AccountError("此邮箱账号已停用，不能发送或自动同步。")
        return self._credentials(row)

    def save_account(self, fields, authorization_code="", account_id=None, reuse_legacy=False):
        if not isinstance(fields, dict) or any(key in fields for key in ("auth_code", "password", "authorization_code", "credential_ref")):
            raise AccountError("请在专用授权码输入框填写凭据，不能写入账号资料。")
        previous = self._row(account_id) if account_id else None
        if "enabled" in fields and not isinstance(fields["enabled"], bool):
            raise AccountError("启用状态必须明确选择开启或关闭。")
        values = dict(previous or {})
        if previous:
            values["cc"] = json.loads(previous["cc"])
        aliases = {"smtp_server": "server", "smtp_port": "port"}
        for key, value in fields.items():
            key = aliases.get(key, key)
            if key in {"name", "email", "server", "port", "imap_server", "imap_port", "sender_name", "cc", "enabled"}:
                values[key] = value
        normalized = {
            "name": _text(values.get("name"), "账号备注", 100),
            "email": _email(values.get("email")),
            "server": _host(values.get("server", "smtp.qiye.aliyun.com"), "SMTP 服务器"),
            "port": _port(values.get("port", 465), "SMTP 端口"),
            "imap_server": _host(values.get("imap_server", "imap.qiye.aliyun.com"), "IMAP 服务器"),
            "imap_port": _port(values.get("imap_port", 993), "IMAP 端口"),
            "sender_name": _text(values.get("sender_name"), "发件署名", 100),
            "cc": json.dumps(_cc(values.get("cc", [])), ensure_ascii=False),
            "enabled": int(bool(values.get("enabled", True))),
        }
        normalized["name"] = normalized["name"] or normalized["email"]
        if previous:
            if normalized["email"].casefold() != previous["email"].casefold():
                raise AccountError("已保存账号的邮箱地址不能更改，以免把旧记录归到另一个邮箱。请新增邮箱账号，原账号和历史会保留。")
            normalized["email"] = previous["email"]
        if authorization_code is None:
            authorization_code = ""
        if not isinstance(authorization_code, str) or "\0" in authorization_code or len(authorization_code) > 4096:
            raise AccountError("授权码格式不正确。")
        if reuse_legacy:
            if authorization_code.strip():
                raise AccountError("请选择复用旧授权码或填写新授权码，不要同时使用。")
            legacy = self._legacy_config()
            old_email = legacy.get("smtp", "email", fallback="").strip()
            if normalized["email"].casefold() != old_email.casefold():
                raise AccountError("只有邮箱地址与原配置一致时才可复用旧授权码。")
            authorization_code = legacy.get("smtp", "auth_code", fallback="")
        has_new_code = bool(authorization_code.strip())
        if not previous and not has_new_code:
            raise AccountError("新增邮箱需要填写本人的授权码，或明确选择复用同邮箱的旧授权码。")
        account_id = previous["id"] if previous else "acct_" + uuid4().hex
        credential_ref = previous["credential_ref"] if previous else ""
        new_reference = "cred_" + uuid4().hex if has_new_code else None
        # Validate availability before persisting any metadata. Existing empty
        # code updates also require the old credential to remain accessible.
        backend = self._backend()
        if has_new_code:
            try:
                backend.set_password(self._service, new_reference, authorization_code)
            except Exception:
                raise CredentialUnavailable("系统凭据保存失败，账号资料未保存；不会改用明文文件。") from None
            credential_ref = new_reference
        else:
            self._secret(credential_ref)
        smtp_changed = not previous or has_new_code or any(normalized[key] != previous[key] for key in ("server", "port", "email"))
        imap_changed = not previous or has_new_code or any(normalized[key] != previous[key] for key in ("imap_server", "imap_port", "email"))
        now = _now()
        payload = dict(normalized, id=account_id, credential_ref=credential_ref,
                       smtp_ready=0 if smtp_changed else previous["smtp_ready"],
                       imap_ready=0 if imap_changed else previous["imap_ready"],
                       revision=previous["revision"] + 1 if previous else 1,
                       created_at=previous["created_at"] if previous else now, updated_at=now,
                       verified_at=None if smtp_changed or imap_changed else previous["verified_at"])
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if previous:
                    columns = [key for key in payload if key != "id"]
                    cursor = connection.execute("UPDATE accounts SET " + ",".join(key + "=?" for key in columns) + " WHERE id=? AND revision=?",
                                                [payload[key] for key in columns] + [account_id, previous["revision"]])
                    if cursor.rowcount != 1:
                        raise AccountError("账号配置已被另一操作更新，请刷新后重试。")
                else:
                    columns = list(payload)
                    connection.execute("INSERT INTO accounts(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", [payload[key] for key in columns])
        except Exception as error:
            if new_reference:
                try:
                    backend.delete_password(self._service, new_reference)
                except Exception:
                    pass
            if isinstance(error, AccountError):
                raise
            if isinstance(error, sqlite3.IntegrityError):
                raise AccountError("该邮箱已经存在，请编辑已有账号。") from None
            raise AccountError("账号资料未保存，请检查目录权限后重试。") from None
        if new_reference and previous:
            try:
                backend.delete_password(self._service, previous["credential_ref"])
            except Exception:
                pass  # Unreferenced old OS credential; never put it into SQLite.
        return self.get_account(account_id)

    def set_enabled(self, account_id, enabled):
        if not isinstance(enabled, bool):
            raise AccountError("启用状态必须明确选择开启或关闭。")
        self._row(account_id)
        with self._connection() as connection:
            connection.execute("UPDATE accounts SET enabled=?,revision=revision+1,updated_at=? WHERE id=?", (int(enabled), _now(), account_id))
        return self.get_account(account_id)

    def verify_account(self, account_id):
        row = self._row(account_id)
        smtp_ok = imap_ok = False
        try:
            credentials = self._credentials(row)
        except AccountError as error:
            with self._connection() as connection:
                connection.execute("UPDATE accounts SET smtp_ready=0,imap_ready=0,verified_at=? WHERE id=? AND revision=?", (_now(), account_id, row["revision"]))
            return {"smtp_ok": False, "imap_ok": False, "message": str(error)}
        try:
            context = ssl.create_default_context()
        except Exception:
            with self._connection() as connection:
                connection.execute("UPDATE accounts SET smtp_ready=0,imap_ready=0,verified_at=? WHERE id=? AND revision=?", (_now(), account_id, row["revision"]))
            return {"smtp_ok": False, "imap_ok": False, "message": "系统 TLS 证书环境不可用，请联系维护者检查；本次没有发送邮件。"}
        try:
            with smtplib.SMTP_SSL(credentials["server"], credentials["port"], context=context, timeout=15) as client:
                result = client.login(credentials["email"], credentials["auth_code"])
                smtp_ok = not isinstance(result, tuple) or result[0] in (235, 503)
        except Exception:
            pass
        client = None
        try:
            client = imaplib.IMAP4_SSL(credentials["imap_server"], credentials["imap_port"], ssl_context=context, timeout=15)
            result = client.login(credentials["email"], credentials["auth_code"])
            imap_ok = isinstance(result, tuple) and result[0] == "OK"
        except Exception:
            pass
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
        with self._connection() as connection:
            cursor = connection.execute("UPDATE accounts SET smtp_ready=?,imap_ready=?,verified_at=? WHERE id=? AND revision=?", (int(smtp_ok), int(imap_ok), _now(), account_id, row["revision"]))
            if cursor.rowcount != 1:
                return {"smtp_ok": False, "imap_ok": False, "message": "测试期间账号配置已更新，请重新验证；本次没有发送邮件。"}
        message = ("SMTP 登录成功" if smtp_ok else "SMTP 登录未通过，请检查服务器、授权码和网络") + "；" + ("IMAP 登录成功" if imap_ok else "IMAP 登录未通过，请检查 IMAP 开关、服务器和授权码") + "。验证仅测试登录，没有发送邮件。"
        return {"smtp_ok": smtp_ok, "imap_ok": imap_ok, "message": message}

    def account_path(self, account_id, filename="邮件库.csv"):
        self._row(account_id)
        if not isinstance(filename, str) or filename not in ACCOUNT_FILES:
            raise AccountError("不允许访问该账号文件名。")
        directory = self.accounts_dir / account_id
        _secure_directory(directory)
        target = directory / filename
        if target.is_symlink():
            raise AccountError("账号文件不能是符号链接。")
        return target

    def _legacy_config(self):
        path = self.root / "email_config.ini"
        if path.is_symlink() or not path.is_file():
            raise AccountError("未找到可读取的原工作台配置。")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with path.open(encoding="utf-8-sig") as stream:
                parser.read_file(stream)
        except (OSError, UnicodeError, configparser.Error):
            raise AccountError("原工作台配置无法解析，未读取或迁移授权码。") from None
        return parser

    def legacy_profile(self):
        if not (self.root / "email_config.ini").exists():
            return None
        config = self._legacy_config()
        try:
            return {
                "name": "原工作台邮箱", "email": config.get("smtp", "email", fallback="").strip(),
                "server": config.get("smtp", "server", fallback="smtp.qiye.aliyun.com").strip(),
                "port": config.getint("smtp", "port", fallback=465),
                "imap_server": config.get("imap", "server", fallback="imap.qiye.aliyun.com").strip(),
                "imap_port": config.getint("imap", "port", fallback=993),
                "sender_name": config.get("smtp", "sender_name", fallback="").strip(),
                "cc": _cc(config.get("smtp", "cc", fallback="")),
            }
        except (ValueError, configparser.Error):
            raise AccountError("原工作台配置的端口或邮箱字段不正确，请手工填写账号。") from None

    def legacy_metadata(self):
        metadata = _csv_metadata(self.root / "邮件库.csv")
        files = []
        for name in ("达人库.csv", "建联库.csv", "邮件库.csv", "合作效果.csv"):
            item = metadata if name == "邮件库.csv" else _csv_metadata(self.root / name)
            files.append({"name": name, "rows": item["rows"], "columns": item["columns"], "exists": item["exists"]})
        with self._connection() as connection:
            claim = connection.execute("SELECT * FROM legacy_claim WHERE singleton=1").fetchone()
        return {"has_mail_log": metadata["exists"], "filename": "邮件库.csv", "rows": metadata["rows"], "columns": metadata["columns"],
                "claimed": claim is not None, "claimed_account_id": claim["account_id"] if claim else None,
                "claimed_at": claim["confirmed_at"] if claim else None, "files": files,
                "claim_basis": claim["basis"] if claim else None,
                "message": "历史邮件已由本人确认归属；原 CSV 保留。" if claim else "历史邮件归属尚未确认，保持只读；当前配置邮箱不构成历史归属证据。"}

    def claim_legacy(self, account_id, confirmation):
        if confirmation != LEGACY_CONFIRMATION:
            raise AccountError("请明确确认历史邮件由所选账号发出；不能依据当前邮箱自动认领。")
        self._row(account_id)
        source = self.root / "邮件库.csv"
        target = self.account_path(account_id)
        # Use precisely the same CSV lock files as mail send/sync writers.
        # Neither a first send nor an old sync may race this one-time copy.
        from workbench_lock import exclusive
        with ExitStack() as stack:
            for path in sorted((source, target), key=str):
                stack.enter_context(exclusive(path.with_name("." + path.name + ".lock")))
            return self._claim_legacy_locked(account_id, source, target)

    def _claim_legacy_locked(self, account_id, source, target):
        replaced = False
        old_target = None
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                claim = connection.execute("SELECT * FROM legacy_claim WHERE singleton=1").fetchone()
                if claim:
                    if claim["account_id"] != account_id:
                        raise AccountError("历史邮件已归属其他账号，不能重复认领或移动；请人工核对原归属记录。")
                    if not target.is_file():
                        raise AccountError("归属记录存在但账号邮件文件缺失，请联系维护者从备份恢复。")
                    return {"claimed": True, "already_claimed": True, "account_id": account_id, "rows": claim["row_count"],
                            "message": "此前已完成归属，本次未重复复制；原文件后续变化不会自动重导。", "backup_path": str(self.root / claim["backup_relative"])}
                account = self._row(account_id, connection)
                if not account["smtp_ready"] or not account["imap_ready"]:
                    raise AccountError("请先完成该账号的 SMTP 和 IMAP 登录验证，再明确确认历史邮件归属。")
                metadata = _csv_metadata(source)
                if not metadata["exists"]:
                    raise AccountError("没有历史邮件 CSV 可以认领。")
                if target.exists() and _csv_metadata(target)["rows"]:
                    raise AccountError("此账号已有发送记录，不能用历史文件覆盖；请联系维护者核对合并。")
                payload = source.read_bytes()
                metadata = _csv_stream_metadata(io.StringIO(payload.decode("utf-8-sig"), newline=""))
                digest = hashlib.sha256(payload).hexdigest()
                old_target = target.read_bytes() if target.exists() else None
                backup_dir = self.state_dir / "legacy-backups" / (_now().replace(":", "-") + "-" + uuid4().hex[:8])
                _secure_directory(backup_dir)
                backup = backup_dir / "邮件库.csv"
                _write_bytes(backup, payload)
                basis = "用户明确确认历史邮件由此账号发出；未以当前配置邮箱自动推断"
                record = {"account_id": account_id, "confirmed_at": _now(), "basis": basis,
                          "source_sha256": digest, "rows": metadata["rows"], "columns": metadata["columns"]}
                _write_bytes(backup_dir / "归属记录.json", json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
                if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                    raise AccountError("认领期间原邮件文件发生变化，请停止旧同步后重试。")
                _write_bytes(target, payload)
                replaced = True
                if target.read_bytes() != payload or backup.read_bytes() != payload:
                    raise OSError("copy verification")
                relative = str(backup.relative_to(self.root))
                connection.execute("INSERT INTO legacy_claim VALUES(1,?,?,?,?,?,?)", (account_id, record["confirmed_at"], basis, digest, metadata["rows"], relative))
            return {"claimed": True, "already_claimed": False, "account_id": account_id, "rows": metadata["rows"],
                    "message": "历史邮件已按本人确认归属，已备份并逐字节复制；原邮件 CSV 和联系人库没有修改。", "backup_path": str(backup)}
        except Exception as error:
            if replaced:
                try:
                    if old_target is None:
                        target.unlink(missing_ok=True)
                    else:
                        _write_bytes(target, old_target)
                except OSError:
                    raise AccountError("归属保存失败且账号文件恢复未完成，请联系维护者检查备份；原 CSV 未修改。") from None
            if isinstance(error, AccountError):
                raise
            raise AccountError("历史归属未完成，原 CSV 未修改；请检查空间、权限和备份后重试。") from None


def get_store(root=None):
    path = str(Path(root if root is not None else Path.cwd()).expanduser().resolve())
    with _STORE_LOCK:
        if path not in _STORES:
            _STORES[path] = AccountStore(path)
        return _STORES[path]
