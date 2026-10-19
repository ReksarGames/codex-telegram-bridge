import re

from conftest import activate
from codex_bridge.controller import HELP, LABELS


async def test_telegram_commands_and_buttons_are_english(controller):
    c = controller
    c.store.db.execute("UPDATE projects SET name='Example Project'")
    for command in ('/start', '/help', '/projects', '/project', '/sessions', '/status',
                    '/queue', '/pause', '/resume', '/new', '/clear', '/codex_status', '/unsupported'):
        await c.command(c.current(), command)
    bodies = await c.handle('events', {})
    assert bodies
    for body in bodies:
        assert not re.search('[А-Яа-яЁё]', body['text'])
        for row in body.get('buttons', []):
            assert all(not re.search('[А-Яа-яЁё]', label) for label, _ in row)
    assert 'Text messages become tasks' in HELP
    assert all(not re.search('[А-Яа-яЁё]', label) for label in LABELS.values())


async def test_codex_question_and_options_keep_their_original_language(controller):
    c = controller
    activate(c)
    await c.on_event({'id': 100, 'method': 'item/tool/requestUserInput', 'params': {
        'threadId': 'thread-a', 'turnId': 'turn-1', 'questions': [
            {'id': 'choice', 'question': 'Какой вариант?', 'options': [{'label': 'Первый вариант'}]}
        ]}})
    body = (await c.handle('events', {}))[0]
    assert 'Какой вариант?' in body['text']
    assert 'You can reply to this message with text.' in body['text']
    assert body['buttons'][0][0][0] == 'Первый вариант'
