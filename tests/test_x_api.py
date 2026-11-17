import httpx
import pytest

from app.collectors.base import CollectorError
from app.collectors.x_api import XAPICollector
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


def tweet(identifier):
    return {'id': str(identifier), 'text': 'سلام', 'created_at': '2026-11-17T09:00:00Z', 'public_metrics': {'impression_count': 12}}


async def test_incremental_pagination_and_media():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer test-secret'
        if '/by/username/' in request.url.path:
            return httpx.Response(200, json={'data': {'id': '42', 'name': 'Example'}})
        assert request.url.params['since_id'] == '100'
        if 'pagination_token' not in request.url.params:
            item = tweet(102)
            item['attachments'] = {'media_keys': ['photo1']}
            return httpx.Response(200, json={'data': [item], 'includes': {'media': [{'media_key': 'photo1', 'type': 'photo', 'url': 'https://example.com/photo.jpg'}]}, 'meta': {'next_token': 'next'}})
        assert request.url.params['pagination_token'] == 'next'
        return httpx.Response(200, json={'data': [tweet(101)], 'meta': {}})
    collector = XAPICollector(httpx.MockTransport(handler))
    result = await collector.fetch_channel_posts(username='example', last_message_id=100, initial_limit=10, settings={'x_bearer_token': 'test-secret'})
    assert [p.message_id for p in result.posts] == [102, 101]
    assert result.posts[0].media_type == 'photo'
    assert result.posts[0].views == 12
    assert result.posts[0].post_url == 'https://x.com/example/status/102'
    assert len(calls) == 3


async def test_initial_request_only_fetches_recent_window():
    calls = []
    def handler(request):
        calls.append(request)
        if '/by/username/' in request.url.path:
            return httpx.Response(200, json={'data': {'id': '42'}})
        assert request.url.params['max_results'] == '10'
        assert 'since_id' not in request.url.params
        return httpx.Response(200, json={'data': [tweet(i) for i in range(20, 10, -1)], 'meta': {'next_token': 'older'}})
    result = await XAPICollector(httpx.MockTransport(handler)).fetch_channel_posts(username='example', last_message_id=None, initial_limit=10, settings={'x_bearer_token': 'secret'})
    assert len(result.posts) == 10
    assert len(calls) == 2


@pytest.mark.parametrize('code', [401, 403, 404, 429, 500])
async def test_api_errors_do_not_leak_response_body(code):
    collector = XAPICollector(httpx.MockTransport(lambda r: httpx.Response(code, text='test-secret')))
    with pytest.raises(CollectorError) as err:
        await collector.fetch_channel_posts(username='example', last_message_id=None, initial_limit=10, settings={'x_bearer_token': 'test-secret'})
    assert 'test-secret' not in str(err.value)


async def test_missing_token():
    with pytest.raises(CollectorError, match='Bearer Token'):
        await XAPICollector().fetch_channel_posts(username='example', last_message_id=None, initial_limit=10, settings={})


async def test_later_page_failure_preserves_cursor_and_archive(db):
    from app.collectors.manager import SyncManager
    channel = db.create_channel(parsed=parse_channel_input('@example'), interval_value=6, interval_unit='hours', initial_limit=10, title=None, is_active=True)
    db.update_settings({'x_bearer_token': 'secret'})
    mode = ['first']
    def handler(request):
        if '/by/username/' in request.url.path:
            return httpx.Response(200, json={'data': {'id': '42'}})
        if mode[0] == 'first':
            return httpx.Response(200, json={'data': [tweet(100)]})
        if 'pagination_token' in request.url.params:
            return httpx.Response(429, text='secret')
        return httpx.Response(200, json={'data': [tweet(102)], 'meta': {'next_token': 'next'}})
    manager = SyncManager(db, web_collector=XAPICollector(httpx.MockTransport(handler)))
    assert (await manager.sync_channel(channel['id']))['status'] == 'success'
    mode[0] = 'fail'
    assert (await manager.sync_channel(channel['id']))['status'] == 'error'
    assert db.get_channel(channel['id'])['last_message_id'] == 100
    assert db.get_channel(channel['id'])['posts_count'] == 1


def test_mask_preserves_and_empty_clears_token(app_client):
    app_client.put('/api/settings', json={'x_bearer_token': 'secret'})
    app_client.put('/api/settings', json={'x_bearer_token': '********'})
    assert app_client.get('/api/settings').json()['settings']['x_bearer_token_set']
    app_client.put('/api/settings', json={'x_bearer_token': ''})
    assert not app_client.get('/api/settings').json()['settings']['x_bearer_token_set']


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
