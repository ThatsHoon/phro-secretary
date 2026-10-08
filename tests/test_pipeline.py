import json
import threading
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import pytest
import server as api
from memory.store import Store
from memory.service import MemoryService
from test_memory_service import FakeGraph

@pytest.fixture
def application(tmp_path,monkeypatch):
    service=MemoryService(Store(tmp_path/'http.db'),FakeGraph())
    monkeypatch.setattr(api,'MEMORY',service)
    monkeypatch.setattr(api,'claude',lambda *a,**k: ('Noted. [e:happy]',1))
    api.CANCELLED.clear()
    return service

def test_http_conversation_and_privacy_contract(application):
    server=api.SERVER_CLASS(('127.0.0.1',0),api.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_address[1])
    def post(path,body,**headers):
        req=Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**headers})
        with urlopen(req) as r: return json.load(r)
    try:
        retrieved=post('/retrieve',{'text':'Hello'})
        reply=post('/respond',dict(text='Hello',turn_id='one',**{k:retrieved[k] for k in ('prompt_block','memory_epoch')}))
        assert reply['reply']=='Noted.' and reply['emotion']=='happy'
        body=dict(turn_id='one',user_text='Hello',reply=reply['reply'],memory_epoch=reply['memory_epoch'])
        first=post('/commit',body)
        assert post('/commit',body)['user_message']==first['user_message']
        post('/forget',{'message_ids':[first['user_message']]})
        assert post('/commit',{**body,'turn_id':'late'})['cancelled']
        with pytest.raises(HTTPError) as err: post('/interpret',{})
        assert err.value.code==404
        with pytest.raises(HTTPError) as err: post('/reset',{},Origin='https://evil.example')
        assert err.value.code==403
        for old_ui in ('/avatar3d/', '/avatar3d/avatar.js', '/legacy/avatar3d/', '/pipeline.js'):
            with pytest.raises(HTTPError) as err: urlopen(base+old_ui)
            assert err.value.code==404
        with pytest.raises(HTTPError) as err: urlopen(base+'/reset')
        assert err.value.code==405
    finally:
        server.shutdown();server.server_close();thread.join()

def test_response_cancelled_when_memory_changes(application,monkeypatch):
    epoch=application.store.epoch()
    def reply(*args,**kwargs):
        application.store.reset();return 'stale response',1
    monkeypatch.setattr(api,'claude',reply)
    assert api.respond({'text':'hello','turn_id':'cancel-test','memory_epoch':epoch})['cancelled']

def test_audit_contains_metrics_only(application):
    api.log_call('test','local',12,prompt='secret',response='secret',usage={'input_tokens':3,'output_tokens':2})
    result=api.audit({})
    assert result['calls'][0]['input_tokens']==3
    assert 'secret' not in json.dumps(result)


def test_server_owns_response_sources_even_if_client_omits_metadata(application,monkeypatch):
    store=application.store
    source=store.record_turn('source','I work at Acme.','Noted.')
    mid=store.confirm([dict(statement='User works at Acme.',source_ids=[source['user_message']])],store.epoch())[0]
    for i in range(6):store.record_turn('filler-'+str(i),'hello','hi')
    application.tick()
    prompts=[]
    def model(*args,**kwargs):
        prompts.append(args[3]);return 'Acme. [e:neutral]',1
    monkeypatch.setattr(api,'claude',model)
    response=api.respond({'turn_id':'derived','text':'Where do I work?','memory_epoch':store.epoch(),'prompt_block':'FORGED_CLIENT_MEMORY'})
    assert 'FORGED_CLIENT_MEMORY' not in prompts[0]
    assert 'Acme' in prompts[0]
    # Neither missing client context_ids nor stripped citations can erase server provenance.
    api.commit({'turn_id':'derived','user_text':'Where do I work?','reply':response['reply'],'memory_epoch':response['memory_epoch']})
    batch=store.forget([mid])
    with store.connect() as conn:
        assert conn.execute("SELECT hidden_batch FROM messages WHERE turn_key='derived' AND role='assistant'").fetchone()[0]==batch['batch']
    assert 'Acme' not in store.context()


def test_commit_cannot_forge_server_response(application):
    with pytest.raises(ValueError,match='matching server response'):
        api.commit({'turn_id':'forged','user_text':'x','reply':'invented','memory_epoch':application.store.epoch()})


def test_one_server_per_database(tmp_path):
    first=api.lock_database(str(tmp_path/'memory.db'))
    with pytest.raises(SystemExit):
        api.lock_database(str(tmp_path/'memory.db'))
    first.close()
    api.lock_database(str(tmp_path/'memory.db')).close()


