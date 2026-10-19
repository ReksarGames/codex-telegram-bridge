import copy
import uuid

import pytest

from codex_bridge.rpc import RPCError
from codex_bridge.security import Rejected
from conftest import activate

TARGET = '11111111-2222-4333-8444-555555555555'


@pytest.fixture
def handoff(controller):
    c = controller
    old = copy.deepcopy(c.rpc.thread)
    target = {**copy.deepcopy(old), 'id': TARGET, 'sessionId': TARGET,
              'status': {'type': 'notLoaded'},
              'turns': [{'id': 'saved-turn', 'status': 'completed', 'items': []}]}
    state = {'old': old, 'target': target, 'loaded': ['thread-a'], 'detached': [],
             'attached': True, 'delay': 1, 'interrupted': False, 'native_queue': [],
             'fail_resume': False, 'bad_resume': None}

    async def detach(project, force=False):
        if state['attached'] and not force:
            raise Rejected('Подключён клиент')
        state['detached'].append((project['id'], force))

    async def call(method, params):
        c.rpc.calls.append((method, params))
        if method == 'thread/read':
            live = old if params['threadId'] == 'thread-a' else target
            if live is old and state['interrupted']:
                if state['delay']:
                    state['delay'] -= 1
                else:
                    old['status'] = {'type': 'idle'}
                    for turn in old['turns']:
                        if turn['status'] == 'inProgress': turn['status'] = 'interrupted'
            return {'thread': copy.deepcopy(live)}
        if method == 'thread/turns/list':
            live = old if params['threadId'] == 'thread-a' else target
            return {'data':copy.deepcopy(live['turns']), 'nextCursor':None}
        if method == 'thread/loaded/list': return {'data': list(state['loaded'])}
        if method == 'thread/queue/list': return {'data': list(state['native_queue'])}
        if method == 'thread/queue/delete':
            assert params['threadId'] == 'thread-a'
            state['native_queue'] = []
            return {'deleted': True}
        if method == 'turn/interrupt':
            assert params == {'threadId': 'thread-a', 'turnId': 'turn-1'}
            state['interrupted'] = True
            return {}
        if method == 'thread/archive':
            assert params['threadId'] == 'thread-a'
            assert not any(t['status'] == 'inProgress' for t in old['turns'])
            state['loaded'].remove('thread-a')
            return {}
        if method == 'thread/unarchive': return {'thread': copy.deepcopy(old)}
        if method == 'thread/resume':
            assert params['threadId'] == TARGET
            if state['fail_resume']: raise RPCError('private error', {'message': 'private error'})
            if target['status']['type'] == 'notLoaded': target['status'] = {'type': 'idle'}
            state['loaded'] = [TARGET]
            result = copy.deepcopy(target)
            if state['bad_resume'] == 'id': result['id'] = str(uuid.uuid4())
            if state['bad_resume'] == 'session': result['sessionId'] = str(uuid.uuid4())
            if state['bad_resume'] == 'cwd': result['cwd'] = str(c.config.projects[1].path)
            if state['bad_resume'] == 'history': target['turns'] = []
            return {'thread': result}
        pytest.fail('Unexpected RPC method: '+method)

    c.rpc.call = call
    c.terminal.detach_for_replace = detach
    state['controller'] = c
    return state


def running(state):
    task = activate(state['controller'])
    state['old']['status'] = {'type': 'active', 'activeFlags': []}
    state['old']['turns'] = [{'id': 'turn-1', 'status': 'inProgress', 'items': []}]
    return task


async def transfer(state, **flags):
    return await state['controller'].handle('migrate', {'project': 'a', 'thread': TARGET,
        'old_process_stopped': True, **flags}, local=True)


