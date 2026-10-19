import json

import pytest

from codex_bridge.controller import question_replies, text_input
from codex_bridge.security import Rejected
from conftest import activate, update


def question_event(questions=None):
    return {'method': 'item/completed', 'params': {'threadId': 'thread-a', 'turnId': 'turn-1',
        'item': {'type': 'agentMessage', 'id': 'call-question', 'phase': 'final_answer',
                 'delivery': 'async', 'text': 'Which option?',
                 'questions': questions if questions is not None else [{'title': 'Какой вариант?', 'options': ['Первый', 'Второй']} ]}}}


async def ask(c, questions=None):
    activate(c)
    event = question_event(questions)
    c.rpc.thread['turns'][0]['items'] = [event['params']['item']]
    await c.on_event(event)
    return c.store.db.execute("SELECT token FROM requests WHERE kind='async_question' ORDER BY rowid").fetchall()


def local_reply(qid):
    return {'type': 'userMessage', 'content': text_input('<send_user_message_question_reply>\n' +
        json.dumps([{'questionItemId': qid, 'question': 'Question', 'answer': 'Local answer'}]) +
        '\n</send_user_message_question_reply>')}


async def test_async_menu_is_delivered_without_result_mirroring_or_blocking(controller):
    c = controller
    tokens = await ask(c)
    await c.on_event(question_event())
    bodies = await c.handle('events', {})
    assert len(tokens) == len(bodies) == 1
    body = bodies[0]
    assert 'Какой вариант?' in body['text'] and '1. Первый' in body['text']
    assert body['final'] and body['buttons'][0][0][0] == 'Первый'
    assert body['buttons'][1][0][0] == 'Второй'
    assert c.store.project('a')['state'] == 'running'
    assert not c.rpc.responses and not c.answers


async def test_async_choice_goes_to_original_turn_once_after_project_switch(controller):
    c = controller
    token = (await ask(c))[0]['token']
    await c.callback('p:b')
    await c.callback(f'q:{token}:o1')
    method, params = c.rpc.calls[-1]
    assert method == 'turn/steer' and params['threadId'] == 'thread-a' and params['expectedTurnId'] == 'turn-1'
    replies = question_replies({'content': params['input']})
    assert replies == {'["request_user_input_async","call-question",0]'}
    assert 'Второй' in params['input'][0]['text']
    assert not c.rpc.responses and c.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    with pytest.raises(Rejected):
        await c.callback(f'q:{token}:o1')
    assert sum(method == 'turn/steer' for method, _ in c.rpc.calls) == 1


async def test_multiple_questions_require_explicit_reply_and_preserve_siblings(controller):
    c = controller
    tokens = await ask(c, [{'title': 'First?'}, {'title': 'Second?', 'options': ['yes', 'no']}])
    assert len(await c.handle('events', {})) == 2
    with pytest.raises(Rejected):
        await c.handle('update', {'update': update('Ambiguous answer')})
    await c.callback(f"q:{tokens[1]['token']}:edit")
    await c.handle('update', {'update': update('My other answer', number=2)})
    assert c.store.db.execute('SELECT state FROM requests WHERE token=?', (tokens[0]['token'],)).fetchone()[0] == 'pending'
    assert '[\\"request_user_input_async\\",\\"call-question\\",1]' in c.rpc.calls[-1][1]['input'][0]['text']


async def test_open_question_reply_and_voice_confirmation_follow_origin(controller):
    c = controller
    token = (await ask(c, [{'title': 'Any details?'}]))[0]['token']
    body = (await c.handle('events', {}))[0]
    await c.handle('ack', {'id': body['id'], 'message_id': 99})
    await c.callback(f'q:{token}:edit')
    context = c.context(c.store.project('a'))
    voice = c.request(c.store.project('a'), 'voice', {'context': context, 'text': 'Voice answer'})
    await c.callback('p:b')
    await c.callback(f'v:{voice}:send')
    assert c.rpc.calls[-1][0] == 'turn/steer' and c.rpc.calls[-1][1]['threadId'] == 'thread-a'
    assert 'Voice answer' in c.rpc.calls[-1][1]['input'][0]['text']


@pytest.mark.parametrize('kind', ['named-answer', 'follow-up'])
async def test_local_reply_invalidates_remote_callback_before_event_arrives(controller, kind):
    c = controller
    token = (await ask(c))[0]['token']
    qid = json.loads(c.store.db.execute('SELECT payload FROM requests WHERE token=?', (token,)).fetchone()[0])['questionItemId']
    reply = local_reply(qid) if kind == 'named-answer' else {'type': 'userMessage', 'content': text_input('New instruction')}
    c.rpc.thread['turns'][0]['items'].append(reply)
    with pytest.raises(Rejected):
        await c.callback(f'q:{token}:o0')
    assert not any(method == 'turn/steer' for method, _ in c.rpc.calls)
    notices = await c.handle('events', {})
    assert len(notices) == 1 and 'buttons' not in notices[0] and 'no longer active' in notices[0]['text']


async def test_local_answer_event_dismisses_only_named_question(controller):
    c = controller
    rows = await ask(c, [{'title': 'First?'}, {'title': 'Second?'}])
    qid = json.loads(c.store.db.execute('SELECT payload FROM requests WHERE token=?', (rows[0]['token'],)).fetchone()[0])['questionItemId']
    await c.on_event({'method': 'item/completed', 'params': {'threadId': 'thread-a', 'turnId': 'turn-1', 'item': local_reply(qid)}})
    assert [r[0] for r in c.store.db.execute('SELECT state FROM requests ORDER BY rowid')] == ['resolved', 'pending']


