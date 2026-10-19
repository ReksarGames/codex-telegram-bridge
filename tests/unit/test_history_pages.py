import pytest

from codex_bridge.rpc import RPCError


async def test_metadata_pages_preserve_old_turns_without_loading_outputs(controller):
    c = controller
    c.rpc.thread['turns'] = [{'id':str(i), 'status':'completed', 'items':[]} for i in range(121)]
    c.rpc.thread['turns'][0]['items'] = [{'type':'functionCallOutput', 'output':'x'*17*1024**2}]
    thread = await c.read_thread('thread-a')
    assert [t['id'] for t in thread['turns']] == [str(i) for i in range(121)]
    assert all(not t['items'] for t in thread['turns'])
    assert c.rpc.thread['turns'][0]['items'], 'History was modified'
    reads = [p for m,p in c.rpc.calls if m == 'thread/read']
    assert reads == [{'threadId':'thread-a', 'includeTurns':False}]
    pages = [p for m,p in c.rpc.calls if m == 'thread/turns/list']
    assert len(pages) == 3 and all(p['itemsView'] == 'notLoaded' for p in pages)


async def test_recovery_finds_client_id_across_pages_without_retry(controller):
    c = controller
    task = c.store.enqueue('a','sent task',None)
    c.store.task_state(task,'unknown')
    c.rpc.thread['turns'] = [{'id':str(i), 'status':'completed', 'items':[]} for i in range(3)]
    c.rpc.thread['turns'][1]['items'] = [{'type':'userMessage', 'clientId':task}]
    await c.reconcile('a')
    assert c.store.active('a') is None and c.store.project('a')['state'] == 'idle'
    assert not any(m == 'turn/start' for m,_ in c.rpc.calls)
    assert any(m == 'thread/resume' and p['excludeTurns'] for m,p in c.rpc.calls)
    pages = [p for m,p in c.rpc.calls if m == 'thread/turns/list']
    assert len(pages) == 3 and all(p['limit'] == 1 for p in pages)


async def test_repeating_history_cursor_fails_closed(controller):
    c = controller
    async def call(method,params):
        return {'data':[], 'nextCursor':'same-cursor'}
    c.rpc.call = call
    with pytest.raises(RPCError,match='did not advance'):
        async for _ in c.history_pages('thread/turns/list',{'threadId':'thread-a'}): pass


async def test_question_messages_are_paged_in_order_without_tool_outputs(controller):
    c = controller
    items = [{'type':'agentMessage','id':'question'}, {'type':'functionCallOutput','output':'large tool output'}, {'type':'userMessage','id':'reply'}]
    c.rpc.thread['turns'] = [{'id':'turn-1','status':'inProgress','items':items}]
    messages = await c.read_turn_messages('thread-a','turn-1')
    assert [m['id'] for m in messages] == ['question','reply']
    pages = [p for m,p in c.rpc.calls if m == 'thread/items/list']
    assert len(pages) == 3 and all(p['limit'] == 1 and p['turnId'] == 'turn-1' for p in pages)


async def test_question_page_from_other_turn_is_rejected(controller):
    c = controller
    async def call(method,params):
        return {'data':[{'turnId':'foreign-turn','item':{'type':'userMessage'}}], 'nextCursor':None}
    c.rpc.call = call
    with pytest.raises(RPCError,match='another turn'):
        await c.read_turn_messages('thread-a','turn-1')
