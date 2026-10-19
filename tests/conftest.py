import copy
import pytest
from codex_bridge.config import ControllerConfig, Project
from codex_bridge.controller import Controller
from codex_bridge.storage import Store

class FakeRPC:
    def __init__(self):
        self.connected = True
        self.calls, self.responses, self.queue = [], [], []
        self.thread = {'id':'thread-a','sessionId':'session-a','status':{'type':'idle'},'turns':[], 'cwd':''}
        self.fail = None
    async def call(self, method, params, **kwargs):
        self.calls.append((method, params))
        if self.fail == method: raise OSError('network lost')
        if method in {'thread/read','thread/resume'}:
            thread = copy.deepcopy(self.thread)
            if (method == 'thread/read' and not params.get('includeTurns',False)) or params.get('excludeTurns'): thread['turns'] = []
            return {'thread':thread}
        if method == 'thread/turns/list':
            start = int(params.get('cursor',0))
            limit = params.get('limit',50)
            ordered = self.thread['turns'][::-1] if params.get('sortDirection') == 'desc' else self.thread['turns']
            turns = copy.deepcopy(ordered[start:start+limit])
            if params.get('itemsView') == 'notLoaded':
                for turn in turns: turn['items'] = []
            return {'data':turns, 'nextCursor':str(start+limit) if start+limit<len(self.thread['turns']) else None}
        if method == 'thread/items/list':
            turn = next(t for t in self.thread['turns'] if t['id'] == params['turnId'])
            start = int(params.get('cursor',0))
            limit = params.get('limit',1)
            return {'data':[{'turnId':turn['id'],'item':copy.deepcopy(item)} for item in turn['items'][start:start+limit]], 'nextCursor':str(start+limit) if start+limit<len(turn['items']) else None}
        if method == 'thread/queue/list': return {'data':self.queue}
        if method == 'thread/queue/delete': self.queue=[]; return {'deleted':True}
        if method == 'thread/loaded/list': return {'data':[]}
        if method == 'thread/unsubscribe': return {'status':'unsubscribed'}
        return {}
    async def respond(self, rid, result):
        if self.fail == 'respond': raise OSError('network lost')
        self.responses.append((rid,result))

@pytest.fixture
def controller(tmp_path):
    for name in ('a','b','state','outbox','runtime'): (tmp_path/name).mkdir(mode=0o700)
    config=ControllerConfig(tuple(Project(n,'Проект '+n,tmp_path/n) for n in ('a','b')),tmp_path/'state',tmp_path/'outbox',tmp_path/'runtime')
    store=Store(config.state/'metadata.db')
    for p in config.projects: store.add_project(p);store.bind(p.id,{'id':'thread-'+p.id,'sessionId':'session-'+p.id})
    c=Controller(config,123456,store);c.rpc=FakeRPC();c.rpc.thread['cwd']=str(tmp_path/'a')
    yield c
    store.db.close()

def update(text=None, number=1, uid=123456, chat_type='private', callback=None):
    m={'from':{'id':uid},'chat':{'id':uid,'type':chat_type}}
    if callback: return {'update_id':number,'callback_query':{'from':m['from'],'message':m,'data':callback,'id':'cb'}}
    m['text']=text
    return {'update_id':number,'message':m}

def activate(c):
    task=c.store.enqueue('a','task',None);c.store.task_state(task,'running','turn-1');c.store.state('a','running','turn-1')
    c.rpc.thread['status']={'type':'active','activeFlags':[]};c.rpc.thread['turns']=[{'id':'turn-1','status':'inProgress','items':[]}]
    return task
