"""Explicit, harmless live experiment. Uses existing ChatGPT login; no API key."""
import asyncio
import json
import os
import pty
import signal
import tempfile
from pathlib import Path

from codex_bridge.rpc import CodexRPC, RPCError


async def main():
    with tempfile.TemporaryDirectory(prefix="ctb-feasibility-") as tmp:
        root = Path(tmp)
        os.chmod(root, 0o700)
        endpoint = root / "codex.sock"
        env = {k: v for k, v in os.environ.items() if k not in {"OPENAI_API_KEY", "CODEX_API_KEY"}}
        server = await asyncio.create_subprocess_exec("codex", "app-server", "--listen", f"unix://{endpoint}", "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="user"', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
        clients, tui = [], None
        try:
            for _ in range(100):
                if endpoint.exists():
                    break
                await asyncio.sleep(.1)
            events = [[], []]
            async def record(i, e):
                events[i].append(e)
            for i in range(2):
                client = CodexRPC(endpoint, lambda e, i=i: record(i, e))
                clients.append(client)
                await client.connect()
            first, second = clients
            result = await first.call("thread/start", {"cwd": str(root), "sandbox": "read-only", "approvalPolicy": "on-request", "approvalsReviewer": "user"})
            thread = result["thread"]
            tid = thread["id"]
            turn = await first.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": "Reply with exactly BRIDGE_SHARED_SESSION_OK. Do not use tools.", "text_elements": []}]})
            turn_id = turn["turn"]["id"]
            await asyncio.sleep(.1)
            result2 = await second.call("thread/resume", {"threadId": tid})
            assert result2["thread"]["id"] == tid
            master, slave = pty.openpty()
            tui = await asyncio.create_subprocess_exec("codex", "resume", "--remote", f"unix://{endpoint}", tid, "--no-alt-screen", stdin=slave, stdout=slave, stderr=slave, env={**env, "TERM": "xterm-256color"}, start_new_session=True)
            os.close(slave)
            async def drain():
                os.set_blocking(master, False)
                while True:
                    try:
                        os.read(master, 65536)
                    except BlockingIOError:
                        pass
                    except OSError:
                        return
                    await asyncio.sleep(.05)
            draining = asyncio.create_task(drain())
            await asyncio.sleep(3)
            assert tui.returncode is None, "remote CLI exited"
            for _ in range(1200):
                if all(any(e["method"] == "turn/completed" and e["params"]["turn"]["id"] == turn_id for e in stream) for stream in events):
                    break
                await asyncio.sleep(.1)
            else:
                raise AssertionError("missing completion on both clients")
            read = await second.call("thread/read", {"threadId": tid, "includeTurns": True})
            assert "BRIDGE_SHARED_SESSION_OK" in json.dumps(read)
            status = next(e["params"]["turn"]["status"] for e in events[0] if e["method"] == "turn/completed")
            assert status == "completed", status
            os.write(master, b"Reply exactly PC_MESSAGE_OK. Do not use tools.")
            await asyncio.sleep(.8)
            os.write(master, b"\r")
            async with asyncio.timeout(120):
                while not all(sum(e["method"] == "turn/completed" for e in stream) == 2 for stream in events):
                    await asyncio.sleep(.1)
            read = await second.call("thread/read", {"threadId": tid, "includeTurns": True})
            assert "PC_MESSAGE_OK" in json.dumps(read) and len(read["thread"]["turns"]) == 2
            print(json.dumps({"version": "0.160.1", "threadId": tid, "sessionId": thread.get("sessionId"), "two_clients_same_thread": True, "remote_tui_alive": tui.returncode is None, "native_tui_message_same_history": True, "completion_both_clients": True, "persisted_history": True}, indent=2))
        finally:
            if tui and tui.returncode is None:
                os.killpg(tui.pid, signal.SIGTERM)
                await tui.wait()
            if 'draining' in locals():
                draining.cancel()
                await asyncio.gather(draining, return_exceptions=True)
                os.close(master)
            for client in clients:
                await client.close()
            if server.returncode is None:
                os.killpg(server.pid, signal.SIGTERM)
                await server.wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RPCError as error:
        print(str(error))
        raise SystemExit(1)