async def test_force_handoff_waits_for_interrupt_preserves_history_and_queue(handoff):
    s = handoff; c = s['controller']; task = running(s)
    queued = c.store.enqueue('a', 'next task', None)
    token = c.request(c.store.project('a'), 'approval', {}, 9)
    s['native_queue'] = [{'id': 'native-old', 'clientUserMessageId': 'local-old'}]
    other = c.store.project('b')
    result = await transfer(s, force=True)
    assert result['thread'] == result['session'] == TARGET and result['state'] == 'idle'
    assert result['paused'] == 1
    assert c.store.project('b') == other
    assert c.store.db.execute('SELECT state FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'interrupted'
    assert c.store.db.execute('SELECT state FROM tasks WHERE id=?', (queued,)).fetchone()[0] == 'queued'
    assert c.store.db.execute('SELECT COUNT(*) FROM sessions WHERE project=?', ('a',)).fetchone()[0] == 2
    assert c.store.db.execute('SELECT state FROM requests WHERE token=?', (token,)).fetchone()[0] == 'stale'
    assert token not in c.requests
    calls = c.rpc.calls
    assert calls[0] == ('thread/read', {'threadId': TARGET, 'includeTurns': False})
    methods = [m for m, _ in calls]
    assert methods.index('thread/queue/delete') < methods.index('turn/interrupt') < methods.index('thread/archive') < methods.index('thread/resume')
    assert methods.count('thread/read') >= 5
    assert 'thread/start' not in methods
    assert s['detached'] == [('a', True)]


@pytest.mark.parametrize('problem', ['wrong_cwd', 'missing', 'foreign_live', 'busy', 'other_project', 'other_session', 'subagent'])
async def test_preflight_failure_never_closes_old_chat(handoff, problem):
    s = handoff; c = s['controller']
    flags = {'force': True}
    if problem == 'wrong_cwd': s['target']['cwd'] = str(c.config.projects[1].path)
    if problem == 'subagent': s['target']['parentThreadId'] = 'parent'
    if problem == 'foreign_live': flags['old_process_stopped'] = False
    if problem == 'busy': running(s); flags['force'] = False
    if problem == 'other_project': c.store.bind('b', s['target'])
    if problem == 'other_session': c.store.bind('b', {'id':'other-thread','sessionId':TARGET})
    if problem == 'missing':
        async def missing(*args): raise RPCError('private missing detail')
        c.rpc.call = missing
    original = c.store.project('a')
    with pytest.raises(Rejected): await transfer(s, **flags)
    assert c.store.project('a') == original
    assert not s['detached']
    assert not any(m in {'turn/interrupt', 'thread/archive', 'thread/resume'} for m, _ in c.rpc.calls)


@pytest.mark.parametrize('failure', ['resume_error', 'id', 'session', 'cwd', 'history'])
async def test_failed_target_activation_never_reports_success_or_binds(handoff, failure):
    s = handoff; c = s['controller']
    s['fail_resume'] = failure == 'resume_error'
    s['bad_resume'] = failure
    with pytest.raises(Rejected): await transfer(s, force=True)
    assert c.store.project('a')['thread'] == 'thread-a'
    assert c.store.project('a')['state'] == 'unknown'
    assert c.store.project('a')['paused'] == 1
    assert not any(m == 'thread/start' for m, _ in c.rpc.calls)


async def test_interrupt_timeout_keeps_binding_and_blocks_queue(handoff,monkeypatch):
    import asyncio
    s=handoff; c=s['controller']; running(s)
    real_timeout=asyncio.timeout
    class Timeout:
        async def __aenter__(self): raise TimeoutError
        async def __aexit__(self,*args): pass
    monkeypatch.setattr(asyncio,'timeout',lambda seconds:Timeout() if seconds == 15 else real_timeout(seconds))
    with pytest.raises(Rejected,match='old turn ended'):
        await transfer(s,force=True)
    assert c.store.project('a')['thread']=='thread-a'
    assert c.store.project('a')['state']=='unknown'
    assert c.store.project('a')['paused']==1
    assert not any(m in {'thread/archive','thread/resume'} for m,_ in c.rpc.calls)


async def test_unknown_delivery_cannot_be_discarded_by_force(handoff):
    s=handoff; c=s['controller']
    task=c.store.enqueue('a','unknown delivery',None)
    c.store.task_state(task,'unknown');c.store.state('a','unknown')
    with pytest.raises(Rejected,match='delivery is unknown'):
        await transfer(s,force=True)
    assert not s['detached'] and c.store.project('a')['thread']=='thread-a'


async def test_gateway_cannot_force_handoff(handoff):
    c=handoff['controller']
    with pytest.raises(Rejected,match='locally'):
        await c.handle('migrate',{'project':'a','thread':TARGET,'force':True,'old_process_stopped':True})
    assert not c.rpc.calls


async def test_current_already_bound_thread_rejoins_without_interrupt(handoff):
    s=handoff; c=s['controller']
    c.store.bind('a',s['target']);s['loaded']=[TARGET]
    task=activate(c)
    s['target']['turns']=[{'id':'turn-1','status':'inProgress','items':[]}]
    s['target']['status']={'type':'active','activeFlags':[]}
    result=await transfer(s,force=True,old_process_stopped=False)
    assert result['thread']==TARGET and result['state']=='running'
    assert c.store.active('a')['id']==task
    assert not s['detached']
    assert not any(m in {'thread/archive','turn/interrupt'} for m,_ in c.rpc.calls)
    assert any(m=='thread/resume' for m,_ in c.rpc.calls)


async def test_binding_transaction_rolls_back_after_partial_write(handoff,monkeypatch):
    s=handoff;c=s['controller'];real_bind=c.store.bind
    def failed_bind(project,thread):
        real_bind(project,thread)
        raise OSError('disk fixture failure')
    monkeypatch.setattr(c.store,'bind',failed_bind)
    with pytest.raises(OSError):await transfer(s,force=True)
    assert c.store.project('a')['thread']=='thread-a'
    assert c.store.project('a')['state']=='unknown'
    assert c.store.project('a')['paused']==1
    assert c.store.db.execute("SELECT COUNT(*) FROM sessions WHERE project='a'").fetchone()[0]==1
    assert not c.store.db.in_transaction


async def test_force_flag_must_be_boolean(handoff):
    c=handoff['controller']
    with pytest.raises(Rejected,match='flags'):await transfer(handoff,force='yes')
    assert not c.rpc.calls and not handoff['detached']
