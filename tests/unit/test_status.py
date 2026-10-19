import time

import pytest

from conftest import activate, update
from codex_bridge.security import Rejected


async def status(c):
    await c.command(c.store.project('a'), '/status')
    return (await c.handle('events', {}))[-1]


async def test_idle_runtime_overrides_stale_bridge_state_without_unblocking_queue(controller):
    c = controller
    c.store.state('a', 'unknown', 'old-turn')
    c.store.enqueue('a', 'still queued', None)
    c.rpc.thread['turns'] = [{'id': 'old-turn', 'status': 'completed', 'items': []}]
    body = await status(c)
    assert 'Codex: ⚪ Idle — no active task' in body['text'] and 'Busy: no' in body['text']
    assert 'Bridge: ⚠️ Needs recovery' in body['text'] and 'Queue: 1.' in body['text']
    assert c.store.project('a')['state'] == 'unknown' and c.store.claim('a') is None
    assert body['final'] and body['buttons'] == [[['🔄 Refresh', 's:a']]]
    assert all(m in {'thread/read', 'thread/turns/list'} for m, _ in c.rpc.calls)
    assert all(not p.get('includeTurns') for m, p in c.rpc.calls if m == 'thread/read')


async def test_local_task_has_live_activity_and_elapsed_without_exposing_outputs(controller, monkeypatch):
    c = controller
    c.rpc.thread['status'] = {'type': 'active', 'activeFlags': []}
    c.rpc.thread['turns'] = [{'id': 'turn-local', 'status': 'inProgress', 'startedAt': 1000,
                            'items': [{'type': 'commandExecution', 'aggregatedOutput': 'secret raw output'}]}]
    monkeypatch.setattr(time, 'time', lambda: 1045)
    await c.on_event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': {'id': 'turn-local'}}})
    await c.on_event({'method': 'item/started', 'params': {'threadId': 'thread-a', 'turnId': 'turn-local',
        'item': {'id': 'private-command', 'type': 'commandExecution', 'command': 'cat .env'}}})
    body = await status(c)
    assert 'Busy: yes' in body['text'] and 'Elapsed: 45 sec.' in body['text']
    assert 'Local' in body['text'] and 'Latest activity: Command execution' in body['text']
    assert 'secret raw output' not in body['text'] and 'cat .env' not in body['text']
    assert not c.store.active('a')


@pytest.mark.parametrize('flags,expected', [
    (['waitingOnApproval'], 'Awaiting approval'),
    (['waitingOnUserInput'], 'Awaiting your reply'),
    ([], 'Running'),
])
async def test_runtime_wait_flags_and_bridge_queue_source(controller, flags, expected):
    c = controller
    activate(c)
    c.rpc.thread['status']['activeFlags'] = flags
    body = await status(c)
    assert expected in body['text'] and 'Busy: yes' in body['text']
    assert 'Source: Bridge queue' in body['text']
    assert not c.rpc.responses


async def test_reference_gate_and_async_question_are_distinct(controller):
    c = controller
    activate(c)
    c.store.state('a', 'waiting_reference', 'turn-1')
    c.request(c.store.project('a'), 'async_question', {'question': 'fixture'})
    body = await status(c)
    assert 'Codex: 🎨 Awaiting review' in body['text']
    assert 'Open async questions: 1 (Codex may continue working)' in body['text']


async def test_old_activity_does_not_leak_into_next_turn_or_project(controller):
    c = controller
    activate(c)
    c.activity['a'] = ('thread-a', 'old-turn', 'WRONG_ACTIVITY', time.time())
    c.activity['b'] = ('thread-b', 'turn-1', 'OTHER_PROJECT', time.time())
    body = await status(c)
    assert 'WRONG_ACTIVITY' not in body['text'] and 'OTHER_PROJECT' not in body['text']
    assert 'no activity event observed yet' in body['text']


@pytest.mark.parametrize('failure', ['disconnected', 'network', 'wrong-session', 'system-error'])
async def test_unverified_status_does_not_claim_idle_or_alter_task(controller, failure):
    c = controller
    task = activate(c)
    if failure == 'disconnected': c.rpc.connected = False
    if failure == 'network': c.rpc.fail = 'thread/read'
    if failure == 'wrong-session': c.rpc.thread['sessionId'] = 'other-session'
    if failure == 'system-error': c.rpc.thread['status'] = {'type': 'systemError'}
    body = await status(c)
    assert 'Busy: unverified' in body['text'] and 'Busy: no' not in body['text']
    assert c.store.active('a')['id'] == task and c.store.project('a')['state'] == 'running'
    assert not c.rpc.responses


async def test_refresh_stays_bound_to_project_without_switching_selection(controller):
    c = controller
    await c.callback('p:b')
    await c.handle('update', {'update': update(number=1, callback='s:a')})
    assert c.current()['id'] == 'b'
    assert c.rpc.calls[-1][1]['threadId'] == 'thread-a'
    for callback in ('s:missing', 's:../../etc'):
        with pytest.raises(Rejected): await c.callback(callback)
    before = len(c.rpc.calls)
    with pytest.raises(Rejected):
        await c.handle('update', {'update': update(number=2, uid=99, callback='s:a')})
    assert len(c.rpc.calls) == before


async def test_status_without_session_and_runtime_not_loaded(controller):
    c = controller
    c.store.db.execute("UPDATE projects SET thread=NULL,session=NULL WHERE id='a'")
    body = await status(c)
    assert 'No project session' in body['text'] and 'Busy: no' in body['text']
    assert not c.rpc.calls
    c.store.bind('a', {'id': 'thread-a', 'sessionId': 'session-a'})
    c.rpc.thread['status'] = {'type': 'notLoaded'}
    body = await status(c)
    assert 'Session is not loaded' in body['text'] and 'Busy: no' in body['text']


async def test_latest_turn_uses_one_metadata_page_and_no_old_large_history(controller):
    c = controller
    c.rpc.thread['status'] = {'type': 'active', 'activeFlags': []}
    c.rpc.thread['turns'] = [{'id': str(i), 'status': 'completed', 'items': []} for i in range(100)]
    c.rpc.thread['turns'][0]['items'] = [{'type': 'tool', 'output': 'x'*17*1024**2}]
    c.rpc.thread['turns'].append({'id': 'current-turn', 'status': 'inProgress', 'items': []})
    body = await status(c)
    assert 'Current turn: current-' in body['text']
    pages = [p for m, p in c.rpc.calls if m == 'thread/turns/list']
    assert pages == [{'threadId': 'thread-a', 'sortDirection': 'desc', 'itemsView': 'notLoaded', 'limit': 1}]
