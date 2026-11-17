from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

PERSIAN_DIGITS_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(text: str) -> str:
    return text.translate(PERSIAN_DIGITS_MAP)


def clean_raw_input(raw: str) -> str:
    return normalize_digits(raw or "").strip()


@dataclass(frozen=True)
class ParsedChannel:
    channel_key: str
    channel_input: str
    username: Optional[str]
    canonical_url: str
    web_preview_url: Optional[str]
    default_title: str
    is_private: bool = False


def parse_channel_input(raw_input: str) -> ParsedChannel:
    value = clean_raw_input(raw_input)
    candidate = value.lstrip("@")
    if "/" in value or ":" in value or "." in value:
        url = urlparse(value if "://" in value else "https://" + value)
        if url.scheme not in {"http", "https"} or url.netloc.lower() not in {
            "x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"
        }:
            raise ValueError("فقط آدرس معتبر x.com یا twitter.com پذیرفته می‌شود.")
        parts = url.path.strip("/").split("/")
        if len(parts) != 1 and not (len(parts) == 3 and parts[1] == "status" and parts[2].isdigit()):
            raise ValueError("آدرس حساب یا لینک مستقیم پست را وارد کنید.")
        candidate = parts[0]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", candidate) or candidate.lower() in {
        "home", "explore", "search", "settings", "messages", "notifications", "i", "intent", "share", "compose", "login", "signup"
    }:
        raise ValueError("نام کاربری X باید ۱ تا ۱۵ حرف انگلیسی، عدد یا زیرخط باشد.")
    username = candidate.lower()
    return ParsedChannel(username, value, username, f"https://x.com/{username}", None, f"@{username}")
