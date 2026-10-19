"""A gateway mount namespace with no repositories, Codex auth, or host processes."""
import os
import sys
from pathlib import Path

from .config import CONFIG, DATA, STATE, runtime_dir
from .security import Rejected


def gateway_command(config: dict) -> list[str]:
    if not Path("/usr/bin/bwrap").exists():
        raise Rejected("Для запуска Gateway требуется bubblewrap; запуск без изоляции запрещён")
    package = Path(__file__).resolve().parent
    environment = Path(sys.prefix).resolve()
    python = Path(sys.executable).resolve()
    # uv's interpreter can live outside /usr, so mount just that interpreter installation.
    interpreter = python.parent.parent
    command = ["/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup", "--cap-drop", "ALL", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/home", "--tmpfs", "/run", "--tmpfs", "/etc"]
    for path in (Path("/etc/ssl"), Path("/etc/ca-certificates"), Path("/etc/resolv.conf"), Path("/etc/hosts"), Path("/etc/nsswitch.conf"), package, environment):
        if path.exists():
            command += ["--ro-bind", str(path.resolve()), str(path)]
    if not interpreter.is_relative_to(Path("/usr")):
        command += ["--ro-bind", str(interpreter), str(interpreter)]
        alias = Path(sys.base_prefix)
        if alias != interpreter:
            command += ["--ro-bind", str(interpreter), str(alias)]
        executable = Path(sys.executable)
        if executable.is_symlink():
            link = Path(os.readlink(executable))
            alias_root = (link if link.is_absolute() else executable.parent / link).parent.parent
            if alias_root != interpreter and alias_root.resolve() == interpreter:
                command += ["--ro-bind", str(interpreter), str(alias_root)]
    for source, writable in ((CONFIG / "gateway.toml", False), (DATA / "outbox", False), (STATE / "gateway", True), (runtime_dir() / "gateway", False)):
        command += ["--bind" if writable else "--ro-bind", str(source), str(source)]
    for key in ("whisper", "whisper_model"):
        value = config.get(key)
        if value:
            path = Path(value).resolve(strict=True)
            if not path.is_relative_to(Path("/usr")):
                command += ["--ro-bind", str(path), str(path)]
    command += ["--clearenv", "--setenv", "HOME", "/tmp", "--setenv", "PATH", "/usr/bin", "--setenv", "PYTHONPATH", str(package.parent), "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "LANG", "C.UTF-8", "--", str(Path(sys.executable).absolute()), "-m", "codex_bridge", "gateway-worker", "--config", str(CONFIG / "gateway.toml"), "--socket", str(runtime_dir() / "gateway/gateway.sock"), "--outbox", str(DATA / "outbox"), "--state", str(STATE / "gateway")]
    return command
