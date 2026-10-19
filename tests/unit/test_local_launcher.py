from argparse import Namespace
from types import SimpleNamespace
import pytest

from codex_bridge import main
from codex_bridge.config import Project
from codex_bridge.security import Rejected


def config(tmp_path, monkeypatch):
    path=tmp_path/'controller.toml'
    path.write_text('user_id = 123456\n');path.chmod(0o600)
    monkeypatch.setattr(main,'CONFIG',tmp_path)


async def test_local_task_uses_same_authorized_queue(controller,tmp_path,monkeypatch):
    config(tmp_path,monkeypatch)
    async def call(socket,op,data):
        return await controller.handle(op,data)
    monkeypatch.setattr(main.ipc,'call',call)
    await main.send_local_task('b','Первая задача\n$(touch never-executed)')
    task=controller.store.db.execute('SELECT * FROM tasks').fetchone()
    assert task['project']=='b' and task['update_id']<0
    assert task['text']=='Первая задача\n$(touch never-executed)'
    assert not controller.rpc.calls


async def test_new_project_attaches_after_real_turn_id(tmp_path,monkeypatch):
    config(tmp_path,monkeypatch)
    sent=[]
    initial={'thread':None,'turn':None,'state':'stopped'}
    live={'thread':'real-thread','turn':'real-turn','state':'running','path':str(tmp_path),'endpoint':'/private/codex.sock'}
    async def call(socket,op,data):
        if op=='local_info':return live if sent else initial
        sent.append(data)
        return {}
    # The callback precedes the text; the next read must wait for a real turn.
    monkeypatch.setattr(main.ipc,'call',call)
    async def no_saved(info): return None
    monkeypatch.setattr(main,'select_saved_thread',no_saved)
    monkeypatch.setattr('builtins.input',lambda _: 'Do a harmless task')
    monkeypatch.setattr('shutil.which',lambda _: None)
    class ExecCaptured(Exception):pass
    def execute(program,args):
        assert program=='codex' and '--remote' in args and 'real-thread' in args
        assert args[args.index('-C')+1]==str(tmp_path)
        raise ExecCaptured
    monkeypatch.setattr(main.os,'execvp',execute)
    with pytest.raises(ExecCaptured):await main.run(Namespace(command='attach',project='b'))
    assert sent[-1]['update']['message']['text']=='Do a harmless task'


async def test_initial_slash_command_does_not_create_a_chat(monkeypatch):
    async def call(*args):return {'thread':None}
    monkeypatch.setattr(main.ipc,'call',call)
    async def no_saved(info): return None
    monkeypatch.setattr(main,'select_saved_thread',no_saved)
    monkeypatch.setattr('builtins.input',lambda _: '/clear')
    with pytest.raises(Rejected):await main.run(Namespace(command='attach',project='a'))


@pytest.mark.parametrize('choice', ['1', '0', '', '3', '-1', '$(touch bad)', '١'])
async def test_saved_session_picker_filters_and_never_starts_threads(tmp_path, monkeypatch, choice):
    calls, closed = [], []
    path = str(tmp_path)
    class RPC:
        def __init__(self, *args): pass
        async def connect(self): pass
        async def close(self): closed.append(True)
        async def call(self, method, params):
            calls.append((method, params))
            return {'data': [
                {'id':'old-session','cwd':path,'updatedAt':1700000000,'preview':'Рабочая история'},
                {'id':'other-project','cwd':'/other','updatedAt':1700000000},
                {'id':'subagent','cwd':path,'parentThreadId':'parent','updatedAt':1700000000},
                {'id':'temporary','cwd':path,'ephemeral':True,'updatedAt':1700000000},
            ]}
    monkeypatch.setattr(main,'CodexRPC',RPC)
    monkeypatch.setattr('builtins.input',lambda _:choice)
    info={'path':path,'endpoint':'/private/socket'}
    if choice in {'1','0'}:
        assert await main.select_saved_thread(info) == ('old-session' if choice == '1' else None)
    else:
        with pytest.raises(Rejected): await main.select_saved_thread(info)
    assert calls == [('thread/list', {'cwd':path,'limit':20,'sortKey':'updated_at','sourceKinds':['cli','vscode','appServer']})]
    assert closed == [True]


async def test_saved_picker_refuses_running_standalone_cli(tmp_path,monkeypatch):
    def blocked(*args): raise Rejected('Старый Codex работает')
    monkeypatch.setattr(main.Controller,'check_project',blocked)
    monkeypatch.setattr(main,'CodexRPC',lambda *args:pytest.fail('must not connect while old CLI runs'))
    with pytest.raises(Rejected):
        await main.select_saved_thread({'path':str(tmp_path),'endpoint':'/private/socket'})


