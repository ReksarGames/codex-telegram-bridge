"""Real handoff in a temporary project; harmless prompts and fixture PNG only."""
import asyncio
import fcntl
import json
import os
import pty
import tempfile
import termios
from pathlib import Path

from PIL import Image

from codex_bridge import ipc
from codex_bridge.config import ControllerConfig, Project
from codex_bridge.controller import Controller
from codex_bridge.security import Rejected


async def main():
    with tempfile.TemporaryDirectory(prefix="ctb-handoff-") as tmp:
        root = Path(tmp)
        os.chmod(root, 0o700)
        for name in ("a", "b", "state", "outbox", "runtime"):
            (root/name).mkdir(mode=0o700)
        config = ControllerConfig(tuple(Project(n, "Test "+n, root/n) for n in ("a", "b")), root/"state", root/"outbox", root/"runtime")
        c = Controller(config, 123456)
        service = asyncio.create_task(c.run())
        client = None
        master = None

        async def wait(check):
            async with asyncio.timeout(120):
                while not check():
                    if service.done(): await service
                    await asyncio.sleep(.1)

        async def submit(text, number):
            message = {"from": {"id": 123456}, "chat": {"id": 123456, "type": "private"}, "text": text}
            await ipc.call(root/"runtime/gateway/gateway.sock", "update", {"update": {"update_id": number, "message": message}})

        try:
            await wait(lambda: (root/"runtime/local.sock").exists())
            await submit("Reply exactly SAVED_TARGET_OK. Do not use tools.", 1)
            await wait(lambda: c.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0] == 1)
            target = c.store.project("a")["thread"]
            saved = (await c.rpc.call("thread/read", {"threadId": target, "includeTurns": True}))["thread"]
            async with c.mutex:
                await c.release_thread(c.store.project("a"))
                await c.start_project("a")
            await submit("Reply exactly OLD_CHAT_OK. Do not use tools.", 2)
            await wait(lambda: c.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0] == 2)
            old = c.store.project("a")["thread"]
            Image.new("RGB", (12, 12), "green").save(root/"a/ref.png")
            await submit("Call bridge_publish_reference with path ref.png and wait for its result. Do not call any other tools.", 3)
            await submit("Reply exactly AFTER_HANDOFF_OK. Do not use tools.", 4)
            await wait(lambda: c.store.project("a")["state"] == "waiting_reference")
            active_task = c.store.active("a")["id"]
            await c.terminal.ensure(c.store.project("a"))
            master, slave = pty.openpty()
            try:
                client = await asyncio.create_subprocess_exec("tmux", "-S", str(c.terminal.socket), "attach-session", "-t", "bridge-a",
                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                    preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
                    env={**os.environ, "TERM": "xterm-256color"})
            finally:
                os.close(slave)
            os.set_blocking(master, False)
            async with asyncio.timeout(15):
                while not (await c.terminal.command("list-clients", "-t", "bridge-a", "-F", "#{client_pid}")).strip():
                    assert client.returncode is None
                    await asyncio.sleep(.1)
            try:
                await ipc.call(root/"runtime/local.sock", "migrate", {"project": "a", "thread": target, "old_process_stopped": True})
                raise AssertionError("Active transfer without --force must fail")
            except Rejected:
                pass
            assert c.store.active("a")["id"] == active_task
            assert c.store.project("a")["state"] == "waiting_reference"
            result = await ipc.call(root/"runtime/local.sock", "migrate", {"project": "a", "thread": target, "old_process_stopped": True, "force": True}, timeout=120)
            print('Handoff state:',result['state'],'paused:',result['paused'],flush=True)
            assert result["thread"] == result["session"] == target
            assert result["state"] == "idle" and result["paused"] == 1
            assert c.store.db.execute("SELECT state FROM tasks WHERE id=?", (active_task,)).fetchone()[0] == "interrupted"
            old_history = (await c.rpc.call("thread/read", {"threadId": old, "includeTurns": True}))["thread"]
            assert old_history["turns"][-1]["status"] == "interrupted"
            new_history = (await c.rpc.call("thread/read", {"threadId": target, "includeTurns": True}))["thread"]
            assert [(t["id"], t["status"]) for t in saved["turns"]] == [(t["id"], t["status"]) for t in new_history["turns"]]
            await asyncio.wait_for(client.wait(), 10)
            assert c.store.project("b")["thread"] is None
            assert c.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='queued'").fetchone()[0] == 1
            await c.terminal.ensure(c.store.project("a"))
            await submit("/resume", 5)
            await wait(lambda: c.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0] == 3)
            assert c.store.project("a")["thread"] == target
            print(json.dumps({"active_turn_interrupted": True, "attached_client_closed": True,
                "target_id_and_history_verified": True, "old_history_retained": True,
                "queue_paused_until_review": True, "next_turn_after_resume": True,
                "other_project_untouched": True}, indent=2))
        finally:
            if client and client.returncode is None:
                client.terminate()
                await client.wait()
            if master is not None: os.close(master)
            c.stop.set()
            await service


if __name__ == "__main__":
    asyncio.run(main())
