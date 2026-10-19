"""Harmless real-Codex queue, image gate and restart smoke test. No Telegram/network credentials."""
import asyncio,json,os,tempfile
from pathlib import Path
from PIL import Image
from codex_bridge.config import ControllerConfig,Project
from codex_bridge.controller import Controller
from codex_bridge import ipc

async def main():
 with tempfile.TemporaryDirectory(prefix='ctb-core-') as tmp:
  root=Path(tmp);os.chmod(root,0o700)
  for name in ['a','b','state','outbox','runtime']: (root/name).mkdir(mode=0o700)
  config=ControllerConfig(tuple(Project(n,'Test '+n,root/n) for n in ['a','b']),root/'state',root/'outbox',root/'runtime')
  controller=Controller(config,123456)
  running=asyncio.create_task(controller.run())
  async def wait_for(predicate,timeout=120):
   async with asyncio.timeout(timeout):
    while not predicate():
     if running.done(): await running
     await asyncio.sleep(.1)
  async def submit(text,n):
   u={'update_id':n,'message':{'from':{'id':123456},'chat':{'id':123456,'type':'private'},'text':text}}
   return await ipc.call(root/'runtime/gateway/gateway.sock','update',{'update':u})
  try:
   await wait_for(lambda:(root/'runtime/gateway/gateway.sock').exists())
   await submit('Reply exactly FIRST_OK. Do not use tools.',1)
   await submit('Reply exactly SECOND_OK. Do not use tools.',2)
   await wait_for(lambda:controller.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0]==2)
   p=controller.store.project('a');first_thread=p['thread']
   Image.new('RGB',(12,12),'green').save(root/'a/ref.png')
   await submit('Call bridge_publish_reference with path ref.png, wait for its result, then reply REFERENCE_ACCEPTED. Do not call any other tools.',3)
   await submit('Reply exactly AFTER_GATE. Do not use tools.',4)
   await wait_for(lambda:controller.store.project('a')['state']=='waiting_reference')
   await asyncio.sleep(1)
   assert controller.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='queued'").fetchone()[0]==1
   token=controller.store.db.execute("SELECT token FROM requests WHERE kind='reference' AND state='pending'").fetchone()[0]
   await ipc.call(root/'runtime/local.sock','reference_decide',{'project':'a','token':token,'decision':'accept'})
   await wait_for(lambda:controller.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0]==4)
   history=await controller.rpc.call('thread/read',{'threadId':first_thread,'includeTurns':True})
   assert len(history['thread']['turns'])==4
   controller.stop.set();await running
   controller=Controller(config,123456);running=asyncio.create_task(controller.run())
   await wait_for(lambda:(root/'runtime/gateway/gateway.sock').exists())
   assert controller.store.project('a')['thread']==first_thread
   assert controller.store.project('a')['state']=='idle'
   await submit('Reply exactly RESTART_OK. Do not use tools.',5)
   await wait_for(lambda:controller.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0]==5)
   await submit('Use apply_patch to create harmless.txt containing exactly BRIDGE_SAFE_FILE_OK. Do not run shell commands or modify any other files.',6)
   await wait_for(lambda:controller.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0]==6)
   assert (root/'a/harmless.txt').read_text().strip()=='BRIDGE_SAFE_FILE_OK'
   print(json.dumps({'native_fifo':True,'same_thread':first_thread,'reference_gate':True,'queue_blocked_until_accept':True,'restart_same_history':True,'workspace_file_edit':True,'completed_real_turns':6},indent=2))
  finally:
   controller.stop.set();await running

asyncio.run(main())