async def test_attach_migrates_selected_history_without_new_prompt(tmp_path,monkeypatch):
    calls=[]
    initial={'thread':None,'state':'stopped','path':str(tmp_path),'endpoint':'/private/socket'}
    live={**initial,'thread':'saved-session','session':'saved-session','state':'idle'}
    async def call(socket,op,data):
        calls.append((op,data))
        if op=='local_info': return live if any(x[0]=='migrate' for x in calls) else initial
        return {}
    async def saved(info): return 'saved-session'
    monkeypatch.setattr(main.ipc,'call',call)
    monkeypatch.setattr(main,'select_saved_thread',saved)
    monkeypatch.setattr('builtins.input',lambda _:pytest.fail('no new prompt when restoring history'))
    monkeypatch.setattr('shutil.which',lambda _:None)
    class ExecCaptured(Exception): pass
    def execute(program,args):
        assert program=='codex' and 'saved-session' in args
        raise ExecCaptured
    monkeypatch.setattr(main.os,'execvp',execute)
    with pytest.raises(ExecCaptured):await main.run(Namespace(command='attach',project='a'))
    assert ('migrate',{'project':'a','thread':'saved-session','old_process_stopped':True}) in calls
    assert not any(op=='update' for op,_ in calls)


async def test_paused_new_project_does_not_submit_first_task(monkeypatch):
    async def call(*args):return {'thread':None,'paused':True}
    monkeypatch.setattr(main.ipc,'call',call)
    monkeypatch.setattr('builtins.input',lambda _: pytest.fail('paused queue must not request a task'))
    with pytest.raises(Rejected):await main.run(Namespace(command='attach',project='a'))


async def test_current_attach_checks_env_and_migrates_before_opening_terminal(tmp_path,monkeypatch):
    tid='11111111-2222-4333-8444-555555555555'
    monkeypatch.setenv('CODEX_THREAD_ID',tid)
    calls=[]
    async def call(socket,op,data,**options):
        calls.append((op,data,options))
        return {'thread':tid,'path':str(tmp_path),'endpoint':'/private/socket'}
    monkeypatch.setattr(main.ipc,'call',call)
    monkeypatch.setattr('shutil.which',lambda _:None)
    monkeypatch.setattr(main.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(main.sys.stdout,'isatty',lambda:True)
    class ExecCaptured(Exception):pass
    def execute(program,args):
        assert program=='codex' and tid in args
        raise ExecCaptured
    monkeypatch.setattr(main.os,'execvp',execute)
    args=main.parser().parse_args(['attach','game','--current','--force','--old-process-stopped'])
    with pytest.raises(ExecCaptured):await main.run(args)
    assert calls[0]==('migrate',{'project':'game','thread':tid,'force':True,'old_process_stopped':True},{'timeout':120})
    assert calls[1][0]=='local_info' and calls[1][1]['attach'] is True


@pytest.mark.parametrize('thread_id',['','$(touch bad)','not-a-uuid'])
async def test_current_attach_rejects_invalid_env_before_any_action(monkeypatch,thread_id):
    monkeypatch.setenv('CODEX_THREAD_ID',thread_id)
    async def forbidden(*args,**kwargs):pytest.fail('No IPC before valid current thread ID')
    monkeypatch.setattr(main.ipc,'call',forbidden)
    with pytest.raises(Rejected,match='CODEX_THREAD_ID'):
        await main.run(main.parser().parse_args(['attach','game','--current','--force']))


async def test_current_attach_can_prepare_connection_without_tty(monkeypatch,capsys):
    tid='11111111-2222-4333-8444-555555555555'
    monkeypatch.setenv('CODEX_THREAD_ID',tid)
    async def call(socket,op,data,**options):return {'thread':tid}
    monkeypatch.setattr(main.ipc,'call',call)
    monkeypatch.setattr(main.sys.stdin,'isatty',lambda:False)
    monkeypatch.setattr(main.os,'execvp',lambda *args:pytest.fail('No interactive terminal in a tool subprocess'))
    await main.run(main.parser().parse_args(['attach','game','--current','--force']))
    assert tid in capsys.readouterr().out


@pytest.mark.parametrize('location', ['root', 'subdirectory', 'symlink'])
async def test_auto_attach_reuses_registered_project_and_existing_thread(tmp_path, monkeypatch, location):
    root = tmp_path/'project'; root.mkdir()
    sub = root/'sub'; sub.mkdir()
    link = tmp_path/'alias'; link.symlink_to(root, target_is_directory=True)
    monkeypatch.chdir({'root': root, 'subdirectory': sub, 'symlink': link}[location])
    monkeypatch.setattr(main, 'load_controller', lambda _: SimpleNamespace(projects=(Project('a', 'Test', root),)))
    # A tool environment may carry an unrelated thread; ordinary launch must ignore it.
    monkeypatch.setenv('CODEX_THREAD_ID', '11111111-2222-4333-8444-555555555555')
    calls = []
    async def call(socket, op, data):
        calls.append((op, data))
        return {'thread': 'existing-thread', 'path': str(root), 'endpoint': '/private/socket'}
    monkeypatch.setattr(main.ipc, 'call', call)
    monkeypatch.setattr('shutil.which', lambda _: None)
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('No picker or new prompt for bound project'))
    class Attached(Exception): pass
    def execute(program, args):
        assert program == 'codex' and 'existing-thread' in args and '--remote' in args
        assert args[args.index('-C')+1] == str(root)
        raise Attached
    monkeypatch.setattr(main.os, 'execvp', execute)
    with pytest.raises(Attached): await main.run(main.parser().parse_args(['attach']))
    assert calls == [('local_info', {'project': 'a', 'attach': True})]


