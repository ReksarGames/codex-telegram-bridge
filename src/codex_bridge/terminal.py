"""Managed native TUI attached to the same App Server thread. Approvals use RPC."""
import asyncio
import shutil
from pathlib import Path

from .security import Rejected, identifier


class Terminal:
    def __init__(self, runtime: Path):
        self.socket = runtime / "tmux.sock"
        self.endpoint = runtime / "codex.sock"

    async def command(self, *args: str, required=True) -> str:
        if not shutil.which("tmux"):
            raise Rejected("tmux is required for managed terminal commands")
        proc = await asyncio.create_subprocess_exec("tmux", "-S", str(self.socket), "-f", "/dev/null", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if required and proc.returncode != 0:
            raise Rejected("Managed terminal unavailable: " + args[0])
        return out.decode(errors="replace")

    def name(self, pid: str) -> str:
        return "bridge-" + identifier(pid)

    async def ensure(self, p: dict, restart: bool = False):
        name = self.name(p["id"])
        existing = await self.command("list-sessions", "-F", "#{session_name}", required=False)
        if name in existing.splitlines():
            if not restart:
                return
            clients = await self.command("list-clients", "-t", name, "-F", "#{client_pid}", required=False)
            if clients.strip():
                raise Rejected("Local terminal attached. Run the command there or detach: Ctrl-B D")
            await self.command("kill-session", "-t", name)
        # Multiple argv entries make tmux exec directly, without a shell string.
        await self.command("new-session", "-d", "-s", name, "-x", "120", "-y", "40", "-c", p["path"], "codex", "resume", "--remote", f"unix://{self.endpoint}", "-C", p["path"], p["thread"], "--no-alt-screen")
        await asyncio.sleep(2)
        dead = await self.command("display-message", "-p", "-t", name, "#{pane_dead}")
        if dead.strip() != "0":
            raise Rejected("Codex TUI stopped")

    async def stop(self):
        if self.socket.exists():
            await self.command("kill-server", required=False)

    async def detach_for_replace(self, p: dict, force: bool = False):
        if not self.socket.exists():
            return
        name = self.name(p["id"])
        existing = await self.command("list-sessions", "-F", "#{session_name}", required=False)
        if name not in existing.splitlines():
            return
        clients = await self.command("list-clients", "-t", name, "-F", "#{client_pid}", required=False)
        if clients.strip() and not force:
            raise Rejected("Detach the local TUI before changing sessions: Ctrl-B D")
        await self.command("kill-session", "-t", name)
