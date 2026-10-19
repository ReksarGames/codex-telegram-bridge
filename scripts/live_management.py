"""Harmless two-project and /new test against the installed native server/TUI."""
import asyncio
import json
import os
import tempfile
from pathlib import Path

from codex_bridge import ipc
from codex_bridge.config import ControllerConfig, Project
from codex_bridge.controller import Controller


async def main():
    with tempfile.TemporaryDirectory(prefix="ctb-projects-") as tmp:
        root = Path(tmp)
        os.chmod(root, 0o700)
        for name in ("a", "b", "state", "outbox", "runtime"):
            (root/name).mkdir(mode=0o700)
        config = ControllerConfig(tuple(Project(n, "Test "+n, root/n) for n in ("a", "b")), root/"state", root/"outbox", root/"runtime")
        c = Controller(config, 123456)
        service = asyncio.create_task(c.run())
        async def wait(check):
            async with asyncio.timeout(120):
                while not check():
                    if service.done():
                        await service
                    await asyncio.sleep(.1)
        update_id = 0
        async def send(text=None, callback=None):
            nonlocal update_id
            update_id += 1
            m = {"from": {"id": 123456}, "chat": {"id": 123456, "type": "private"}}
            u = {"update_id": update_id}
            if callback:
                u["callback_query"] = {"from": m["from"], "message": m, "data": callback}
            else:
                m["text"] = text
                u["message"] = m
            return await ipc.call(root/"runtime/gateway/gateway.sock", "update", {"update": u})
        try:
            await wait(lambda: (root/"runtime/gateway/gateway.sock").exists())
            await send("Reply PROJECT_A_OK. Do not use tools.")
            await send(callback="p:b")
            await send("Reply PROJECT_B_OK. Do not use tools.")
            await wait(lambda: c.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0] == 2)
            a, b = c.store.project("a"), c.store.project("b")
            assert a["thread"] != b["thread"]
            for p in (a, b):
                t = (await c.rpc.call("thread/read", {"threadId": p["thread"], "includeTurns": True}))["thread"]
                assert t["cwd"] == p["path"] and len(t["turns"]) == 1
            await c.terminal.ensure(a)
            await send(callback="p:a")
            await send("/new")
            token = c.store.db.execute("SELECT token FROM requests WHERE project='a' AND kind='new'").fetchone()[0]
            await send(callback=f"n:{token}:yes")
            new = c.store.project("a")
            assert new["thread"] != a["thread"]
            assert c.store.project("b")["thread"] == b["thread"]
            assert c.store.db.execute("SELECT COUNT(*) FROM sessions WHERE project='a'").fetchone()[0] == 2
            await send("Reply NEW_CHAT_OK. Do not use tools.")
            await wait(lambda: c.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0] == 3)
            old = (await c.rpc.call("thread/read", {"threadId": a["thread"], "includeTurns": True}))["thread"]
            assert "PROJECT_A_OK" in json.dumps(old)
            print(json.dumps({"two_projects_distinct_threads": True, "switch_preserves_other_thread": True, "native_tui_detached_before_new": True, "new_chat_real_id": True, "old_history_preserved": True}, indent=2))
        finally:
            c.stop.set()
            await service


if __name__ == "__main__":
    asyncio.run(main())
