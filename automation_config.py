"""TapTap 自动化的共享默认配置与账号文本解析。"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path


DEFAULT_PHONE_NUMBER = "3412640535"
DEFAULT_PHONE_COUNTRY = "auto"
DEFAULT_SMS_API_URL = "http://a.62-us.com/api/get_sms?key=03b891d2d74603649eb43c0dff4fe43a"
DEFAULT_SMS_TOKEN = "03b891d2d74603649eb43c0dff4fe43a"
DEFAULT_JFBYM_API_URL = "http://api.jfbym.com/api/YmServer/customApi"
DEFAULT_JFBYM_TOKEN = "E7LhAfiKssKDUGCudpvAhgSfOoSeYuSoc5_CsEM5ONI"
DEFAULT_JFBYM_TYPE = "50009"

COUNTRY_OPTIONS = {
    "auto": "美国或加拿大（自动）",
    "United States": "美国（United States）",
    "Canada": "加拿大（Canada）",
}

_ACCOUNT_KEY_ALIASES = {
    "phone": "phone",
    "phone_number": "phone",
    "mobile": "phone",
    "手机号": "phone",
    "country": "country",
    "phone_country": "country",
    "国家": "country",
    "国家地区": "country",
    "sms_api": "sms_api_url",
    "sms_api_url": "sms_api_url",
    "api": "sms_api_url",
    "短信接口": "sms_api_url",
    "短信api": "sms_api_url",
    "sms_token": "sms_token",
    "sms_key": "sms_token",
    "key": "sms_token",
    "短信token": "sms_token",
}


def normalize_country(value: str | None) -> str:
    raw = (value or "").strip()
    compact = re.sub(r"[\s_-]+", "", raw).lower()
    if compact in {"canada", "ca", "加拿大", "加拿大(ca)"}:
        return "Canada"
    if compact in {
        "unitedstates", "unitedstatesofamerica", "usa", "us", "美国", "美国(us)",
    }:
        return "United States"
    return "auto"


def country_candidates(value: str | None) -> list[str]:
    country = normalize_country(value)
    if country == "Canada":
        return ["Canada", "United States"]
    if country == "United States":
        return ["United States", "Canada"]
    return ["United States", "Canada"]


def build_sms_api_url(api_url: str | None, token: str | None) -> str:
    url = (api_url or DEFAULT_SMS_API_URL).strip()
    key = (token or "").strip()
    if not key:
        return url
    if "{token}" in url:
        return url.replace("{token}", urllib.parse.quote(key, safe=""))

    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    replaced = False
    updated = []
    for name, value in query:
        if name.lower() in {"key", "token", "api_key", "apikey"}:
            updated.append((name, key))
            replaced = True
        else:
            updated.append((name, value))
    if not replaced:
        updated.append(("key", key))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(updated), parsed.fragment)
    )


def _apply_account_value(result: dict[str, str], key: str, value) -> None:
    normalized_key = re.sub(r"[\s-]+", "_", str(key).strip().lower())
    target = _ACCOUNT_KEY_ALIASES.get(normalized_key)
    if not target:
        return
    text = str(value).strip()
    if target == "country":
        text = normalize_country(text)
    if text:
        result[target] = text


def parse_account_text(content: str) -> dict[str, str]:
    """解析单账号文本，兼容 JSON、key=value 和普通手机号/API 文本。"""
    text = (content or "").lstrip("\ufeff").strip()
    if not text:
        raise ValueError("账号文件为空")
    result: dict[str, str] = {}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for key, value in payload.items():
            _apply_account_value(result, key, value)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("#"):
            continue
        match = re.match(r"^([^=：:]{1,32})\s*[=：:]\s*(.+)$", line)
        if match:
            _apply_account_value(result, match.group(1), match.group(2))

    if "phone" not in result:
        phone_match = re.search(
            r"(?<!\d)(?:\+?1[\s-]?)?([2-9]\d{2}[\s-]?\d{3}[\s-]?\d{4})(?!\d)",
            text,
        )
        if not phone_match:
            phone_match = re.search(r"(?<!\d)(\d{7,15})(?!\d)", text)
        if phone_match:
            result["phone"] = re.sub(r"\D", "", phone_match.group(1))

    if "sms_api_url" not in result:
        url_match = re.search(r"https?://[^\s]+", text)
        if url_match:
            result["sms_api_url"] = url_match.group(0).rstrip(",;，；")

    if "country" not in result:
        if re.search(r"加拿大|\bcanada\b|\bCA\b", text, re.IGNORECASE):
            result["country"] = "Canada"
        elif re.search(r"美国|\bunited\s*states\b|\bUSA?\b", text, re.IGNORECASE):
            result["country"] = "United States"

    if not result.get("phone"):
        raise ValueError("账号文件中未找到手机号")
    return result


def load_account_file(path: str | Path) -> dict[str, str]:
    account_path = Path(path)
    if not account_path.is_file():
        raise ValueError(f"账号文件不存在: {account_path}")
    try:
        content = account_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"账号文件读取失败: {exc}") from exc
    return parse_account_text(content)