@pytest.mark.parametrize('change', ['turn', 'thread', 'expiry', 'epoch', 'missing-origin', 'network'])
async def test_async_stale_requests_and_failed_delivery_are_not_retried(controller, change):
    c = controller
    token = (await ask(c))[0]['token']
    if change == 'turn': c.rpc.thread['turns'][0]['status'] = 'completed'
    if change == 'thread': c.store.bind('a', {'id': 'new', 'sessionId': 'new'})
    if change == 'expiry': c.store.db.execute('UPDATE requests SET expires=0')
    if change == 'epoch': c.epoch = 'new-epoch'
    if change == 'missing-origin': c.rpc.thread['turns'][0]['items'] = []
    if change == 'network': c.rpc.fail = 'turn/steer'
    with pytest.raises(Rejected): await c.callback(f'q:{token}:o0')
    if change == 'network':
        assert c.store.project('a')['state'] == 'unknown'
        with pytest.raises(Rejected): await c.callback(f'q:{token}:o0')
    assert not c.rpc.responses


async def test_fast_completion_keeps_question_notification_but_removes_buttons(controller):
    c = controller
    token = (await ask(c))[0]['token']
    await c.on_event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': 'turn-1', 'status': 'completed', 'items': [question_event()['params']['item']]}}})
    notices = await c.handle('events', {})
    assert len(notices) == 2 and 'Codex has a question' in notices[0]['text']
    assert 'buttons' not in notices[0] and 'no longer active' in notices[0]['text']
    assert 'Which option?' not in notices[1]['text']
    with pytest.raises(Rejected): await c.callback(f'q:{token}:o0')


async def test_sensitive_question_is_never_stored_or_sent(controller):
    c = controller
    await ask(c, [{'title': 'password=private-value'}])
    assert not c.store.db.execute('SELECT 1 FROM requests').fetchone()
    assert 'private-value' not in str(await c.handle('events', {}))


async def test_voice_reply_after_switch_uses_specific_origin_question(controller):
    c = controller
    await ask(c, [{'title': 'First?'}, {'title': 'Second?'}])
    bodies = await c.handle('events', {})
    await c.handle('ack', {'id': bodies[0]['id'], 'message_id': 91})
    await c.callback('p:b')
    message = update(number=3)
    message['message'].pop('text')
    message['message'].update(voice={'duration': 3, 'file_size': 100, 'file_id': 'fixture'},
                              reply_to_message={'message_id': 91})
    result = await c.handle('update', {'update': message})
    token = result['voice']['token']
    await c.handle('voice', {'token': token, 'text': 'Voice from other selected project'})
    await c.callback(f'v:{token}:send')
    assert c.rpc.calls[-1][1]['threadId'] == 'thread-a'
    assert '[\\"request_user_input_async\\",\\"call-question\\",0]' in c.rpc.calls[-1][1]['input'][0]['text']


async def test_async_reply_to_message_after_switch_uses_origin(controller):
    c = controller
    await ask(c)
    body = (await c.handle('events', {}))[0]
    await c.handle('ack', {'id': body['id'], 'message_id': 91})
    await c.callback('p:b')
    message = update('Explicit reply', number=4)
    message['message']['reply_to_message'] = {'message_id': 91}
    await c.handle('update', {'update': message})
    assert c.rpc.calls[-1][0] == 'turn/steer' and c.rpc.calls[-1][1]['threadId'] == 'thread-a'


async def test_async_menu_reaches_gateway_as_new_message_and_accepts_reply(controller, tmp_path, monkeypatch):
    import asyncio
    from codex_bridge.telegram import Gateway

    c = controller
    await ask(c)
    state = tmp_path / 'gateway-state'
    state.mkdir()
    sent = []

    class API:
        async def send(self, uid, body, photo, edit_id):
            assert edit_id is None and photo is None
            assert body['buttons'][0][0][0] == 'Первый'
            sent.append(body)
            return {'message_id': 95}

    gateway = Gateway({'user_id': 123456}, tmp_path / 'socket', c.config.outbox, state, API())
    gateway.setting('status:a', 999)

    async def ipc_call(socket, op, data=None):
        result = await c.handle(op, data or {})
        if op == 'ack': gateway.stop.set()
        return result

    monkeypatch.setattr('codex_bridge.telegram.ipc.call', ipc_call)
    try:
        await asyncio.wait_for(gateway.delivery(), 2)
        assert len(sent) == 1
        await c.callback('p:b')
        message = update('Gateway reply', number=5)
        message['message']['reply_to_message'] = {'message_id': 95}
        await c.handle('update', {'update': message})
        assert c.rpc.calls[-1][0] == 'turn/steer' and c.rpc.calls[-1][1]['threadId'] == 'thread-a'
    finally:
        gateway.state.close()


async def test_async_message_without_questions_remains_a_regular_result(controller):
    from dataclasses import replace
    c = controller
    c.config = replace(c.config, mirror_results=True)
    activate(c)
    event = question_event()
    event['params']['item'].update(questions=None, text='Ordinary async message')
    await c.on_event(event)
    assert c.answers['a'][2] == 'Ordinary async message'
    await c.on_event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {
        'id': 'turn-1', 'status': 'completed', 'items': [event['params']['item']]}}})
    notices = await c.handle('events', {})
    assert len(notices) == 1 and 'Ordinary async message' in notices[0]['text']
    assert not c.store.db.execute('SELECT 1 FROM requests').fetchone()
