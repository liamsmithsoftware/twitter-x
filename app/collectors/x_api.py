"""Official X API v2 public timelines. No scraping or session-cookie login."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import httpx

from app.collectors.base import CollectorError, FetchBatchResult, FetchedPost, build_httpx_proxy_url


class XAPICollector:
    def __init__(self, transport=None):
        self.transport = transport

    @staticmethod
    async def _get(client, path, params=None):
        try:
            response = await client.get(path, params=params)
        except httpx.HTTPError:
            raise CollectorError("اتصال به API ایکس برقرار نشد؛ شبکه و پروکسی را بررسی کنید.") from None
        errors = {
            401: "Bearer Token معتبر نیست؛ تنظیمات را بررسی کنید.",
            403: "دسترسی مجاز نیست؛ طرح API یا عمومی بودن حساب را بررسی کنید.",
            404: "حساب مورد نظر پیدا نشد.",
            429: "محدودیت نرخ یا سهمیه API؛ بعداً دوباره تلاش کنید.",
        }
        if response.status_code != 200:
            # Never persist response bodies or request headers: they may contain secrets.
            raise CollectorError(errors.get(response.status_code, f"خطای سرویس X (HTTP {response.status_code})"))
        try:
            result = response.json()
        except ValueError:
            raise CollectorError("پاسخ نامعتبر از API ایکس.") from None
        if not isinstance(result, dict) or result.get("errors"):
            raise CollectorError("API ایکس پاسخ ناقص یا خطا برگرداند؛ نشانگر دریافت تغییر نکرد.")
        return result

    async def fetch_channel_posts(self, *, username: str, last_message_id: Optional[int], initial_limit: int, settings: dict) -> FetchBatchResult:
        token = settings.get("x_bearer_token", "").strip()
        if not token:
            raise CollectorError("برای دریافت واقعی، Bearer Token را در تنظیمات وارد کنید.")
        async with httpx.AsyncClient(
            base_url="https://api.x.com/2/", headers={"Authorization": f"Bearer {token}"},
            timeout=30, proxy=build_httpx_proxy_url(settings), transport=self.transport,
            follow_redirects=False, trust_env=False,
        ) as client:
            user = (await self._get(client, f"users/by/username/{username}")).get("data")
            if not user or not str(user.get("id", "")).isdigit():
                raise CollectorError("حساب مورد نظر پیدا نشد.")
            params = {
                "max_results": str(max(5, min(100, initial_limit)) if last_message_id is None else 100),
                "tweet.fields": "created_at,public_metrics,attachments",
                "expansions": "attachments.media_keys",
                "media.fields": "type,url,preview_image_url",
            }
            if last_message_id is not None:
                params["since_id"] = str(last_message_id)
            posts = []
            seen_tokens = set()
            while True:
                batch = await self._get(client, f"users/{user['id']}/tweets", params)
                media = {m["media_key"]: m for m in batch.get("includes", {}).get("media", [])}
                for tweet in batch.get("data", []):
                    try:
                        item = next((media[k] for k in tweet.get("attachments", {}).get("media_keys", []) if k in media), {})
                        media_type = {"photo": "photo", "video": "video", "animated_gif": "video"}.get(item.get("type"), "text")
                        posts.append(FetchedPost(
                            message_id=int(tweet["id"]), post_url=f"https://x.com/{username}/status/{tweet['id']}",
                            published_at=datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00")),
                            content=tweet["text"], media_type=media_type,
                            media_url=item.get("url") or item.get("preview_image_url"),
                            views=tweet.get("public_metrics", {}).get("impression_count"),
                        ))
                    except (KeyError, ValueError, TypeError):
                        raise CollectorError("ساختار پست دریافتی ناقص است؛ دریافت ذخیره نشد.") from None
                next_token = batch.get("meta", {}).get("next_token")
                if not next_token or (last_message_id is None and len(posts) >= initial_limit):
                    break
                if next_token in seen_tokens:
                    raise CollectorError("صفحه‌بندی API تکرار شد؛ دریافت ذخیره نشد.")
                seen_tokens.add(next_token)
                params["pagination_token"] = next_token
            return FetchBatchResult(user.get("name"), posts, "api")
