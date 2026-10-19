import json
import pytest
from conftest import activate,update
from codex_bridge.security import Rejected
from codex_bridge.storage import Store

async def test_fifo_dedup_and_projects(controller):
    c=controller
    for n,t in [(1,'первая\nUnicode 😀 $HOME; $(touch /tmp/pwn)'),(2,'вторая')]: await c.handle('update',{'update':update(t,n)})
    assert (await c.handle('update',{'update':update('duplicate',1)}))['duplicate']
    assert not c.rpc.calls
    first=c.store.claim('a');assert first['text'].startswith('первая\n');assert c.store.claim('a') is None
    await c.handle('update',{'update':update(number=3,callback='p:b')});await c.handle('update',{'update':update('проект b',4)})
    assert c.store.claim('b')['text']=='проект b'
    c.store.task_state(first['id'],'completed');assert c.store.claim('a')['text']=='вторая'
    assert c.store.db.execute('SELECT text FROM tasks WHERE id=?',(first['id'],)).fetchone()[0] is None

async def test_pause_wait_unknown(controller):
    c=controller;c.store.enqueue('a','pending',None)
    await c.handle('update',{'update':update('/pause')});assert c.store.claim('a') is None
    await c.handle('update',{'update':update('/resume',2)})
    for state in ['running','waiting_approval','waiting_user','waiting_reference','unknown']:
        c.store.state('a',state);assert c.store.claim('a') is None
    c.store.state('a','idle');assert c.store.claim('a')

async def test_recovery_not_repeat(controller):
    c=controller;activate(c);c.store.recover();await c.reconcile('a')
    assert c.store.active('a')['state']=='unknown';assert c.store.claim('a') is None
    c.rpc.thread['turns'][0]['status']='completed';c.rpc.thread['status']={'type':'idle'};await c.reconcile('a')
    assert c.store.active('a') is None;assert c.store.project('a')['state']=='idle'

async def test_reconcile_client_id(controller):
    c=controller;tid=c.store.enqueue('a','task',None);c.store.task_state(tid,'unknown')
    c.rpc.thread['turns']=[{'id':'after-loss','status':'completed','items':[{'type':'userMessage','clientId':tid}]}]
    await c.reconcile('a');assert not c.store.active('a');assert c.store.project('a')['state']=='idle'

async def test_reconcile_unstarted_queue(controller):
    c=controller;tid=c.store.enqueue('a','task',None);c.store.task_state(tid,'unknown');c.rpc.queue=[{'id':'q1','clientUserMessageId':tid}]
    await c.reconcile('a');assert c.store.claim('a')['id']==tid
    assert any(m=='thread/queue/delete' for m,_ in c.rpc.calls)

async def test_only_structured_completion(controller):
    c=controller;activate(c)
    await c.on_event({'method':'item/commandExecution/outputDelta','params':{'threadId':'thread-a','delta':'private output'}})
    assert c.store.project('a')['state']=='running';assert c.store.db.execute('SELECT COUNT(*) FROM notices').fetchone()[0]==0
    await c.on_event({'method':'turn/completed','params':{'threadId':'thread-a','turn':{'id':'turn-1','status':'completed','items':[]}}})
    assert c.store.active('a') is None;assert c.store.project('a')['state']=='idle'

async def test_clear_is_not_faked(controller):
    c=controller;await c.handle('update',{'update':update('/clear')})
    assert c.store.project('a')['thread']=='thread-a';assert not c.rpc.calls
    assert 'does not substitute' in c.store.db.execute('SELECT body FROM notices').fetchone()[0]
    activate(c)
    with pytest.raises(Rejected): await c.handle('update',{'update':update('/clear',2)})

def test_backup_and_schema(controller,tmp_path):
    c=controller;tid=c.store.enqueue('a','pending',99);p=tmp_path/'backup.db';c.store.backup(p);s=Store(p)
    assert s.db.execute('SELECT id FROM tasks').fetchone()[0]==tid;s.db.close()
