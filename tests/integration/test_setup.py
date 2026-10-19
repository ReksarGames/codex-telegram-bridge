import pytest
import tomllib

from codex_bridge import setup
from codex_bridge.security import Rejected
from codex_bridge.telegram import TelegramError


@pytest.mark.parametrize('scenario', ['retry_success', 'missing_chat', 'bad_token', 'forbidden_chat', 'wrong_user', 'group'])
async def test_telegram_setup_diagnostics(monkeypatch, capsys, scenario):
    calls, prompts, closed = [], [], []
    secret = 'private-token-never-print'

    class API:
        def __init__(self, token):
            assert token == secret
            self.session = self

        async def close(self):
            closed.append(True)

        async def call(self, method, params):
            calls.append(method)
            if method == 'getMe':
                if scenario == 'bad_token':
                    raise TelegramError(secret, 400)
                return {'is_bot': True, 'username': 'example_bot'}
            assert params == {'chat_id': 123456}
            if scenario == 'forbidden_chat':
                raise TelegramError(secret, 403)
            if scenario == 'missing_chat' or (scenario == 'retry_success' and calls.count('getChat') == 1):
                raise TelegramError(secret, 400)
            return {'id': 999 if scenario == 'wrong_user' else 123456,
                    'type': 'group' if scenario == 'group' else 'private'}

    monkeypatch.setattr(setup, 'BotAPI', API)
    monkeypatch.setattr('builtins.input', lambda prompt: prompts.append(prompt) or '')
    if scenario == 'retry_success':
        await setup.verify_telegram({'token': secret, 'user_id': 123456})
        assert calls == ['getMe', 'getChat', 'getChat']
        assert len(prompts) == 1
        assert '@example_bot' in capsys.readouterr().out
    else:
        with pytest.raises(Rejected) as exc:
            await setup.verify_telegram({'token': secret, 'user_id': 123456})
        assert secret not in str(exc.value)
        if scenario == 'bad_token':
            assert 'getMe' in str(exc.value)
            assert calls == ['getMe'] and not prompts
        elif scenario == 'missing_chat':
            assert 'User ID' in str(exc.value)
            assert calls.count('getChat') == 2 and len(prompts) == 1
        elif scenario == 'forbidden_chat':
            assert 'getChat' in str(exc.value) and '403' in str(exc.value)
            assert not prompts
    assert closed == [True]
    assert secret not in capsys.readouterr().out


async def test_first_setup_collects_private_values_locally(tmp_path, monkeypatch, capsys):
    config, state, data, runtime = (tmp_path/name for name in ('config', 'state', 'data', 'runtime'))
    project = tmp_path/'workspace'
    project.mkdir()
    for name, path in (('CONFIG', config), ('STATE', state), ('DATA', data)):
        monkeypatch.setattr(setup, name, path)
    monkeypatch.setattr(setup, 'runtime_dir', lambda: runtime)
    monkeypatch.setattr(setup, 'doctor', lambda: True)
    steps, prompts = [], []
    token = '123456789:' + 'x'*35
    monkeypatch.setattr(setup.getpass, 'getpass', lambda prompt: prompts.append(prompt) or token)
    answers = iter(['123456', 'y', 'tools', 'Tools', str(project), 'n', ''])
    monkeypatch.setattr('builtins.input', lambda prompt: prompts.append(prompt) or next(answers))

    async def verify(values):
        assert values == {'token': token, 'user_id': 123456}
        steps.append('verify')

    monkeypatch.setattr(setup, 'verify_telegram', verify)
    monkeypatch.setattr(setup, 'install_units', lambda: steps.append('units'))
    monkeypatch.setattr(setup, 'configure_shell', lambda: steps.append('shell'))
    await setup.wizard()
    gateway = tomllib.loads((config/'gateway.toml').read_text())
    controller = tomllib.loads((config/'controller.toml').read_text())
    assert gateway == {'token': token, 'user_id': 123456}
    assert controller['projects'] == [{'id': 'tools', 'name': 'Tools', 'path': str(project)}]
    assert controller['mirror_results'] is True
    assert steps == ['verify', 'units', 'shell']
    assert any('Bot Token' in prompt for prompt in prompts)
    assert any('numeric Telegram User ID' in prompt for prompt in prompts)
    assert all(prompt.isascii() for prompt in prompts)
    for path in config.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o700
    assert token not in capsys.readouterr().out
