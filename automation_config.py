"""TapTap 自动化的共享默认配置与账号文本解析。"""

from __future__ import annotations

import json
import re
from pathlib import Path


DEFAULT_PHONE_NUMBER = "3412640535"
DEFAULT_PHONE_COUNTRY = "auto"
# 短信验证码链接由每条账号记录提供，不使用固定接口。
DEFAULT_SMS_API_URL = ""
DEFAULT_JFBYM_API_URL = "http://api.jfbym.com/api/YmServer/customApi"
DEFAULT_JFBYM_TOKEN = "E7LhAfiKssKDUGCudpvAhgSfOoSeYuSoc5_CsEM5ONI"
DEFAULT_JFBYM_TYPE = "50009"

COUNTRY_OPTIONS = {
    "auto": "美国 / 加拿大（+1，同等优先）",
    "United States": "美国 / 加拿大（+1，任一均可）",
    "Canada": "加拿大 / 美国（+1，任一均可）",
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
    # 美国和加拿大共用 +1 区号，登录流程中二者是同等有效的候选项。
    # 保留 value 参数是为了兼容旧设置和账号文件，但不再赋予任何一方优先级。
    return ["United States", "Canada"]


def extract_sms_verification_code(data: str) -> str:
    """从纯文本、JSON 或带分隔符的短信响应中提取 TapTap 6 位验证码。"""
    text = str(data or "").strip()
    if not text:
        return ""
    patterns = (
        r"(?i)\[?TapTap\]?\D{0,48}(\d{6})(?!\d)",
        r"(?i)(?:verification\s*code|verify\s*code|验证码|code)\D{0,24}(\d{6})(?!\d)",
        r"(?<!\d)(\d{6})(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


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


def _extract_phone_numbers(text: str) -> list[str]:
    phones = []
    for match in re.finditer(
        r"(?<!\d)(?:\+?1[\s-]?)?([2-9]\d{2}[\s-]?\d{3}[\s-]?\d{4})(?!\d)",
        text,
    ):
        phone = re.sub(r"\D", "", match.group(1))
        if phone and phone not in phones:
            phones.append(phone)
    if phones:
        return phones
    for match in re.finditer(r"(?<!\d)(\d{7,15})(?!\d)", text):
        phone = match.group(1)
        if phone not in phones:
            phones.append(phone)
    return phones


def _parse_account_mapping(payload: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in payload.items():
        _apply_account_value(result, key, value)
    if not result.get("phone"):
        raise ValueError("账号记录中未找到手机号")
    result["phone"] = re.sub(r"\D", "", result["phone"])
    return result


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
        phones = _extract_phone_numbers(text)
        if phones:
            result["phone"] = phones[0]

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


def parse_accounts_text(content: str) -> list[dict[str, str]]:
    """解析多账号文本；支持 JSON 数组、分段配置和一行一个手机号。"""
    text = (content or "").lstrip("\ufeff").strip()
    if not text:
        raise ValueError("账号文件为空")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    raw_accounts = None
    if isinstance(payload, list):
        raw_accounts = payload
    elif isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
        raw_accounts = payload["accounts"]
    elif isinstance(payload, dict):
        raw_accounts = [payload]

    accounts: list[dict[str, str]] = []
    if raw_accounts is not None:
        for item in raw_accounts:
            if isinstance(item, dict):
                accounts.append(_parse_account_mapping(item))
            elif isinstance(item, (str, int)):
                accounts.append(parse_account_text(str(item)))
            else:
                raise ValueError("JSON 账号列表包含不支持的记录")
    else:
        common: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.match(r"^([^=：:]{1,32})\s*[=：:]\s*(.+)$", stripped)
            if match:
                normalized_key = re.sub(r"[\s-]+", "_", match.group(1).strip().lower())
                if _ACCOUNT_KEY_ALIASES.get(normalized_key) != "phone":
                    _apply_account_value(common, match.group(1), match.group(2))

        blocks = [part.strip() for part in re.split(r"\r?\n\s*\r?\n", text) if part.strip()]
        if len(blocks) > 1 and all(len(_extract_phone_numbers(block)) == 1 for block in blocks):
            for block in blocks:
                account = dict(common)
                account.update(parse_account_text(block))
                accounts.append(account)
        else:
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                url_match = re.search(r"https?://[^\s]+", stripped, re.IGNORECASE)
                direct_url = url_match.group(0).rstrip(",;，；") if url_match else ""
                for phone in _extract_phone_numbers(stripped):
                    account = dict(common)
                    account["phone"] = phone
                    if direct_url:
                        account["sms_api_url"] = direct_url
                    if re.search(r"加拿大|\bcanada\b|\bCA\b", stripped, re.IGNORECASE):
                        account["country"] = "Canada"
                    elif re.search(
                        r"美国|\bunited\s*states\b|\bUSA?\b", stripped, re.IGNORECASE,
                    ):
                        account["country"] = "United States"
                    accounts.append(account)
            if not accounts:
                account = dict(common)
                account.update(parse_account_text(text))
                accounts.append(account)

    unique: list[dict[str, str]] = []
    seen = set()
    for account in accounts:
        phone = re.sub(r"\D", "", account.get("phone", ""))
        if not phone or phone in seen:
            continue
        account["phone"] = phone
        account["country"] = normalize_country(account.get("country"))
        seen.add(phone)
        unique.append(account)
    if not unique:
        raise ValueError("账号文件中未找到有效手机号")
    return unique


def load_account_file(path: str | Path) -> dict[str, str]:
    account_path = Path(path)
    if not account_path.is_file():
        raise ValueError(f"账号文件不存在: {account_path}")
    try:
        content = account_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"账号文件读取失败: {exc}") from exc
    return parse_account_text(content)
