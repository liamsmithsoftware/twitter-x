from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class FetchedPost:
    message_id: int
    post_url: str
    published_at: datetime
    content: str
    media_type: str = "text"
    media_url: Optional[str] = None
    views: Optional[int] = None


@dataclass
class FetchBatchResult:
    channel_title: Optional[str]
    posts: List[FetchedPost]
    source_mode: str


class CollectorError(RuntimeError):
    """A safe, user-facing error while reading a public X profile."""
