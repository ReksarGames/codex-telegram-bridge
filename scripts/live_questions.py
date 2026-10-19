"""Harmless real-Codex async-question routing test; no Telegram credentials."""
import asyncio
import json
import os
import tempfile
from pathlib import Path

from codex_bridge.config import ControllerConfig, Project
from codex_bridge.controller import Controller, question_replies


async def main():
    with tempfile.TemporaryDirectory(prefix='ctb-questions-') as tmp:
        root = Path(tmp)
        os.chmod(root, 0o700)
        for name in ('project', 'state', 'outbox', 'runtime'):
            (root / name).mkdir(mode=0o700)
        config = ControllerConfig((Project('test', 'Question test', root / 'project'),),
                                  root / 'state', root / 'outbox', root / 'runtime')
        controller = Controller(config, 123456)
        running = asyncio.create_task(controller.run())

        async def wait_for(predicate):
            async with asyncio.timeout(120):
                while not predicate():
                    if running.done():
                        await running
                    await asyncio.sleep(.1)

        def update(number, text=None, callback=None):
            message = {'from': {'id': 123456}, 'chat': {'id': 123456, 'type': 'private'}}
            if callback:
                return {'update_id': number, 'callback_query': {'id': 'fixture', 'from': message['from'],
                        'message': message, 'data': callback}}
            return {'update_id': number, 'message': {**message, 'text': text}}

        try:
            await wait_for(lambda: (root / 'runtime/gateway/gateway.sock').exists())
            prompt = ('Use request_user_input_async to ask exactly "Bridge menu test: select a fixture" '
                      'with options "Alpha" and "Beta". Then run only sleep 15 with exec_command to give '
                      'the test client time to answer. Do not read or modify files, access the network, '
                      'or request approvals. After the answer, reply exactly BRIDGE_ASYNC_REPLY_OK '
                      'if the answer contains Beta.')
            await controller.handle('update', {'update': update(1, text=prompt)})
            await wait_for(lambda: controller.store.db.execute(
                "SELECT 1 FROM requests WHERE kind='async_question' AND state='pending'").fetchone())
            notices = await controller.handle('events', {})
            question = next(n for n in notices if n.get('buttons'))
            assert question['buttons'][0][0][0] == 'Alpha'
            assert question['buttons'][1][0][0] == 'Beta'
            await controller.handle('update', {'update': update(2, callback=question['buttons'][1][0][1])})
            await wait_for(lambda: controller.store.project('test')['state'] == 'idle')
            project = controller.store.project('test')
            thread = (await controller.rpc.call('thread/read', {
                'threadId': project['thread'], 'includeTurns': True}))['thread']
            assert len(thread['turns']) == 1
            items = thread['turns'][0]['items']
            replies = [i for i in items if i['type'] == 'userMessage' and question_replies(i)]
            assert len(replies) == 1 and 'Beta' in replies[0]['content'][0]['text']
            assert any(i.get('text', '').strip() == 'BRIDGE_ASYNC_REPLY_OK' for i in items)
            # Codex sandbox can create empty protected-directory mountpoints.
            assert not any(p.is_file() or p.is_symlink() for p in (root / 'project').rglob('*'))
            print(json.dumps({'async_menu': True, 'structured_reply_in_same_turn': True,
                              'one_task_one_turn': True, 'no_project_files_changed': True}))
        finally:
            controller.stop.set()
            await running


if __name__ == '__main__':
    asyncio.run(main())
