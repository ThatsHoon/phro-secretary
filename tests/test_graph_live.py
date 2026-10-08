"""Opt-in local synthetic MemoryEngine integration; never opens a production memory DB."""
import os
import json
import threading
from urllib.request import Request,urlopen
import asyncio
import pytest
import server as api
from memory.store import Store
from memory.graph import Graph
from memory.service import MemoryService

def test_claude_client_answers_engine_prompts():
    # memory_engine: the engine's prompt goes to the transport, the JSON after any reasoning comes back validated.
    from memory_engine.llm_client.claude_cli_client import ClaudeCLIClient
    from memory_engine.llm_client.config import ModelSize
    from memory_engine.prompts import prompt_library
    from memory_engine.prompts.dedupe_edges import EdgeDuplicate
    seen=[]
    def transport(system,prompt,model):
        seen.append((system,prompt,model))
        return 'idx 1 is the old job [1]. {"note": {"x": 1}} {"duplicate_facts": [], "contradicted_facts": [1]}'
    client=ClaudeCLIClient(transport)
    context={'existing_edges':[{'idx':0,'fact':'a'}],'edge_invalidation_candidates':[{'idx':1,'fact':'b'}],'new_edge':'c'}
    answer=asyncio.run(client.generate_response(prompt_library.dedupe_edges.resolve_edge(context),
                                                response_model=EdgeDuplicate,model_size=ModelSize.small))
    assert answer=={'duplicate_facts':[],'contradicted_facts':[1]}
    system,prompt,model=seen[0]
    assert model=='haiku' and 'deduplication' in system and '<NEW FACT>' in prompt and 'contradicted_facts' in prompt
    bad=ClaudeCLIClient(lambda *a:'no json here')
    with pytest.raises(ValueError):
        asyncio.run(bad.generate_response(prompt_library.dedupe_edges.resolve_edge(context),response_model=EdgeDuplicate))

