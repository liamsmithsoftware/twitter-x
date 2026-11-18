from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import sync_playwright

from app.collectors.base import CollectorError
from app.collectors.manager import SyncManager
from app.collectors.x_web import XWebCollector, browser_proxy, read_profile
from app.utils.channel_parser import parse_channel_input


@pytest.mark.parametrize('value', ['@Example', 'example', 'https://x.com/Example', 'twitter.com/Example/status/123?s=20'])
def test_x_inputs(value):
    parsed = parse_channel_input(value)
    assert parsed.username == 'example'
    assert parsed.canonical_url == 'https://x.com/example'


@pytest.mark.parametrize('value', ['', 'https://x.com.evil.com/foo', 'https://evil.com/foo', 'https://x.com/home', 'https://x.com/i/lists/123', '@abcdefghijklmnop', 'https://x.com/user/likes', 'https://x.com:443/user'])
def test_invalid_x_inputs(value):
    with pytest.raises(ValueError):
        parse_channel_input(value)


def test_large_ids_are_lossless_in_browser_and_excel(app_client, fake_web):
    import io
    from openpyxl import load_workbook
    from tests.conftest import make_post
    identifier = 1859193287961234567
    fake_web.set_posts([make_post(identifier)])
    response = app_client.post('/api/channels', json={'channel_input': '@example'})
    assert response.json()['channel']['last_message_id'] == str(identifier)
    post = app_client.get('/api/posts').json()['items'][0]
    assert post['message_id'] == str(identifier)
    workbook = load_workbook(io.BytesIO(app_client.get('/api/exports/excel').content))
    assert workbook.active.cell(2, 4).value == str(identifier)


def test_invalid_timezone_is_rejected(app_client):
    assert app_client.put('/api/settings', json={'timezone': 'Not/AZone'}).status_code == 400


PROFILE = 'https://x.com/example'


def card(identifier, *, username='example', pinned=False, **changes):
    return {'href': f'https://x.com/{username}/status/{identifier}', 'datetime': '2026-11-17T09:15:00Z',
            'pinned': pinned, 'content': 'سلام', 'metrics': '', 'media_type': 'text', 'media_url': None, **changes}


def window(*cards, **changes):
    return {'url': PROFILE, 'title': 'Example (@Example) / X', 'hasNotice': False, 'notice': '', 'dialog': None,
            'emptyState': False, 'emptyText': '', 'nextY': 800, 'atEnd': True, 'loading': False, 'cards': list(cards), **changes}


def profile_page(*windows, status=200, url=PROFILE, stalled=None):
    """Recorded DOM windows; no browser binary or network needed for the sync/error tests."""
    page = Mock(url=url)
    page.goto.return_value = Mock(ok=status == 200, status=status)
    handles = [Mock(json_value=Mock(return_value=w)) for w in windows]
    if stalled is not None:
        handles.append(BrowserTimeout('timeout'))
        page.evaluate.return_value = stalled
    page.wait_for_function.side_effect = handles
    return page


def test_first_read_deduplicates_and_limits_by_exact_id():
    big = 1859193287961234567
    photo = card(big, media_type='photo', media_url='https://pbs.twimg.com/media/one.jpg', metrics='1,234 Views')
    page = profile_page(window(card(1, pinned=True), photo, photo),
                        window(card(big + 4, username='other'), card(102), card(101)))
    batch = read_profile(page, 'example', None, 2)
    assert (batch.source_mode, batch.channel_title) == ('web', 'Example')
    assert [p.message_id for p in batch.posts] == [big, 102]
    post = batch.posts[0]
    assert (post.post_url, post.content, post.views) == (PROFILE + f'/status/{big}', 'سلام', 1234)
    assert post.published_at.isoformat() == '2026-11-17T09:15:00+00:00'
    assert post.media_url == photo['media_url']
    assert batch.posts[1].views is None
    page.goto.assert_called_once_with(PROFILE, wait_until='domcontentloaded', timeout=30_000)


def test_incremental_read_does_not_stop_at_pinned_or_older_post():
    page = profile_page(window(card(100, pinned=True), card(90), card(103)), window(card(101), card(100)))
    assert [p.message_id for p in read_profile(page, 'example', 100, 1).posts] == [103, 101]


