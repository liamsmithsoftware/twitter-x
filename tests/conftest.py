from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.collectors.base import FetchBatchResult, FetchedPost  # noqa: E402


class FakeWebCollector:
    """Deterministic stand-in for the X API collector (no network in tests)."""

    def __init__(self, posts: Optional[List[FetchedPost]] = None, title: str = "حساب آزمایشی") -> None:
        self.posts: List[FetchedPost] = posts or []
        self.title = title
        self.calls: List[Dict[str, Optional[int]]] = []

    def set_posts(self, posts: List[FetchedPost]) -> None:
        self.posts = posts

    async def fetch_channel_posts(
        self,
        *,
        username: str,
        last_message_id: Optional[int],
        initial_limit: int,
        settings: Dict[str, str],
    ) -> FetchBatchResult:
        self.calls.append({"username": username, "last_message_id": last_message_id})
        # Mirror real-world behaviour: return the whole recent window, let SyncManager dedupe.
        return FetchBatchResult(channel_title=self.title, posts=list(self.posts), source_mode="api")


def make_post(
    message_id: int,
    *,
    username: str = "test_channel",
    text: Optional[str] = None,
    hours_ago: float = 1.0,
    media_type: str = "text",
    views: Optional[int] = 100,
) -> FetchedPost:
    base = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    return FetchedPost(
        message_id=message_id,
        post_url=f"https://x.com/{username}/{message_id}",
        published_at=base - timedelta(hours=hours_ago),
        content=text if text is not None else f"محتوای پیام شماره {message_id}",
        media_type=media_type,
        media_url=None,
        views=views,
    )


@pytest.fixture()
def db(tmp_path: Path):
    from app.database import DatabaseManager

    return DatabaseManager(db_path=tmp_path / "test.sqlite")


@pytest.fixture()
def fake_web() -> FakeWebCollector:
    return FakeWebCollector()


@pytest.fixture()
def app_client(tmp_path: Path, fake_web: FakeWebCollector, monkeypatch):
    from fastapi.testclient import TestClient

    import app.services.exporter as exporter_module
    from app.main import create_app

    # Keep generated .sqlite snapshots inside the test sandbox instead of data/exports
    monkeypatch.setattr(exporter_module, "EXPORTS_DIR", tmp_path / "exports")

    application = create_app(
        db_path=tmp_path / "api_test.sqlite",
        enable_scheduler=False,
        web_collector=fake_web,
    )
    with TestClient(application) as client:
        yield client


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
