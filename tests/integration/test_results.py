import base64
import io
from dataclasses import replace

import pytest
from PIL import Image

from codex_bridge import references
from codex_bridge.telegram import Gateway
from conftest import activate


def event(item, turn='turn-1', thread='thread-a'):
    return {'method': 'item/completed', 'params': {'threadId': thread, 'turnId': turn, 'item': item}}


def generated():
    raw = io.BytesIO()
    Image.new('RGB', (8, 8), 'green').save(raw, format='PNG')
    return {'type': 'imageGeneration', 'id': 'image-1', 'status': 'completed',
            'result': base64.b64encode(raw.getvalue()).decode(), 'savedPath': '/private/.ssh/id_rsa'}


async def complete(c, items=None):
    await c.on_event({'method': 'turn/completed', 'params': {'threadId': 'thread-a',
        'turn': {'id': 'turn-1', 'status': 'completed', 'items': items or []}}})


async def test_final_reply_survives_empty_completion_and_filters_secrets(controller):
    c = controller
    c.config = replace(c.config, mirror_results=True)
    task = activate(c)
    await c.on_event(event({'type': 'agentMessage', 'phase': 'commentary', 'text': 'private progress'}))
    await c.on_event(event({'type': 'agentMessage', 'phase': 'final_answer',
        'text': 'Исправлено R24.\nТесты прошли.\nsecret=do-not-forward'}))
    await c.on_event(event({'type': 'commandExecution', 'aggregatedOutput': 'private tool output'}))
    await c.on_event(event({'type': 'agentMessage', 'phase': 'commentary', 'text': 'later commentary'}))
    await complete(c)
    await complete(c)
    bodies = await c.handle('events', {})
    assert len(bodies) == 1 and bodies[0]['final'] is True
    assert 'Исправлено R24.\nТесты прошли.' in bodies[0]['text']
    assert task[:8] in bodies[0]['text']
    assert all(value not in bodies[0]['text'] for value in ('private progress', 'private tool output', 'later commentary', 'do-not-forward'))
    assert not c.answers and c.store.active('a') is None


async def test_mirroring_can_be_disabled(controller):
    c = controller
    activate(c)
    item = {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'private final reply'}
    await c.on_event(event(item))
    await c.on_event(event(generated()))
    await complete(c, [item, generated()])
    bodies = await c.handle('events', {})
    assert len(bodies) == 1 and 'private final reply' not in bodies[0]['text']
    assert not list(c.config.outbox.rglob('*.png')) and not c.answers


async def test_long_final_reply_is_split_without_losing_tail(controller):
    c = controller
    c.config = replace(c.config, mirror_results=True)
    activate(c)
    answer = 'Ответ\n'*1300 + 'END_OF_REPLY'
    await complete(c, [{'type': 'agentMessage', 'phase': 'final_answer', 'text': answer}])
    bodies = await c.handle('events', {})
    assert len(bodies) > 1
    assert all(body['final'] and len(body['text']) < 4000 for body in bodies)
    assert ''.join(body['text'].split('\n\n', 1)[1] for body in bodies) == answer


async def test_stale_outputs_do_not_enter_current_turn(controller):
    c = controller
    c.config = replace(c.config, mirror_results=True)
    activate(c)
    for thread, turn in [('thread-b', 'old-turn'), ('thread-a', 'old-turn')]:
        await c.on_event(event(generated(), turn, thread))
        await c.on_event(event({'type': 'agentMessage', 'phase': 'final_answer', 'text': 'wrong answer'}, turn, thread))
    await complete(c)
    assert 'wrong answer' not in str(await c.handle('events', {}))
    assert not list(c.config.outbox.rglob('*.png'))


async def test_image_and_reply_reach_gateway_without_editing_old_status(controller, tmp_path, monkeypatch):
    c = controller
    c.config = replace(c.config, mirror_results=True)
    activate(c)
    item = generated()
    await c.on_event(event(item))
    await c.on_event(event(item))
    await complete(c, [item, {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Картинка готова.'}])
    state = tmp_path/'gateway'
    state.mkdir()
    sent = []

    class API:
        async def send(self, uid, body, photo, edit_id):
            assert edit_id is None
            if photo:
                assert Image.open(io.BytesIO(photo)).format == 'PNG'
            sent.append((body, photo))
            return {'message_id': len(sent)}

    gateway = Gateway({'user_id': 123456}, tmp_path/'socket', c.config.outbox, state, API())
    gateway.setting('status:a', 999)

    async def ipc_call(socket, op, data=None):
        result = await c.handle(op, data or {})
        if op == 'ack' and len(sent) == 2:
            gateway.stop.set()
        return result

    monkeypatch.setattr('codex_bridge.telegram.ipc.call', ipc_call)
    try:
        import asyncio
        await asyncio.wait_for(gateway.delivery(), 2)
        assert len(sent) == 2 and sum(bool(photo) for _, photo in sent) == 1
        assert any('Картинка готова.' in body['text'] for body, _ in sent)
        assert gateway.setting('status:a') == '999'
        assert not list(c.config.outbox.rglob('*.png'))
        assert not await c.handle('events', {})
    finally:
        gateway.state.close()


@pytest.mark.parametrize('encoded', ['not base64', base64.b64encode(b'not an image').decode(), None])
def test_invalid_generated_data_rejected(tmp_path, encoded):
    from codex_bridge.security import Rejected
    with pytest.raises(Rejected):
        references.publish_generated(tmp_path, 'a', 'nonce', encoded)
    assert not list(tmp_path.rglob('*.png'))


def test_generated_export_does_not_read_saved_path_and_bounds_size(tmp_path, monkeypatch):
    from codex_bridge.security import Rejected
    monkeypatch.setattr(references, 'open_beneath', lambda *args: pytest.fail('Must not open any source path'))
    name = references.publish_generated(tmp_path, 'a', 'nonce', generated()['result'])
    assert (tmp_path/name).is_file()
    monkeypatch.setattr(references, 'MAX_IMAGE', 10)
    with pytest.raises(Rejected):
        references.publish_generated(tmp_path, 'a', 'large', 'a'*100)