@pytest.mark.parametrize('page', [
    profile_page(status=401), profile_page(status=403), profile_page(status=404), profile_page(status=429),
    profile_page(status=503), profile_page(url='https://x.com/i/flow/login'),
    profile_page(window(hasNotice=True, notice='These posts are protected')),
    profile_page(window(hasNotice=True, notice='This account doesn’t exist')),
    profile_page(window(hasNotice=True, emptyState=True, notice='No posts yet. Log in to see posts.')),
    profile_page(window(card(101), dialog='Sign in to X')),
    profile_page(stalled=window(emptyText='Unrecognised shell')),
    profile_page(window(card(101, datetime='not a timestamp'))),
    profile_page(window(card(101, content=None))),
    profile_page(window(card(101, href='https://elsewhere.invalid/example/status/101'))),
])
def test_unavailable_pages_are_not_empty_successes(page):
    with pytest.raises(CollectorError):
        read_profile(page, 'example', None, 1)


def test_explicit_empty_and_short_public_timeline():
    empty = profile_page(window(hasNotice=True, emptyState=True, notice='@example hasn’t posted'))
    assert not read_profile(empty, 'example', None, 10).posts
    short = window(card(7))
    assert [p.message_id for p in read_profile(profile_page(short, stalled=short), 'example', None, 10).posts] == [7]


@pytest.mark.parametrize('failure', ['login', 'stalled_loader', 'missing_cursor', 'scroll_limit'])
async def test_incomplete_read_preserves_archive_and_cursor(db, monkeypatch, failure):
    import app.collectors.x_web as web
    first = profile_page(window(card(100)))
    if failure == 'login':
        later = profile_page(window(card(102)), window(card(102), dialog='Log in: private response text'))
    elif failure == 'scroll_limit':
        monkeypatch.setattr(web, '_MAX_SCROLLS', 1)
        later = profile_page(window(card(102)), window(card(101)))
    else:
        later = profile_page(window(card(102)), stalled=window(card(102), loading=failure == 'stalled_loader'))
    pages = iter([first, later])
    monkeypatch.setattr(XWebCollector, '_visit', staticmethod(
        lambda name, cursor, limit, proxy: read_profile(next(pages), name, cursor, limit)))
    manager = SyncManager(db)  # actual default collector, no credentials set
    ch = db.create_channel(parse_channel_input('@example'), initial_limit=1)
    assert (await manager.sync_channel(ch['id']))['new_saved_count'] == 1
    error = await manager.sync_channel(ch['id'], trigger_type='scheduled')
    assert error['status'] == 'error'
    assert error['previous_last_message_id'] == error['new_last_message_id'] == 100
    assert db.get_channel(ch['id'])['posts_count'] == 1
    log = db.list_fetch_logs()[0]
    assert log['source_mode'] == 'web' and log['trigger_type'] == 'scheduled'
    assert 'private response text' not in log['message']


def test_proxy_is_optional_and_keeps_credentials_out_of_server_url():
    settings = dict(proxy_enabled='1', proxy_type='http', proxy_host='::1', proxy_port='8080',
                    proxy_username='user', proxy_password='p@ss/word')
    assert browser_proxy(settings) == {'server': 'http://[::1]:8080', 'username': 'user', 'password': 'p@ss/word'}
    assert browser_proxy({**settings, 'proxy_enabled': '0'}) is None
    with pytest.raises(CollectorError, match='SOCKS5') as err:
        browser_proxy({**settings, 'proxy_type': 'socks5'})
    assert 'p@ss/word' not in str(err.value)
    with pytest.raises(CollectorError):
        browser_proxy({**settings, 'proxy_host': ''})


def test_rendered_profile_in_chromium():
    """All requests are fulfilled locally. Also exercises JS hydration and virtualised scrolling."""
    html = (Path(__file__).parent / 'fixtures' / 'profile.html').read_text(encoding='utf-8')
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except BrowserError as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip('Install Chromium (python -m playwright install chromium) for the rendered-DOM test')
            raise
        with browser:
            page = browser.new_page(locale='en-US')
            requests = []
            def serve(route):
                requests.append(route.request.url)
                if route.request.url == PROFILE:
                    assert not {'authorization', 'cookie'}.intersection(route.request.all_headers())
                    route.fulfill(content_type='text/html; charset=UTF-8', body=html)
                else:
                    route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg"/>')
            page.route('**/*', serve)
            result = read_profile(page, 'example', 100, 10)
    assert requests[0] == PROFILE
    assert all(url == PROFILE or url.startswith('https://pbs.twimg.com/media/') for url in requests)
    assert [p.message_id for p in result.posts] == [1859193287961234567, 101]
    photo, video = result.posts
    assert (photo.content, photo.views, photo.media_type) == ('سلام 🌿\nجهان', 1234, 'photo')
    assert photo.media_url == 'https://pbs.twimg.com/media/example.jpg'
    assert (video.content, video.views, video.media_type) == ('', None, 'video')
    assert video.media_url == 'https://pbs.twimg.com/media/preview.jpg'