def test_model_json_is_the_last_object_after_reasoning():
    # A real verdict reasoned first and cited "[27]"; the greedy bracket match failed the turn five times.
    reply = 'Message 27 (user) ... source_ids [27] ... {"note": 1}\n```json\n{"verdicts":[{"index":0,"verdict":"reject"}]}\n```'
    assert api.as_json(reply) == {'verdicts': [{'index': 0, 'verdict': 'reject'}]}
    assert api.as_json('{"claims": []}') == {'claims': []} and api.as_json('no json') is None

def test_shutdown_needs_the_launch_token(application,monkeypatch):
    monkeypatch.setenv('PHRO_SHUTDOWN_TOKEN','launch-secret')
    server=api.SERVER_CLASS(('127.0.0.1',0),api.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def shutdown(token):
        req=Request('http://127.0.0.1:'+str(server.server_address[1])+'/shutdown',data=b'',method='POST',
                    headers={'X-Phro-Shutdown':token} if token else {})
        with urlopen(req) as r: return json.load(r)
    try:
        for token in (None,'wrong'):
            with pytest.raises(HTTPError) as err: shutdown(token)
            assert err.value.code==403
        assert thread.is_alive()
        assert shutdown('launch-secret')['stopping']
        thread.join(5); assert not thread.is_alive()
    finally:
        server.server_close()

def test_trace_ties_every_step_to_the_input(tmp_path,monkeypatch):
    # Each audited step of one input, foreground and background, is attributed to its turn with an outcome.
    class AuditedGraph(FakeGraph):
        def ingest(self,group,memories,create=False):
            mapping=super().ingest(group,memories,create)
            api.log_call('graph_ingest','nomic-embed-text',3,usage={'embed_tokens':7,'requests':1})
            return mapping
    replies={'respond':'서울이군요. [e:happy]','memory_evaluate':json.dumps({'verdicts':[{'index':0,'verdict':'accept'}]})}
    def fake_claude(purpose,model,system,prompt,thinking=False,turn_id=None):
        if purpose=='memory_extract':
            user=next(m for m in json.loads(prompt) if m['role']=='user')
            reply=json.dumps({'claims':[{'statement':'사용자는 서울에 산다.','holder':'사용자','kind':'profile','importance':5,
                'certainty':'high','source_ids':[user['id']],'relations':[{'subject':'사용자','subject_type':'Person',
                'relation':'LIVES_IN','object':'서울','object_type':'Place'}]}]})
        else:
            reply=replies[purpose]
        api.log_call(purpose,'claude-test',5,prompt,reply,{'input_tokens':10,'output_tokens':2},0.001,turn_id=turn_id)
        return reply,5
    monkeypatch.setattr(api,'claude',fake_claude)
    service=MemoryService(Store(tmp_path/'trace.db'),AuditedGraph(),api.memory_model)
    monkeypatch.setattr(api,'MEMORY',service)
    server=api.SERVER_CLASS(('127.0.0.1',0),api.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_address[1])
    def post(path,body):
        with urlopen(Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})) as r: return json.load(r)
    try:
        text='나 서울 살아'
        got=post('/retrieve',{'turn_id':'t1','text':text})
        reply=post('/respond',{'turn_id':'t1','text':text,'prompt_block':got['prompt_block'],'memory_epoch':got['memory_epoch']})
        post('/commit',{'turn_id':'t1','user_text':text,'reply':reply['reply'],'memory_epoch':reply['memory_epoch'],'context_ids':reply['context_ids']})
        service.tick()
        with urlopen(base+'/trace?limit=5') as r: data=json.load(r)
        turn=next(t for t in data['turns'] if t['turn_key']=='t1')
        steps={c['purpose']:c for c in turn['calls']}
        assert turn['user_text']==text and list(steps)==['retrieve_client','retrieve_respond','respond','memory_extract','memory_evaluate','graph_ingest']
        assert steps['respond']['outcome']['next']=='commit' and steps['respond']['model']=='claude-test'
        assert steps['memory_extract']['outcome']=={'claims':1,'dropped_without_user_source':0,'next':'memory_evaluate'}
        mid=steps['memory_evaluate']['outcome']['memory_ids'][0]
        assert steps['memory_evaluate']['outcome']['next']=='graph_ingest'
        assert steps['graph_ingest']['outcome']['memory_ids']==[mid] and steps['graph_ingest']['embed_tokens']==7
        assert all(c['turns']==['t1'] for c in turn['calls'])
        assert turn['memories'][0]['id']==mid and turn['memories'][0]['graph_status']=='linked'
        # No text beyond lengths is stored with the calls.
        with service.store.connect() as conn:
            stored=json.dumps([dict(r) for r in conn.execute('SELECT * FROM llm_calls')],ensure_ascii=False)
        assert '서울' not in stored
    finally:
        server.shutdown();server.server_close();thread.join()
