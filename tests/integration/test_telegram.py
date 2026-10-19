import asyncio
import json
from pathlib import Path

import aiohttp
from aiohttp import web
import pytest

from codex_bridge.telegram import BotAPI, TelegramError, Gateway
from conftest import update


async def test_commands_menu_is_scoped_to_owner(tmp_path):
    from codex_bridge.telegram import BOT_COMMANDS
    calls = []
    class API:
        async def call(self, method, params):
            calls.append((method, params))
            return True
    gateway = Gateway({'user_id': 123456}, tmp_path/'socket', tmp_path, tmp_path, API())
    try:
        await gateway.setup_menu()
        assert calls == [
            ('setMyCommands', {'commands': BOT_COMMANDS, 'scope': {'type': 'chat', 'chat_id': 123456}}),
            ('setChatMenuButton', {'chat_id': 123456, 'menu_button': {'type': 'commands'}}),
        ]
        assert len({c['command'] for c in BOT_COMMANDS}) == len(BOT_COMMANDS) == 13
        assert all(1 <= len(c['description']) <= 256 for c in BOT_COMMANDS)
        assert 'locally' in next(c['description'] for c in BOT_COMMANDS if c['command'] == 'clear')
        assert all(c['description'].isascii() for c in BOT_COMMANDS)
    finally:
        gateway.state.close()


async def test_menu_registration_failure_closes_gateway(tmp_path):
    closed = []
    class API:
        def __init__(self): self.session = self
        async def call(self, *args): raise TelegramError('Telegram API error 401', 401)
        async def close(self): closed.append(True)
    gateway = Gateway({'user_id': 123456}, tmp_path/'socket', tmp_path, tmp_path, API())
    from codex_bridge.security import Rejected
    with pytest.raises((TelegramError, Rejected)):
        await gateway.run()
    assert closed == [True]
    assert not gateway.jobs


async def test_passive_delivery_does_not_wait_for_menu_or_incoming_message(tmp_path, monkeypatch):
    menu_started, menu_release, poll_started, sent = [asyncio.Event() for _ in range(4)]
    calls, closed = [], []

    class API:
        def __init__(self): self.session = self
        async def call(self, method, params):
            calls.append(method)
            if method == 'setMyCommands':
                menu_started.set()
                await menu_release.wait()
            elif method == 'getUpdates':
                poll_started.set()
                await asyncio.Event().wait()
            return True
        async def send(self, uid, body, photo, edit_id):
            assert uid == 123456 and body['text'] == 'Codex completed a local task'
            assert photo is None and edit_id is None
            sent.set()
            return {'message_id': 95}
        async def close(self): closed.append(True)

    gateway = Gateway({'user_id': 123456}, tmp_path/'socket', tmp_path, tmp_path, API())
    delivered = False
    async def ipc_call(socket, op, data=None):
        nonlocal delivered
        if op == 'events':
            if delivered: return []
            return [{'id': 1, 'project': 'a', 'text': 'Codex completed a local task', 'final': True}]
        assert op == 'ack' and data == {'id': 1, 'message_id': 95}
        delivered = True
        return {}
    monkeypatch.setattr('codex_bridge.telegram.ipc.call', ipc_call)
    running = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(menu_started.wait(), 1)
        await asyncio.wait_for(poll_started.wait(), 1)
        await asyncio.wait_for(sent.wait(), 1)
        assert delivered and not running.done()
        menu_release.set()
        async with asyncio.timeout(1):
            while 'setChatMenuButton' not in calls: await asyncio.sleep(0)
        assert not running.done()
    finally:
        gateway.stop.set()
        await asyncio.wait_for(running, 2)
    assert closed == [True]


