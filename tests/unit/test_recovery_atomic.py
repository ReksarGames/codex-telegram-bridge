import json
import pytest
from conftest import activate, update
from codex_bridge.security import Rejected


async def test_enqueue_failure_rolls_back_update_id(controller, monkeypatch):
    c = controller
    real = c.store.enqueue
    def fail(*args):
        raise OSError("injected disk failure")
    monkeypatch.setattr(c.store, "enqueue", fail)
    with pytest.raises(OSError):
        await c.handle("update", {"update": update("safe task", 9)})
    assert not c.store.db.execute("SELECT 1 FROM updates WHERE id=9").fetchone()
    monkeypatch.setattr(c.store, "enqueue", real)
    await c.handle("update", {"update": update("safe task", 9)})
    assert c.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


async def test_reconnect_rejoins_before_leaving_unknown(controller):
    c = controller
    activate(c)
    c.store.recover()
    await c.reconcile("a")
    assert any(method == "thread/resume" for method, _ in c.rpc.calls)
    assert c.store.project("a")["state"] == "unknown"


async def test_old_epoch_button_and_sensitive_payload_are_invalidated(controller):
    c = controller
    activate(c)
    token = c.request(c.store.project("a"), "question", {"private_pending_text": "fixture"}, 7)
    c.store.recover()
    with pytest.raises(Rejected):
        c.get_request(token, {"question"})
    assert c.store.db.execute("SELECT payload FROM requests WHERE token=?", (token,)).fetchone()[0] == "{}"


async def test_new_rejects_other_client_and_preserves_old_history(controller):
    c = controller
    async def call(method, params):
        c.rpc.calls.append((method, params))
        if method == "thread/loaded/list":
            return {"data": ["thread-a"]}
        if method == "thread/resume":
            return {"thread": c.rpc.thread}
        if method == "thread/read":
            return {"thread": c.rpc.thread}
        if method == "thread/turns/list":
            return {"data": c.rpc.thread["turns"], "nextCursor": None}
        return {"status": "unsubscribed"}
    c.rpc.call = call
    token = c.request(c.store.project("a"), "new", {})
    with pytest.raises(Rejected):
        await c.callback(f"n:{token}:yes")
    assert not any(method == "thread/start" for method, _ in c.rpc.calls)
    assert c.store.project("a")["thread"] == "thread-a"
    assert c.store.project("a")["state"] == "unknown"


async def test_clear_never_sends_keys_even_with_tmux(controller, monkeypatch):
    c = controller
    monkeypatch.setattr("codex_bridge.controller.shutil.which", lambda _: "/usr/bin/tmux")
    await c.command(c.store.project("a"), "/clear")
    assert c.store.project("a")["thread"] == "thread-a"
    assert not c.rpc.calls
    assert c.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


async def test_secret_question_requires_local_answer_without_storage(controller):
    c = controller
    activate(c)
    await c.on_event({"id": 10, "method": "item/tool/requestUserInput", "params": {
        "threadId": "thread-a", "turnId": "turn-1", "questions": [{"id": "q", "header": "Secret", "question": "password=fixture-value", "options": None, "isSecret": False}]
    }})
    assert c.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    assert "fixture-value" not in json.dumps([dict(r) for r in c.store.db.execute("SELECT * FROM notices")])
    assert c.store.project("a")["state"] == "waiting_user"


async def test_removed_project_cannot_remain_selected(controller):
    from dataclasses import replace
    c=controller
    c.store.setting("selected", "a")
    c.config=replace(c.config, projects=(c.config.projects[1],))
    await c.handle("update", {"update": update("new request", 17)})
    assert c.store.db.execute("SELECT project FROM tasks").fetchone()[0] == "b"
    with pytest.raises(Rejected):
        await c.callback("p:a")


def test_projects_cannot_share_active_session_id(controller):
    import sqlite3
    c=controller
    with pytest.raises(sqlite3.IntegrityError):
        c.store.bind('b',{'id':'different-live-thread','sessionId':'session-a'})
    assert c.store.project('b')['session']=='session-b'


async def test_delivery_failure_diagnostics_do_not_expose_payload(controller,monkeypatch,capsys):
    from codex_bridge.rpc import RPCError
    c=controller
    task=c.store.enqueue('a','private-user-task',None)
    async def call(method, params):
        c.rpc.calls.append((method,params))
        raise RPCError('private-error-text',{'code':-32602,'message':'private-error-text'})
    async def stop_after_iteration(delay):c.stop.set()
    monkeypatch.setattr(c.rpc,'call',call)
    monkeypatch.setattr('codex_bridge.controller.asyncio.sleep',stop_after_iteration)
    await c.dispatch()
    diagnostic=c.store.setting('delivery:'+task)
    assert json.loads(diagnostic)=={'stage':'queue','error':'RPCError','code':-32602}
    assert c.store.project('a')['state']=='unknown'
    assert len(c.rpc.calls)==1
    output=capsys.readouterr().out
    assert 'private-error-text' not in output+diagnostic
    assert 'private-user-task' not in output+diagnostic
