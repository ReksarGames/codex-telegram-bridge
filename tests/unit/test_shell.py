import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codex_bridge import shell
from codex_bridge.security import Rejected


@pytest.mark.parametrize('name', ['bash', 'zsh', 'fish'])
def test_shell_enable_disable_preserves_existing_settings(tmp_path, monkeypatch, name):
    monkeypatch.setattr(shell.Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('ZDOTDIR', raising=False)
    path = tmp_path/'.config/fish/functions/codex.fish' if name == 'fish' else tmp_path/('.'+name+'rc')
    original = '' if name == 'fish' else '# My settings\nexport EDITOR=code\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(original)
    shell.configure_shell(True, name)
    first = path.read_text()
    assert first.startswith(original) and first.count(shell.START) == 1
    assert 'command codex' in first and ' auto' in first
    shell.configure_shell(True, name)
    assert path.read_text() == first
    shell.configure_shell(False, name)
    assert path.read_text() == original
    assert path.with_name(path.name+'.codex-bridge.bak').read_text() == original
    assert path.with_name(path.name+'.codex-bridge.bak').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('existing', ['alias codex="mine"\n', 'codex() { echo mine; }\n', 'function codex { echo mine; }\n'])
def test_existing_custom_codex_is_not_replaced(tmp_path, monkeypatch, existing):
    monkeypatch.setattr(shell.Path, 'home', lambda: tmp_path)
    path = tmp_path/'.bashrc';path.write_text(existing)
    with pytest.raises(Rejected, match='custom'):
        shell.configure_shell(True, 'bash')
    assert path.read_text() == existing


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'bad_marker'])
def test_unsafe_shell_file_unchanged(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(shell.Path, 'home', lambda: tmp_path)
    path=tmp_path/'.bashrc';other=tmp_path/'other';other.write_text('private original')
    if kind == 'symlink':path.symlink_to(other)
    elif kind == 'hardlink':os.link(other,path)
    else:path.write_text(shell.START)
    with pytest.raises((Rejected, OSError)):
        shell.configure_shell(True, 'bash')
    assert other.read_text() == 'private original'


def test_wizard_shell_choice_can_decline_without_creating_config(tmp_path, monkeypatch):
    monkeypatch.setattr(shell.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr('builtins.input', lambda _: 'n')
    shell.configure_shell(shell='bash')
    assert not (tmp_path/'.bashrc').exists()


@pytest.mark.parametrize('name', ['bash', 'fish', 'zsh'])
def test_real_shell_routes_bare_command_and_preserves_literal_arguments(tmp_path, monkeypatch, name):
    if not shutil.which(name):pytest.skip('Shell not installed')
    monkeypatch.setattr(shell.Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('ZDOTDIR', raising=False)
    bindir=tmp_path/'bin space';bindir.mkdir()
    python=bindir/'python';monkeypatch.setattr(shell.sys, 'executable',str(python))
    (bindir/'codex-bridge').write_text('#!/bin/sh\nprintf "bridge:%s\\n" "$@"\n')
    (bindir/'codex').write_text('#!/bin/sh\nprintf "native:%s\\n" "$@"\n')
    for p in (bindir/'codex',bindir/'codex-bridge'):p.chmod(0o700)
    shell.configure_shell(True,name)
    path=tmp_path/'.config/fish/functions/codex.fish' if name=='fish' else tmp_path/('.'+name+'rc')
    import shlex
    script=f"source {shlex.quote(str(path))}; codex; codex login '$HOME;$(touch NEVER)'"
    result=subprocess.run([name,'--no-config' if name=='fish' else '--norc' if name=='bash' else '-f','-c',script],env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH']},text=True,capture_output=True,check=True)
    assert result.stdout.splitlines()==['bridge:auto','native:login','native:$HOME;$(touch NEVER)']
    assert not (Path.cwd()/'NEVER').exists()