async def mock_server(handler):
    app = web.Application()
    app.router.add_post('/{method}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f'http://127.0.0.1:{port}/'


async def test_mock_api_unicode_and_rate_limit(monkeypatch):
    seen = []
    async def handler(request):
        seen.append(await request.json())
        if len(seen) == 1:
            return web.json_response({'ok': False, 'error_code': 429, 'parameters': {'retry_after': 1}})
        return web.json_response({'ok': True, 'result': {'message_id': 100}})
    runner, base = await mock_server(handler)
    api = BotAPI('not-a-real-token')
    api.base = base
    original_sleep = asyncio.sleep
    delays = []
    async def fast_sleep(delay):
        delays.append(delay)
        await original_sleep(0)
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
    try:
        assert await api.call('sendMessage', {'text': 'кириллица 😀', 'chat_id': 123}) == {'message_id': 100}
        assert seen[-1]['text'] == 'кириллица 😀'
        assert delays == [1]
    finally:
        await api.session.close()
        await runner.cleanup()


async def test_token_not_in_errors():
    secret = '123456789:'+'X'*35
    async def handler(request):
        return web.json_response({'ok': False, 'error_code': 401, 'description': secret})
    runner, base = await mock_server(handler)
    api = BotAPI(secret)
    api.base = base
    try:
        with pytest.raises(TelegramError) as exc:
            await api.call('getMe', {})
        assert secret not in str(exc.value)
    finally:
        await api.session.close()
        await runner.cleanup()


async def test_gateway_auth_before_ipc_download(tmp_path, monkeypatch):
    class API:
        async def download_voice(self, *args):
            pytest.fail('unauthorized download')
    gateway = Gateway({'user_id': 123456}, tmp_path/'socket', tmp_path, tmp_path, API())
    async def forbidden(*args, **kwargs):
        pytest.fail('unauthorized IPC')
    monkeypatch.setattr('codex_bridge.ipc.call', forbidden)
    try:
        await gateway.process_update(update('/new', uid=99))
        await gateway.process_update(update(number=1, chat_type='group', callback='p:a'))
    finally:
        gateway.state.close()


async def test_retry_limit_on_server_failure(monkeypatch):
    attempts=[]
    async def handler(request):
        attempts.append(1)
        return web.json_response({'ok':False,'error_code':503})
    runner,base=await mock_server(handler)
    api=BotAPI('fake');api.base=base
    async def no_delay(_):pass
    monkeypatch.setattr(asyncio,'sleep',no_delay)
    try:
        with pytest.raises(TelegramError,match='retry limit'):
            await api.call('getMe',{})
        assert len(attempts)==6
    finally:
        await api.session.close();await runner.cleanup()


async def test_polling_auth_failure_is_fatal_and_safe(tmp_path):
    from codex_bridge.security import Rejected
    class API:
        async def call(self,*args):raise TelegramError('Telegram API error 401',401)
    gateway=Gateway({'user_id':123456},tmp_path/'socket',tmp_path,tmp_path,API())
    try:
        with pytest.raises(Rejected,match='401'):
            await gateway.polling()
    finally:
        gateway.state.close()


async def test_streamed_api_response_is_read_to_end():
    async def handler(request):
        response=web.StreamResponse(headers={'Content-Type':'application/json'})
        await response.prepare(request)
        await response.write(b'{"ok":true,"result":')
        await asyncio.sleep(.02)
        await response.write(b'[{"update_id":12}]}')
        await response.write_eof()
        return response
    runner,base=await mock_server(handler)
    api=BotAPI('fake');api.base=base
    try:
        assert await api.call('getUpdates',{})==[{'update_id':12}]
    finally:
        await api.session.close();await runner.cleanup()


async def test_missing_controller_socket_restarts_gateway_without_consuming_update(tmp_path, monkeypatch):
    from codex_bridge.security import Rejected
    closed = []
    class API:
        def __init__(self): self.session = self
        async def call(self, method, params):
            if method == 'getUpdates':
                return [update('/status', number=42)]
            return True
        async def close(self): closed.append(True)
    gateway = Gateway({'user_id': 123456}, tmp_path/'missing/socket', tmp_path, tmp_path, API())
    gateway.setting('offset', 42)
    with pytest.raises(Rejected, match='Controller socket'):
        await asyncio.wait_for(gateway.run(), 2)
    assert closed == [True] and not gateway.jobs
    import sqlite3
    db = sqlite3.connect(tmp_path/'gateway.db')
    try:
        assert db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()[0] == '42'
    finally: db.close()
