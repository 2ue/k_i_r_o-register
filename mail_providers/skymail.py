"""SkyMail / Cloud Mail 邮箱服务"""
import hashlib
import random
import re
import string
import time
from datetime import datetime, timezone

from .base import MailProvider


def _random_mailbox_name() -> str:
    return (
        "".join(random.choices(string.ascii_lowercase, k=6))
        + "".join(random.choices(string.digits, k=4))
    )


def _parse_time(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        date = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_code(message: dict) -> str | None:
    content = (
        f"{message.get('subject', '')}\n"
        f"{message.get('text', '')}\n"
        f"{message.get('content', '')}"
    ).strip()
    if not content:
        return None
    match = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>", content, re.I)
    if match:
        return match.group(1)
    match = re.search(r"(?:Verification code|code is|代码为|验证码)[:\s]*(\d{6})", content, re.I)
    if match and match.group(1) != "177010":
        return match.group(1)
    for code in re.findall(r">\s*(\d{6})\s*<|(?<![#&])\b(\d{6})\b", content):
        value = code[0] or code[1]
        if value and value != "177010":
            return value
    return None


class SkyMailProvider(MailProvider):
    """SkyMail / Cloud Mail 邮箱服务提供者"""

    name = "skymail"
    display_name = "SkyMail"

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        domain_id=None,
        domain: str = "",
        admin_email: str = "",
        admin_password: str = "",
        token: str = "",
        mailbox_password: str = "",
        role_name: str = "",
        session=None,
    ):
        self.base_url = str(base_url or "https://skymail.ink").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = self._normalize_domain(domain or domain_id)
        self.admin_email = str(admin_email or "").strip()
        self.admin_password = str(admin_password or "").strip()
        self.token = str(token or "").strip()
        self.mailbox_password = str(mailbox_password or "").strip()
        self.role_name = str(role_name or "").strip()
        self.address = None
        self._seen_refs = set()

        self._parse_api_key()
        if not self.domain and self.admin_email and "@" in self.admin_email:
            self.domain = self.admin_email.rsplit("@", 1)[1].strip().lower()

        if session is None:
            from curl_cffi import requests as curl_requests
            session = curl_requests.Session(impersonate="chrome131")
        self.session = session

    @staticmethod
    def _normalize_domain(value) -> str:
        text = str(value or "").strip()
        if not text or text == "default":
            return ""
        text = text.removeprefix("@")
        if "://" in text:
            text = text.split("://", 1)[1]
        return text.split("/", 1)[0].strip().lower()

    def _parse_api_key(self) -> None:
        if not self.api_key:
            return
        if self.api_key.lower().startswith("token:"):
            self.token = self.api_key.split(":", 1)[1].strip()
            return
        if ":" in self.api_key and "@" in self.api_key.split(":", 1)[0]:
            email, password = self.api_key.split(":", 1)
            self.admin_email = self.admin_email or email.strip()
            self.admin_password = self.admin_password or password.strip()
            return
        self.token = self.token or self.api_key

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        auth: bool = True,
        expected: tuple[int, ...] = (200, 201),
    ):
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth:
            token = self._ensure_token()
            headers["Authorization"] = token
        resp = self.session.request(
            method.upper(),
            f"{self.base_url}{path}",
            headers=headers,
            json=payload,
            timeout=30,
            verify=False,
        )
        if resp.status_code not in expected:
            raise RuntimeError(f"SkyMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if isinstance(data, dict) and str(data.get("code", 200)) not in ("0", "200"):
            raise RuntimeError(f"SkyMail 请求失败: {data.get('message') or data}")
        return data.get("data") if isinstance(data, dict) and "data" in data else data

    def _ensure_token(self) -> str:
        if self.token:
            return self.token
        if not self.admin_email or not self.admin_password:
            raise RuntimeError("SkyMail 需要填写管理员邮箱和管理员密码")
        data = self._request(
            "POST",
            "/api/public/genToken",
            payload={"email": self.admin_email, "password": self.admin_password},
            auth=False,
        )
        token = str(data.get("token") if isinstance(data, dict) else "").strip()
        if not token:
            raise RuntimeError("SkyMail 生成 Token 失败: 响应缺少 token")
        self.token = token
        return self.token

    @staticmethod
    def _items(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items") or data.get("records") or data.get("list") or data.get("data") or []
        return []

    def create_mailbox(self) -> str:
        if not self.domain:
            raise RuntimeError("SkyMail 需要在“域名”字段填写邮箱域名")
        self.address = f"{_random_mailbox_name()}@{self.domain}"
        user = {"email": self.address}
        if self.mailbox_password:
            user["password"] = self.mailbox_password
        if self.role_name:
            user["roleName"] = self.role_name
        self._request("POST", "/api/public/addUser", payload={"list": [user]})
        return self.address

    def wait_otp(self, timeout: int = 120, poll_interval: int = 3) -> str:
        if not self.address:
            return ""

        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self._fetch_latest_message()
            if message:
                content = f"{message.get('subject', '')}\n{message.get('text', '')}\n{message.get('content', '')}"
                received_value = str(message.get("createTime") or "")
                digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
                ref = f"content:skymail:{self.address}:{received_value}:{digest}"
                if ref not in self._seen_refs:
                    code = _extract_code(message)
                    if code:
                        self._seen_refs.add(ref)
                        return code
            time.sleep(max(0.2, poll_interval))
        return ""

    def _fetch_latest_message(self) -> dict | None:
        data = self._request(
            "POST",
            "/api/public/emailList",
            payload={
                "toEmail": self.address,
                "timeSort": "desc",
                "type": 0,
                "isDel": 0,
                "num": 1,
                "size": 10,
            },
        )
        messages = [item for item in self._items(data) if isinstance(item, dict)]
        if not messages:
            return None
        return max(messages, key=lambda item: (
            (_parse_time(item.get("createTime")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(),
            str(item.get("emailId") or ""),
        ))

    def list_domains(self) -> list[dict]:
        domain = self.domain
        if not domain and self.admin_email and "@" in self.admin_email:
            domain = self.admin_email.rsplit("@", 1)[1].strip().lower()
        return [{"id": domain, "domain": domain}] if domain else []

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if close:
            close()