@pytest.mark.skipif(os.getenv('PHRO_LIVE_TEST')!='1',reason='set PHRO_LIVE_TEST=1 for local Ollama/FalkorDB test')
def test_real_graph_roundtrip(tmp_path,monkeypatch):
    store=Store(tmp_path/'live.db'); graph=Graph(store.path)
    def admission(purpose,model,system,prompt):
        # Deliberate model double: paid Claude transport is not part of local integration.
        if purpose=='memory_extract':
            evidence=json.loads(prompt)
            return {'claims':[dict(statement='Alice works at Acme Robotics in Seoul.',holder='Alice',source_ids=[evidence[0]['id']],
                                   relations=[{'subject':'Alice','subject_type':'Person','relation':'WORKS_AT',
                                               'object':'Acme Robotics','object_type':'Organization'}])]}
        return {'verdicts':[{'index':0,'verdict':'accept'}]}
    service=MemoryService(store,graph,admission)
    monkeypatch.setattr(api,'MEMORY',service)
    monkeypatch.setattr(api,'claude',lambda *a,**k:('Noted. [e:neutral]',1))
    server=api.SERVER_CLASS(('127.0.0.1',0),api.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def post(path,body):
        request=Request('http://127.0.0.1:'+str(server.server_address[1])+path,
                        data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=60) as response:return json.load(response)
    groups=set()
    original=graph.new_group
    def group():
        name=original(); groups.add(name); return name
    graph.new_group=group
    try:
        print('HEALTH',graph.health(),flush=True)
        text='Alice works at Acme Robotics in Seoul.'
        before=post('/retrieve',{'text':text})
        reply=post('/respond',{'turn_id':'synthetic','text':text,'prompt_block':before['prompt_block'],'memory_epoch':before['memory_epoch']})
        assert post('/commit',{'turn_id':'synthetic','user_text':text,'reply':reply['reply'],'memory_epoch':reply['memory_epoch'],'context_ids':reply['context_ids']})['committed']
        print('INGEST',service.tick(),flush=True)
        mid=store.visible_memories()[0]['id']
        result=post('/retrieve',{'text':'Where does Alice work?'})
        print('SEARCH',result,flush=True)
        assert not result['degraded']
        assert any(mid in m.get('source_memory_ids',[]) for m in result['memories'])
        service=MemoryService(Store(store.path),graph)
        assert service.retrieve('Alice Acme')['memories']
        batch=store.forget([mid])['batch']
        assert not service.retrieve('Alice')['memories']
        service.tick(); assert not service.retrieve('Alice')['memories']
        store.restore(batch); service.tick()
        assert service.retrieve('Alice Acme')['memories']
        batch=store.forget([mid])['batch']; store.purge(batch); service.tick()
        assert not service.retrieve('Alice')['memories']
        print('RESTART ARCHIVE RESTORE PURGE PASS',flush=True)
    finally:
        server.shutdown();server.server_close();thread.join()
        for name in groups: graph.delete(name)
        service.close()

@pytest.mark.skipif(os.getenv('PHRO_LIVE_TEST')!='1',reason='local model integration opt-in')
def test_korean_confirmed_memory(tmp_path):
    store=Store(tmp_path/'korean.db'); calls=[]
    graph=Graph(store.path,audit=lambda *a,**k:calls.append(k)); service=MemoryService(store,graph)
    group=None
    try:
        turn=store.record_turn('korean','민수는 서울에 살고 있다.','알겠습니다.')
        # Pinned lookup must also work for triplet-ingested memories (their episode node is written by graph.py).
        relation={'subject':'민수','subject_type':'Person','relation':'LIVES_IN','object':'서울','object_type':'Place'}
        mid=store.confirm([dict(statement='민수는 서울에 살고 있다.',holder='민수',pinned=True,source_ids=[turn['user_message']],relations=[relation])],store.epoch())[0]
        projected=service.tick();group=projected['group']
        result=service.retrieve('민수는 어디에 사나요?')
        print('KOREAN',result,flush=True)
        assert not result['degraded']
        assert any(m['id']==mid for m in result['memories'])
        assert any(m['id']==mid for m in service.retrieve('unrelated weather')['memories'])
        # Usage accounting reaches the audit hook. With exact-name endpoints a lone fact needs no Claude call
        # (no entity dedupe, no competing edge), only local embeddings.
        assert any(c['usage']['embed_tokens']>0 for c in calls)
    finally:
        state=store.graph_state()
        for name in {group,state['pending_group'],state['graph_group']} - {None}: graph.delete(name)
        service.close()

@pytest.mark.skipif(os.getenv('PHRO_LIVE_TEST')!='1',reason='local model integration opt-in')
def test_korean_verified_relations_are_retrievable(tmp_path):
    # KG-1: endpoints come from verified triples, so no entity extraction can drop them.
    store=Store(tmp_path/'relations.db'); graph=Graph(store.path); service=MemoryService(store,graph)
    rel=lambda s,st,r,o,ot:{'subject':s,'subject_type':st,'relation':r,'object':o,'object_type':ot}
    def remember(key,statement,relations):
        turn=store.record_turn(key,statement,'알겠습니다.')
        return store.confirm([dict(statement=statement,source_ids=[turn['user_message']],relations=relations)],store.epoch())[0]
    groups=set()
    try:
        seoul=remember('a','사용자는 서울에 살고 있다.',[rel('사용자','Person','LIVES_IN','서울','Place')])
        coffee=remember('b','사용자의 동료 지연은 커피를 좋아한다.',[rel('지연','Person','COLLEAGUE_OF','사용자','Person'),rel('지연','Person','LIKES','커피','Thing')])
        groups.add(service.tick()['group'])
        found=service.retrieve('사용자는 어디에 살아?')
        print('WHERE',found,flush=True)
        assert not found['degraded'] and any(m['id']==seoul for m in found['memories'])
        found=service.retrieve('지연은 무엇을 좋아해?')
        print('JIYEON',found,flush=True)
        assert any(m['id']==coffee for m in found['memories'])
        busan=remember('c','사용자는 이제 부산에 살고 있다.',[rel('사용자','Person','LIVES_IN','부산','Place')])
        groups.add(service.tick()['group'])
        found=service.retrieve('사용자는 지금 어디에 살아?')
        print('CORRECTION',found,flush=True)
        assert any(m['id']==busan for m in found['memories'])
        # KG-2: coverage is recorded per memory, separately from "projection done".
        assert store.graph_state()['memory_status']=={seoul:'linked',coffee:'linked',busan:'linked'}
        assert service.health()['coverage']=={'linked':3}
    finally:
        state=store.graph_state()
        for name in (groups|{state['pending_group'],state['graph_group']})-{None}: graph.delete(name)
        service.close()

def facts(graph, group):
    """(subject, relation, object, valid, fact) rows and entity names of a live projection."""
    from redis import Redis
    with Redis(port=graph.config.falkor_port) as redis:
        edges = redis.execute_command('GRAPH.RO_QUERY', group, 'MATCH (s:Entity)-[e:RELATES_TO]->(t:Entity) '
                                      'RETURN s.name, e.name, t.name, e.invalid_at IS NULL, e.fact')[1]
        names = redis.execute_command('GRAPH.RO_QUERY', group, 'MATCH (n:Entity) RETURN n.name')[1]
    text = lambda v: v.decode() if isinstance(v, bytes) else v
    rows = {(text(s), text(r), text(o), text(valid) in ('true', 1, True), text(fact)) for s, r, o, valid, fact in edges}
    return rows, {text(row[0]) for row in names}

@pytest.mark.skipif(os.getenv('PHRO_LIVE_TEST')!='1',reason='local model integration opt-in')
def test_forgetting_is_incremental_and_matches_a_rebuild(tmp_path):
    store=Store(tmp_path/'incremental.db'); graph=Graph(store.path); service=MemoryService(store,graph)
    rel=lambda s,r,o,ot='Thing':{'subject':s,'subject_type':'Person','relation':r,'object':o,'object_type':ot}
    def remember(key,statement,relations):
        turn=store.record_turn(key,statement,'알겠습니다.')
        return store.confirm([dict(statement=statement,source_ids=[turn['user_message']],relations=relations)],store.epoch())[0]
    groups=set()
    try:
        seoul=remember('a','사용자는 서울에 살고 있다.',[rel('사용자','LIVES_IN','서울','Place')])
        busan=remember('b','사용자는 이제 부산에 살고 있다.',[rel('사용자','LIVES_IN','부산','Place')])
        coffee=remember('c','지연은 커피를 좋아한다.',[rel('지연','LIKES','커피')])
        coffee2=remember('d','지연이 좋아하는 음료는 커피다.',[rel('지연','LIKES','커피')])
        cat=remember('e','민호는 고양이를 키운다.',[rel('민호','OWNS','고양이')])
        first=service.tick(); group=first['group']; groups.add(group)
        edges,_=facts(graph,group)
        print('BEFORE',sorted(edges),flush=True)
        assert ('사용자','LIVES_IN','서울',False,'사용자는 서울에 살고 있다.') in edges  # superseded by 부산
        # Two wordings of one fact: MemoryEngine may merge them into one edge with two sources, or keep two edges.
        shared=len([e for e in edges if e[:3]==('지연','LIKES','커피')])==1
        # Forgetting the correction, the first wording of a shared fact, and a fact with its own entities.
        store.forget([busan]); store.forget([coffee]); store.forget([cat])
        step=service.tick()
        print('REMOVE',step,flush=True)
        assert step['group']==group and not step['rebuild'] and step['removed']==3 and step['added']==0
        edges,names=facts(graph,group)
        print('AFTER',sorted(edges),sorted(names),'shared' if shared else 'separate',flush=True)
        assert ('사용자','LIVES_IN','서울',True,'사용자는 서울에 살고 있다.') in edges   # valid again
        assert ('지연','LIKES','커피',True,'지연이 좋아하는 음료는 커피다.') in edges    # kept (reworded if shared)
        assert not {'부산','민호','고양이'} & names
        assert not any('부산' in e[4] or e[4]=='지연은 커피를 좋아한다.' for e in edges)
        # The same visible memories built from scratch give the same graph.
        with store.connect() as conn: conn.execute('UPDATE runtime SET rebuild=1 WHERE id=1')
        rebuilt=service.tick(); groups.add(rebuilt['group'])
        assert rebuilt['rebuild']
        assert facts(graph,rebuilt['group'])==(edges,names)
        found=service.retrieve('사용자는 어디에 살아?')
        assert any(m['id']==seoul for m in found['memories'])
        assert all(m['id']!=busan for m in found['memories'])
        assert any(m['id']==coffee2 for m in service.retrieve('지연은 무엇을 좋아해?')['memories'])
    finally:
        state=store.graph_state()
        for name in (groups|{state['pending_group'],state['graph_group']})-{None}: graph.delete(name)
        service.close()

@pytest.mark.skipif(os.getenv('PHRO_LIVE_TEST')!='1',reason='local model integration opt-in')
def test_forgetting_a_middle_correction_and_restoring_it(tmp_path):
    store=Store(tmp_path/'chain.db'); graph=Graph(store.path); service=MemoryService(store,graph)
    rel=lambda o:[{'subject':'사용자','subject_type':'Person','relation':'LIVES_IN','object':o,'object_type':'Place'}]
    def remember(key,statement,place):
        turn=store.record_turn(key,statement,'알겠습니다.')
        return store.confirm([dict(statement=statement,source_ids=[turn['user_message']],relations=rel(place))],store.epoch())[0]
    groups=set()
    try:
        remember('a','사용자는 서울에 살고 있다.','서울')
        busan=remember('b','사용자는 이제 부산에 살고 있다.','부산')
        remember('c','사용자는 이제 대전에 살고 있다.','대전')
        groups.add(service.tick()['group'])
        valid=lambda: {e[2] for e in facts(graph,store.graph_state()['graph_group'])[0] if e[1]=='LIVES_IN' and e[3]}
        assert valid()=={'대전'}
        # 대전 superseded 부산, which had superseded 서울: forgetting 부산 must not bring 서울 back.
        batch=store.forget([busan])['batch']; step=service.tick()
        assert not step['rebuild'] and step['removed']==1
        print('CHAIN',sorted(facts(graph,step['group'])[0]),flush=True)
        assert valid()=={'대전'}
        assert '부산' not in facts(graph,step['group'])[1]
        store.restore(batch); step=service.tick()
        assert not step['rebuild'] and step['added']==1
        print('RESTORED',sorted(facts(graph,step['group'])[0]),flush=True)
        # Restored with its original date, 부산 is older than 대전, so 대전 stays the current one.
        assert '대전' in valid()
    finally:
        state=store.graph_state()
        for name in (groups|{state['pending_group'],state['graph_group']})-{None}: graph.delete(name)
        service.close()

@pytest.mark.skipif(os.getenv('PHRO_LIVE_TEST')!='1',reason='local model integration opt-in')
def test_moved_or_copied_database_adopts_its_graph(tmp_path):
    import shutil, sqlite3
    from redis import Redis
    from memory.graph import prefix_for
    def graphs():
        with Redis(port=6379) as r: return {n.decode() for n in r.execute_command('GRAPH.LIST')}
    def open_db(path):
        store=Store(path); return store,MemoryService(store,Graph(store.path))
    origin=tmp_path/'origin.db'; store,service=open_db(origin)
    prefixes=set()
    try:
        turn=store.record_turn('a','사용자는 서울에 살고 있다.','알겠습니다.')
        relation={'subject':'사용자','subject_type':'Person','relation':'LIVES_IN','object':'서울','object_type':'Place'}
        mid=store.confirm([dict(statement='사용자는 서울에 살고 있다.',source_ids=[turn['user_message']],relations=[relation])],store.epoch())[0]
        original=service.tick()['group']; prefixes.add(prefix_for(store.path)); service.close()
        # A copy (backup restored elsewhere): the original still needs its graph, so it is left in place.
        copy=tmp_path/'copy.db'
        src,dst=sqlite3.connect(origin),sqlite3.connect(copy)
        src.backup(dst); src.close(); dst.close()
        store,service=open_db(copy); prefixes.add(prefix_for(store.path))
        step=service.tick()
        assert step['rebuild'] and step['group']!=original and original in graphs()
        assert any(m['id']==mid for m in service.retrieve('사용자는 어디 살아?')['memories'])
        service.close()
        # A move: the old path is gone, so the graph it named is deleted and a new one built.
        moved=tmp_path/'moved'/'origin.db'; moved.parent.mkdir()
        for suffix in ('','-wal','-shm'):
            if os.path.exists(str(origin)+suffix): shutil.move(str(origin)+suffix,str(moved)+suffix)
        store,service=open_db(moved); prefixes.add(prefix_for(store.path))
        step=service.tick()
        assert step['rebuild'] and original not in graphs()
        assert any(m['id']==mid for m in service.retrieve('사용자는 어디 살아?')['memories'])
        assert not service.retrieve('사용자는 어디 살아?')['degraded']
    finally:
        service.close()
        with Redis(port=6379) as r:
            for name in graphs():
                if any(name.startswith(p) for p in prefixes): r.execute_command('GRAPH.DELETE',name)

@pytest.mark.skipif(os.getenv('PHRO_LIVE_TEST')!='1' or os.getenv('PHRO_CLAUDE_TEST')!='1' or not api.CLAUDE,
                    reason='paid: set PHRO_LIVE_TEST=1 PHRO_CLAUDE_TEST=1 for Claude-backed MemoryEngine calls')
def test_claude_judges_what_rules_cannot(tmp_path,monkeypatch):
    # Vocabulary relations are settled by rule (a job change supersedes, a second liking coexists); a relation
    # outside it (DRIVES) goes to MemoryEngine's resolver on Claude; a memory without verified triples is extracted by
    # MemoryEngine's own add_episode pipeline on Claude. No local chat model is involved.
    store=Store(tmp_path/'judge.db'); graph=Graph(store.path,audit=api.log_call,llm=api.graph_model)
    service=MemoryService(store,graph); monkeypatch.setattr(api,'MEMORY',service)
    rel=lambda r,o,ot='Organization':{'subject':'사용자','subject_type':'Person','relation':r,'object':o,'object_type':ot}
    def remember(key,statement,relations):
        turn=store.record_turn(key,statement,'알겠습니다.')
        return store.confirm([dict(statement=statement,source_ids=[turn['user_message']],relations=relations)],store.epoch())[0]
    groups=set()
    try:
        remember('a','사용자는 토스에 다닌다.',[rel('WORKS_AT','토스')])
        remember('b','사용자는 커피를 좋아한다.',[rel('LIKES','커피','Thing')])
        remember('car1','사용자는 아반떼를 탄다.',[rel('DRIVES','아반떼','Thing')])
        groups.add(service.tick()['group'])
        kakao=remember('c','사용자는 회사를 옮겨 이제 카카오에 다닌다.',[rel('WORKS_AT','카카오')])
        remember('d','사용자는 녹차도 좋아한다.',[rel('LIKES','녹차','Thing')])
        remember('car2','사용자는 차를 바꿔서 이제 소나타를 탄다.',[rel('DRIVES','소나타','Thing')])
        groups.add(service.tick()['group'])
        rows,_=facts(graph,store.graph_state()['graph_group'])
        valid={(s,r,o) for s,r,o,ok,_ in rows if ok}
        print('JUDGED',sorted(rows),flush=True)
        assert ('사용자','WORKS_AT','카카오') in valid and ('사용자','WORKS_AT','토스') not in valid
        assert {('사용자','LIKES','커피'),('사용자','LIKES','녹차')} <= valid
        assert ('사용자','DRIVES','소나타') in valid and ('사용자','DRIVES','아반떼') not in valid  # Claude's call
        # The judgement is recorded as expansion evidence; once promoted, the next car is settled by rule.
        assert {c['relation']:c['judgements'] for c in service.expansion()['candidates']}['DRIVES']=={'first':1,'contradicts':1}
        service.promote('DRIVES','single')
        with store.connect() as conn:
            before=conn.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose='memory_engine'").fetchone()[0]
        remember('car3','사용자는 이제 그랜저를 탄다.',[rel('DRIVES','그랜저','Thing')])
        groups.add(service.tick()['group'])
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose='memory_engine'").fetchone()[0]==before
        rows,_=facts(graph,store.graph_state()['graph_group'])
        valid={(s,r,o) for s,r,o,ok,_ in rows if ok}
        assert ('사용자','DRIVES','그랜저') in valid and ('사용자','DRIVES','소나타') not in valid
        # Forgetting the job change re-validates the old job (invalidated_by points at the judged episode).
        batch=store.forget([kakao])['batch']; service.tick()
        rows,_=facts(graph,store.graph_state()['graph_group'])
        assert ('사용자','WORKS_AT','토스',True) in {row[:4] for row in rows}
        store.restore(batch)
        legacy=remember('e','민지는 대전에 산다.',None)
        groups.add(service.tick()['group'])
        assert store.graph_state()['memory_status'][legacy]=='linked'
        found=service.retrieve('민지는 어디 살아?')
        print('LEGACY',found,flush=True)
        assert any(m['id']==legacy for m in found['memories'])
        with store.connect() as conn:
            judged=conn.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose='memory_engine' AND error IS NULL").fetchone()[0]
            local=conn.execute("SELECT SUM(input_tokens) FROM llm_calls WHERE purpose IN ('graph_ingest','graph_remove','graph_search')").fetchone()[0]
            # Trace reaches into the graph event loop and MemoryEngine's worker threads: judgements carry the turn.
            rows=[dict(r) for r in conn.execute("SELECT turns,outcome FROM llm_calls WHERE purpose IN ('memory_engine','graph_ingest')")]
        assert judged>0 and not local  # MemoryEngine's judgement ran on Claude; local sessions only embed
        assert all(r['turns'] for r in rows), rows
        assert any(r['outcome'] and 'judgement' in r['outcome'] for r in rows), rows
    finally:
        state=store.graph_state()
        for name in (groups|{state['pending_group'],state['graph_group']})-{None}: graph.delete(name)
        service.close()
