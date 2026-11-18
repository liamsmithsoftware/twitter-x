"""Read the public, logged-out X profile in an isolated Chromium browser."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import Page, ProxySettings, sync_playwright
from playwright.sync_api import TimeoutError as BrowserTimeout

from app.collectors.base import CollectorError, FetchBatchResult, FetchedPost
from app.utils.channel_parser import parse_channel_input

_READ_WINDOW = Path(__file__).with_name("timeline.js").read_text(encoding="utf-8")
_MAX_SCROLLS = 40
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
_LOGIN = "ایکس برای نمایش این صفحه ورود به حساب را می‌خواهد؛ دریافت بدون ورود ممکن نیست."
_UNAVAILABLE = "صفحهٔ عمومی حساب قابل خواندن نیست؛ دسترسی بدون ورود یا ساختار صفحهٔ ایکس را بررسی کنید."
_INCOMPLETE = "بارگذاری پست‌ها کامل نشد؛ مطلبی ذخیره نشد و نشانگر دریافت تغییر نکرد."
_RATE_LIMIT = "ایکس تعداد درخواست‌ها را محدود کرده است؛ بعداً دوباره تلاش کنید."


def browser_proxy(settings: dict) -> ProxySettings | None:
    if settings.get("proxy_enabled") != "1":
        return None
    host = (settings.get("proxy_host") or "").strip()
    port = (settings.get("proxy_port") or "").strip()
    scheme = settings.get("proxy_type") or "socks5"
    if (
        not host or re.search(r"[\s/@?#]", host)
        or not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535
        or scheme not in {"http", "socks5"}
    ):
        raise CollectorError("نوع، آدرس و پورت پروکسی را در تنظیمات بررسی کنید.")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 literal
    proxy: ProxySettings = {"server": f"{scheme}://{host}:{port}"}
    if settings.get("proxy_username") or settings.get("proxy_password"):
        if scheme == "socks5":
            raise CollectorError("مرورگر از رمز عبور برای SOCKS5 پشتیبانی نمی‌کند؛ از پروکسی HTTP یا SOCKS5 بدون رمز استفاده کنید.")
        proxy.update(username=settings.get("proxy_username", ""), password=settings.get("proxy_password", ""))
    return proxy


def _check_location(url: str, username: str) -> None:
    location = urlsplit(url)
    if location.scheme != "https" or location.netloc.lower() not in _X_HOSTS:
        raise CollectorError(_UNAVAILABLE)
    path = location.path.lower().rstrip("/")
    if path in {"/login", "/signup"} or path.startswith("/i/flow/"):
        raise CollectorError(_LOGIN)
    if path != f"/{username}":
        raise CollectorError(_UNAVAILABLE)


def _explicitly_empty(window: dict, username: str) -> bool:
    """Fail closed on interstitials. A normal login link in the footer is not a wall."""
    if window["dialog"] is None and not window["hasNotice"] and window["cards"]:
        return False
    text = (window["dialog"] or window["notice"] or window["emptyText"] or "").lower().replace("’", "'")
    if "posts are protected" in text or "tweets are protected" in text:
        raise CollectorError("پست‌های این حساب محافظت‌شده هستند و در وب عمومی نمایش داده نمی‌شوند.")
    if "doesn't exist" in text or "account suspended" in text or "account is suspended" in text:
        raise CollectorError("این حساب پیدا نشد یا تعلیق شده است.")
    if "rate limit" in text or "too many requests" in text:
        raise CollectorError(_RATE_LIMIT)
    login_labels = ("log in to", "sign in to", "sign up to")
    if window["dialog"] is not None or window["hasNotice"]:
        login_labels = ("log in", "sign in", "sign up", "login")
    if any(label in text for label in login_labels):
        raise CollectorError(_LOGIN)
    if window["dialog"] is None and window["emptyState"] and not window["cards"]:
        if f"@{username} hasn't posted" in text or "no posts yet" in text:
            return True
    raise CollectorError(_UNAVAILABLE)


def _parse_card(card: dict, username: str) -> FetchedPost | None:
    link = urlsplit(card["href"])
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/status/([0-9]{1,19})/?", link.path)
    if link.scheme != "https" or link.netloc.lower() not in _X_HOSTS or not match:
        raise CollectorError(_INCOMPLETE)
    if match[1].lower() != username:
        return None  # e.g. a repost; the visible ID belongs to another author
    if card["content"] is None:
        raise CollectorError(_INCOMPLETE)
    identifier = int(match[2])  # Never pass a snowflake through a JavaScript Number.
    try:
        published_at = datetime.fromisoformat((card["datetime"] or "").replace("Z", "+00:00"))
        if published_at.tzinfo is None or not 0 < identifier < 2**63:
            raise ValueError
    except ValueError:
        raise CollectorError(_INCOMPLETE) from None
    views = re.search(r"(?<![\w.,])([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+) views?\b", card["metrics"], re.I)
    media_url = card["media_url"]
    return FetchedPost(
        message_id=identifier, post_url=f"https://x.com/{username}/status/{identifier}",
        published_at=published_at, content=card["content"], media_type=card["media_type"],
        media_url=media_url if media_url and media_url.startswith("https://") else None,
        views=int(views[1].replace(",", "")) if views else None,
    )


def read_profile(page: Page, username: str, last_message_id: int | None, initial_limit: int) -> FetchBatchResult:
    response = page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=30_000)
    if response is None:
        raise CollectorError(_UNAVAILABLE)
    if not response.ok:
        errors = {401: _LOGIN, 403: _UNAVAILABLE, 404: "حساب مورد نظر پیدا نشد.", 429: _RATE_LIMIT}
        raise CollectorError(errors.get(response.status, f"خطای وب ایکس (HTTP {response.status})"))
    _check_location(page.url, username)

    seen_links: set[str] = set()
    posts: dict[int, FetchedPost] = {}
    unpinned: set[int] = set()
    title = None
    for step in range(_MAX_SCROLLS + 1):
        args = {"seen": list(seen_links), "profilePath": f"/{username}"}
        try:
            handle = page.wait_for_function(
                _READ_WINDOW, arg=args, timeout=15_000 if step == 0 else 4_000, polling=250,
            )
            window = handle.json_value()
            handle.dispose()
            stalled = False
        except BrowserTimeout:
            window = page.evaluate(_READ_WINDOW, {**args, "immediate": True})
            stalled = True
        _check_location(window["url"], username)
        if _explicitly_empty(window, username):
            if seen_links:
                raise CollectorError(_INCOMPLETE)
            break
        profile_title = re.match(rf"(.+?)\s*\(@{re.escape(username)}\)\s*/", window["title"], re.I)
        if profile_title:
            title = profile_title[1].strip()
        for card in window["cards"]:
            post = _parse_card(card, username)
            seen_links.add(card["href"])
            if post:
                posts[post.message_id] = post
                if not card["pinned"]:
                    unpinned.add(post.message_id)
        if last_message_id is None and len(unpinned) >= initial_limit:
            break
        if last_message_id is not None and last_message_id in unpinned:
            break
        if stalled:
            if window["loading"] or not window["atEnd"]:
                raise CollectorError(_INCOMPLETE)
            # A quiet bottom is the end of the *visible* timeline, not proof of complete history.
            if last_message_id is not None and last_message_id not in posts and max(posts, default=0) > last_message_id:
                raise CollectorError(
                    "پست جدید دیده شد اما نشانگر قبلی در صفحهٔ عمومی پیدا نشد. "
                    "اگر آن پست حذف شده است، نشانگر را بدون حذف آرشیو بازنشانی کنید."
                )
            break
        if step == _MAX_SCROLLS:
            raise CollectorError(_INCOMPLETE)
        page.evaluate("y => window.scrollTo(0, y)", window["nextY"])

    ordered = sorted(posts.values(), key=lambda post: post.message_id, reverse=True)
    if last_message_id is None:
        ordered = ordered[:initial_limit]
    else:
        ordered = [p for p in ordered if p.message_id > last_message_id]
    return FetchBatchResult(title, ordered, "web")


class XWebCollector:
    def __init__(self) -> None:
        self._slots = asyncio.Semaphore(2)  # bound Chromium instances across concurrent manual syncs

    async def fetch_channel_posts(
        self, *, username: str, last_message_id: int | None, initial_limit: int, settings: dict,
    ) -> FetchBatchResult:
        profile = parse_channel_input(username)
        proxy = browser_proxy(settings)
        async with self._slots:
            # Keep browser I/O off the server loop (also works with Windows selector loops).
            return await asyncio.to_thread(self._visit, profile.channel_key, last_message_id, initial_limit, proxy)

    @staticmethod
    def _visit(
        username: str, last_message_id: int | None, initial_limit: int, proxy: ProxySettings | None,
    ) -> FetchBatchResult:
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True, proxy=proxy)
                except BrowserError:
                    raise CollectorError(
                        "Chromium اجرا نشد. در محیط برنامه python -m playwright install chromium "
                        "را اجرا و وابستگی‌های سیستم را بررسی کنید."
                    ) from None
                with browser:
                    # A new non-persistent context. Never import the user's cookies or storage.
                    page = browser.new_page(locale="en-US")
                    return read_profile(page, username, last_message_id, initial_limit)
        except BrowserTimeout:
            raise CollectorError("زمان انتظار برای باز شدن صفحهٔ ایکس به پایان رسید.") from None
        except BrowserError:
            # Browser errors may include URLs or proxy details; never persist them in logs.
            raise CollectorError("صفحهٔ وب ایکس باز نشد؛ اتصال شبکه و تنظیمات پروکسی را بررسی کنید.") from None
