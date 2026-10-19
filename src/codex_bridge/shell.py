"""Opt-in routing of a bare codex command; never replace the installed binary."""
import os
import pwd
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path

from .security import Rejected

START = "# >>> codex-telegram-bridge >>>\n"
END = "# <<< codex-telegram-bridge <<<\n"


def configure_shell(enabled: bool | None = None, shell: str | None = None):
    shell = shell or Path(pwd.getpwuid(os.getuid()).pw_shell).name
    home = Path.home()
    if shell == "fish":
        path = home / ".config/fish/functions/codex.fish"
    elif shell in {"bash", "zsh"}:
        path = (Path(os.environ.get("ZDOTDIR", str(home))) if shell == "zsh" else home) / (".zshrc" if shell == "zsh" else ".bashrc")
    else:
        print("Automatic launch supports Fish, Bash and Zsh; use codex-tg")
        return
    original, info = "", None
    if path.exists() or path.is_symlink():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 1024**2:
                raise Rejected("Shell configuration must be a regular file owned by your user")
            original = stream.read()
    managed = START in original
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise Rejected("The managed shell block is damaged; automatic changes cancelled")
    if managed:
        start, end = original.index(START), original.index(END) + len(END)
        if original.index(END) < start:
            raise Rejected("Invalid managed shell block")
        rest = original[:start] + original[end:]
    else:
        rest = original
    if enabled is None:
        print(f"Shell: {shell}. A bare codex command will open the bridge in registered projects; commands with arguments remain unchanged.")
        answer = input(f"Connect the bridge when launching codex? Currently {'enabled' if managed else 'disabled'} [y/n, Enter to keep]: ").strip().lower()
        if not answer:
            return
        if answer not in {"y", "yes", "n", "no"}:
            raise Rejected("Answer y or n; shell configuration was not changed")
        enabled = answer in {"y", "yes"}
    if enabled and not managed and ((shell == "fish" and original) or re.search(r"(?m)^\s*(?:function\s+codex\b|codex\s*\(\s*\)|alias\s+codex[=\s])", rest)):
        raise Rejected("A custom codex function or alias already exists; keeping it")
    executable = shlex.quote(str(Path(sys.executable).parent / "codex-bridge"))
    if shell == "fish":
        body = f"function codex\n    if test (count $argv) -eq 0\n        {executable} auto\n    else\n        command codex $argv\n    end\nend\n"
    else:
        body = f'codex() {{\n    if [ "$#" -eq 0 ]; then\n        {executable} auto\n    else\n        command codex "$@"\n    fi\n}}\n'
    updated = rest + (("\n" if rest and not rest.endswith("\n") else "") + START + body + END if enabled else "")
    if updated != original:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if info:
            backup = path.with_name(path.name + ".codex-bridge.bak")
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w") as stream:
                    stream.write(original)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(updated)
        try:
            os.chmod(temporary, stat.S_IMODE(info.st_mode) if info else 0o600)
            if info:
                current = path.lstat()
                if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size):
                    raise Rejected("Shell configuration changed during installation; retry locally")
            if not info and (path.exists() or path.is_symlink()):
                raise Rejected("Shell configuration appeared during installation; keeping it")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Automatic launch through codex {'enabled' if enabled else 'disabled'}. Open a new terminal.")