@pytest.mark.parametrize('location', ['outside', 'prefix', 'symlink_escape'])
async def test_auto_attach_refuses_unregistered_directory_before_ipc(tmp_path, monkeypatch, location):
    root = tmp_path/'project'; root.mkdir()
    outside = tmp_path/'outside'; outside.mkdir()
    prefix = tmp_path/'project-other'; prefix.mkdir()
    link = root/'escape'; link.symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir({'outside': outside, 'prefix': prefix, 'symlink_escape': link}[location])
    monkeypatch.setattr(main, 'load_controller', lambda _: SimpleNamespace(projects=(Project('a', 'Test', root),)))
    async def forbidden(*args, **kwargs): pytest.fail('No IPC for unregistered cwd')
    monkeypatch.setattr(main.ipc, 'call', forbidden)
    with pytest.raises(Rejected, match='не зарегистрирована'):
        await main.run(main.parser().parse_args(['attach']))


@pytest.mark.parametrize('argv', [[], ['game'], ['--help']])
def test_short_launcher_uses_attach_arguments(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(main.sys, 'argv', ['codex-tg', *argv])
    monkeypatch.setattr(main, 'main', lambda args: calls.append(args))
    main.launch()
    assert calls == [['attach', '--direct', *argv]]


async def test_direct_attach_opens_native_cli_without_managed_tmux(monkeypatch):
    calls = []
    async def call(socket, op, data):
        calls.append((op, data))
        return {'thread': 'same-thread', 'path': '/allowed/project', 'endpoint': '/private/codex.sock'}
    monkeypatch.setattr(main.ipc, 'call', call)
    monkeypatch.setattr('shutil.which', lambda _: '/usr/bin/tmux')
    class Attached(Exception): pass
    def execute(program, argv):
        assert program == 'codex'
        assert argv == ['codex', 'resume', '--remote', 'unix:///private/codex.sock', '-C', '/allowed/project', 'same-thread']
        raise Attached
    monkeypatch.setattr(main.os, 'execvp', execute)
    with pytest.raises(Attached):
        await main.run(main.parser().parse_args(['attach', 'game', '--direct']))
    assert calls == [('local_info', {'project': 'game', 'attach': False})]


@pytest.mark.parametrize('registered', [True, False])
async def test_auto_shell_launch_routes_only_registered_directories(tmp_path, monkeypatch, registered):
    root=tmp_path/'project';root.mkdir()
    monkeypatch.chdir(root if registered else tmp_path)
    monkeypatch.setattr(main,'load_controller',lambda _:SimpleNamespace(projects=(Project('a','Test',root),)))
    calls=[]
    async def call(socket,op,data):
        calls.append((op,data))
        return {'thread':'existing','path':str(root),'endpoint':'/private/socket'}
    monkeypatch.setattr(main.ipc,'call',call)
    class Attached(Exception):pass
    def execute(program,argv):
        assert program=='codex'
        assert argv==(['codex','resume','--remote','unix:///private/socket','-C',str(root),'existing'] if registered else ['codex'])
        raise Attached
    monkeypatch.setattr(main.os,'execvp',execute)
    with pytest.raises(Attached):await main.run(main.parser().parse_args(['auto']))
    assert calls==([('local_info',{'project':'a','attach':False})] if registered else [])
