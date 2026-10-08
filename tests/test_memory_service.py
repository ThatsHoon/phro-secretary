import json
from types import SimpleNamespace
import pytest
from memory.store import Store
from memory.service import MemoryService

class FakeGraph:
    config = SimpleNamespace(digest=lambda:'config')
    def __init__(self):
        self.groups = {}; self.sequence = 0; self.fail = False; self.hook = None
        self.learned = {'single':set(),'multi':set()}; self.observed = []
    def take_observations(self):
        observed, self.observed = self.observed, []
        return observed
    def new_group(self):
        self.sequence += 1
        return 'group-'+str(self.sequence)
    def ingest(self, group, memories, create=False):
        if create: self.groups[group] = {}
        mapping = {}
        for m in memories:
            ep = 'episode-'+str(m['id'])
            self.groups[group][ep] = m['statement']
            mapping[ep] = [m['id']]
        if self.hook: self.hook()
        if self.fail: raise RuntimeError('lost acknowledgement')
        return mapping
    def search(self,group,text,limit=12,pinned_episodes=(),turn_id=None,context=()):
        return [dict(uuid=ep,fact=fact,episodes=[ep],invalid_at=None) for ep,fact in self.groups[group].items()]
    def edge_counts(self,group,episodes): return {ep:1 for ep in episodes if ep in self.groups.get(group,{})}
    def owns(self,group): return group.startswith('group-')
    def remove(self,group,episodes,statements):
        for ep in episodes: self.groups[group].pop(ep)
    def delete(self,group): self.groups.pop(group,None)
    def close(self): pass

def setup(tmp_path):
    store = Store(tmp_path/'memory.db'); graph = FakeGraph()
    return store,graph,MemoryService(store,graph)

def add(store,key='one'):
    turn = store.record_turn(key,'I work at Acme.','Noted.')
    return store.confirm([dict(statement='User works at Acme.',source_ids=[turn['user_message']])],store.epoch())[0]

def test_projection_append_restart_archive_restore_purge(tmp_path):
    store,graph,service = setup(tmp_path); mid=add(store)
    service.tick(); group=store.graph_state()['graph_group']
    assert service.retrieve('work')['memories'][0]['id']==mid
    add(store,'two'); service.tick()
    assert store.graph_state()['graph_group']==group
    service=MemoryService(Store(store.path),graph)
    batch=store.forget([mid])['batch']
    # Before the worker runs, the forgotten memory is already filtered out of retrieval.
    assert [m['id'] for m in service.retrieve('work')['memories']]==[2]
    # Archive, restore and purge reconcile the same graph: only the affected episode changes.
    assert service.tick()['removed']==1 and graph.groups[group].keys()=={'episode-2'}
    assert all(m['id']!=mid for m in service.retrieve('work')['memories'])
    store.restore(batch); assert service.tick()['added']==1
    assert mid in [m['id'] for m in service.retrieve('work')['memories']]
    batch=store.forget([mid])['batch']; store.purge(batch); service.tick()
    assert all(m['id']!=mid for m in service.retrieve('work')['memories'])
    assert list(graph.groups)==[group]

def test_uncertain_write_rebuilds_without_duplicate_append(tmp_path):
    store,graph,service=setup(tmp_path); add(store); service.tick()
    old=store.graph_state()['graph_group']; add(store,'two'); graph.fail=True
    with pytest.raises(RuntimeError): service.tick()
    assert not service.retrieve('work')['memories']
    graph.fail=False; store.retry(); MemoryService(Store(store.path),graph).tick()
    assert old not in graph.groups
    assert len(service.retrieve('work')['memories'])==2

def test_revision_and_privacy_races_fail_closed(tmp_path):
    store,graph,service=setup(tmp_path); mid=add(store)
    graph.hook=lambda:store.forget([mid])
    assert service.tick()['stale']
    assert not store.graph_state()['ready']
    graph.hook=None; service.tick()
    assert not service.retrieve('work')['memories']
    assert len(graph.groups)==1

def test_admission_requires_single_accept_and_user_evidence(tmp_path):
    store,graph,service=setup(tmp_path)
    turn=store.record_turn('one','I work at Acme.','You are CEO.')
    claim=dict(statement='User works at Acme.',source_ids=[turn['user_message']])
    def llm(purpose,*args):
        return {'claims':[claim]} if purpose=='memory_extract' else {'verdicts':[{'index':0,'verdict':'accept'},{'index':0,'verdict':'accept'}]}
    service.llm=llm; service.tick(); assert not store.visible_memories()
    turn=store.record_turn('two','I work at Acme.','Noted.')
    claim['source_ids']=[turn['user_message']]
    service.llm=lambda purpose,*args: {'claims':[claim]} if purpose=='memory_extract' else {'verdicts':[{'index':0,'verdict':'accept'}]}
    service.tick(); assert len(store.visible_memories())==1

def test_rejection_reasons_are_counted_and_flagged(tmp_path):
    from memory import trace
    store,graph,service=setup(tmp_path)
    def llm(purpose,model,system,prompt):
        # Audited like server.claude: a row the trace can attach the step's outcome to.
        with store.connect() as conn:
            trace.record(conn.execute('INSERT INTO llm_calls(purpose,model,ms,turn_id) VALUES(?,?,0,?)',
                                      (purpose,model,trace.turns()[0])).lastrowid)
        if purpose=='memory_extract':
            return {'claims':[dict(statement=f'Claim {i}.',source_ids=[turn['user_message']]) for i in range(4)]}
        return {'verdicts':[{'index':0,'verdict':'accept'},{'index':1,'verdict':'reject','reason':'date'},
                            {'index':2,'verdict':'reject','reason':'made up'}]}
    service.llm=llm
    for i in range(2):
        turn=store.record_turn(f't{i}',f'Trip on day {i}.','Noted.'); service.tick()
    report=service.expansion()['rejections']
    found={r['reason']:r for r in report['reasons']}
    assert (report['checks'],report['claims'],report['rejected'])==(2,8,6)
    # An unknown reason is "other"; a claim the check skipped is "omitted".
    assert {k:r['count'] for k,r in found.items()}=={'date':2,'other':2,'omitted':2}
    assert not found['date']['flagged'] and [e['user_text'] for e in found['date']['examples']]==['Trip on day 1.','Trip on day 0.']
    turn=store.record_turn('t2','Trip on day 2.','Noted.'); service.tick()
    assert {r['reason']:r['flagged'] for r in service.expansion()['rejections']['reasons']}['date']
    # A forgotten turn still counts but is never shown.
    store.forget([m['id'] for m in store.visible_memories()][:1])
    date={r['reason']:r for r in service.expansion()['rejections']['reasons']}['date']
    assert date['count']==3 and 'Trip on day 0.' not in [e['user_text'] for e in date['examples']]

def test_shared_source_forget_closure_and_stale_commit(tmp_path):
    store,graph,service=setup(tmp_path)
    a=store.record_turn('one','a','a reply'); b=store.record_turn('two','b','b reply')
    store.confirm([dict(statement='a',source_ids=[a['user_message']]),dict(statement='both',source_ids=[a['user_message'],b['user_message']])],store.epoch())
    epoch=store.epoch(); batch=store.forget([1])
    assert len(batch['message_ids'])==4
    assert store.context()=='(없음)'
    with pytest.raises(ValueError): store.record_turn('late','secret','secret',epoch=epoch)

def test_summary_rejects_privacy_change(tmp_path):
    store,graph,service=setup(tmp_path)
    for i in range(11): store.record_turn(str(i),'user','reply')
    def llm(*args):
        store.forget(message_ids=[1]); return {'summary':'secret'}
    service.llm=llm
    assert service.summary_due()
    assert service.summarize()['stale']
    assert 'secret' not in store.context()


def test_archive_between_graph_state_and_source_snapshot_is_reconciled(tmp_path):
    store,graph,service=setup(tmp_path); mid=add(store); service.tick()
    original=store.projection_input; old=store.graph_state()['graph_group']
    add(store,'another')
    def snapshot():
        store.forget([mid]); return original()
    store.projection_input=snapshot
    service.tick()
    assert graph.groups[old].keys()=={'episode-2'}
    assert all(mid not in ids for ids in store.graph_state()['episode_map'].values())


def test_purge_removes_turn_digest_and_blocks_replay(tmp_path):
    store,graph,service=setup(tmp_path); mid=add(store)
    store.purge(store.forget([mid])['batch'])
    with store.connect() as conn:
        assert conn.execute('SELECT digest FROM turns').fetchone()[0]==''
    with pytest.raises(ValueError): store.record_turn('one','I work at Acme.','Noted.')


def test_graph_retries_stop_and_explicit_retry_recovers(tmp_path):
    store,graph,service=setup(tmp_path);add(store);graph.fail=True
    for attempt in range(5):
        with store.connect() as conn: conn.execute('UPDATE runtime SET graph_retry_at=0')
        with pytest.raises(RuntimeError): service.tick()
    assert service.tick()['retry_pending']
    assert store.graph_state()['graph_attempts']==5
    graph.fail=False;store.retry();service.tick()
    assert service.retrieve('Acme')['memories']


def test_invalidated_pinned_fact_cannot_bypass_graph_validity(tmp_path):
    store,graph,service=setup(tmp_path);mid=add(store);service.tick()
    with store.connect() as conn: conn.execute('UPDATE memories SET pinned=1 WHERE id=?',(mid,))
    graph.search=lambda *a,**k: [dict(uuid='edge',fact='User works at Acme.',episodes=['episode-'+str(mid)],invalid_at='2020-01-01T00:00:00Z')]
    assert service.retrieve('Acme')['memories']==[]
    graph.search=lambda *a,**k: [dict(uuid='edge',fact='Current atomic fact',episodes=['episode-'+str(mid)],invalid_at=None)]
    result=service.retrieve('Acme')['memories'][0]
    assert result['text']=='Current atomic fact' and result['score']>100


def test_retry_exhaustion_still_cleans_uncertain_graph(tmp_path):
    store,graph,service=setup(tmp_path);add(store);graph.fail=True
    for attempt in range(5):
        with store.connect() as conn: conn.execute('UPDATE runtime SET graph_retry_at=0')
        with pytest.raises(RuntimeError):service.tick()
    assert graph.groups
    assert service.tick()['retry_pending']
    assert not graph.groups and store.graph_state()['pending_group'] is None


def test_forget_hides_replies_that_used_the_conversation_or_retrieved_memory(tmp_path):
    store,graph,service=setup(tmp_path)
    original=store.record_turn('source','My name is Minjun.','Hello Minjun.')
    mid=store.confirm([dict(statement='User is Minjun.',source_ids=[original['user_message']])],store.epoch())[0]
    _,dependencies=store.context_snapshot([mid])
    store.record_turn('recall','What is my name?','Minjun.',context_ids=dependencies)
    _,dependencies=store.context_snapshot()
    store.record_turn('followup','Repeat it.','Minjun.',context_ids=dependencies)
    batch=store.forget([mid])
    # The source turn goes whole; later turns lose only the replies that may repeat it.
    assert len(batch['message_ids'])==4 and batch['turns']==1 and batch['reply_turns']==2
    assert 'Minjun' not in store.context() and 'Repeat it.' in store.context()
    store.restore(batch['batch'])
    assert 'Minjun' in store.context()
    batch=store.forget([mid]);store.purge(batch['batch'])
    with store.connect() as conn:
        assert not any('Minjun' in r[0] for r in conn.execute('SELECT text FROM messages'))


def test_graph_only_context_provenance_survives_absence_of_citation(tmp_path):
    store,graph,service=setup(tmp_path);mid=add(store)
    _,dependencies=store.context_snapshot([mid],limit=0)
    store.record_turn('derived','What was my job?','Acme.',context_ids=dependencies)
    store.forget([mid])
    assert store.context()=='user: What was my job?'


def test_malformed_relations_from_extraction_fall_back_to_none(tmp_path):
    from memory.service import MemoryService
    store = Store(tmp_path / 'memory.db')
    store.record_turn('one', '나는 서울에 살아.', 'ok')
    def llm(purpose, model, system, prompt):
        if purpose == 'memory_extract':
            source = json.loads(prompt)[0]['id']
            return {'claims': [{'statement': '사용자는 서울에 살고 있다.', 'holder': '사용자', 'source_ids': [source],
                                'relations': [{'subject': '사용자', 'relation': 'LIVES_IN', 'object': ''}]}]}
        return {'verdicts': [{'index': 0, 'verdict': 'accept'}]}
    service = MemoryService(store, FakeGraph(), llm)
    service.tick()
    assert [m['relations'] for m in store.visible_memories()] == [None]

def test_backfill_relations_for_unanalysed_memories(tmp_path):
    store = Store(tmp_path / 'memory.db')
    store.record_turn('one', '나는 서울에 살고 커피를 안 마셔.', 'ok')
    def extract(purpose, model, system, prompt):
        if purpose == 'memory_extract':
            source = json.loads(prompt)[0]['id']
            return {'claims': [{'statement': s, 'holder': '사용자', 'source_ids': [source]}
                               for s in ('사용자는 서울에 살고 있다.', '사용자는 커피를 마시지 않는다.')]}
        return {'verdicts': [{'index': 0, 'verdict': 'accept'}, {'index': 1, 'verdict': 'accept'}]}
    service = MemoryService(store, FakeGraph(), extract)
    service.tick()
    seoul, coffee = store.visible_memories()
    assert seoul['relations'] is None and coffee['relations'] is None
    calls = []
    def relate(purpose, model, system, prompt):
        calls.append(purpose)
        if purpose == 'relations_backfill':
            return {'items': [{'id': seoul['id'], 'relations': [{'subject': '사용자', 'relation': 'LIVES_IN', 'object': '서울'}]},
                              {'id': coffee['id'], 'relations': [{'subject': '사용자', 'relation': 'DRINKS', 'object': '커피'}]},
                              {'id': 999, 'relations': []}]}
        # The check rejects the dropped negation.
        return {'verdicts': [{'id': seoul['id'], 'verdict': 'accept'}, {'id': coffee['id'], 'verdict': 'reject'}]}
    service.llm = relate
    revision = store.graph_state()['revision']
    assert service.backfill_relations() == {'analysed': 2, 'accepted': 1, 'remaining': 1}
    assert calls == ['relations_backfill', 'relations_check']
    relations = {m['id']: m['relations'] for m in store.visible_memories()}
    assert relations[seoul['id']][0]['relation'] == 'LIVES_IN' and relations[coffee['id']] is None
    state = store.graph_state()
    assert state['revision'] == revision + 1 and not state['rebuild'] and state['stale'] == {seoul['id']}
    # Only the re-analysed memory's episode is replaced.
    assert service.tick()['removed'] == 1 and store.graph_state()['stale'] == set()

def test_claim_citing_the_reply_is_dropped_without_failing_the_turn(tmp_path):
    store,graph,service=setup(tmp_path)
    turn=store.record_turn('trip','What should I pack for Osaka?','Since you live in Busan, pack light.')
    def llm(purpose,model,system,prompt):
        if purpose=='memory_extract':
            return {'claims':[dict(statement='User plans to visit Osaka.',source_ids=[turn['user_message']]),
                              dict(statement='User lives in Busan.',source_ids=[turn['assistant_message']]),
                              dict(statement='No evidence.',source_ids=[])]}
        assert 'Busan' not in prompt.split('PROPOSALS:')[1]  # never reaches evaluation
        return {'verdicts':[{'index':0,'verdict':'accept'}]}
    service.llm=llm; service.tick()
    assert [m['statement'] for m in store.visible_memories()]==['User plans to visit Osaka.']
    with store.connect() as conn:
        assert conn.execute('SELECT status FROM turns').fetchone()[0]=='done'

@pytest.mark.parametrize('text,skip',[
    ('내가 어디 산다고 했지?',True), ('고양이 키우는 사람이 나야, 수아야?',True),
    ('요즘 나 어디 산다고?',True), ('만약 내가 고양이를 키운다면 이름을 뭐로 지을까?',True),
    # Anything that may state or presuppose a fact is extracted: help requests, imperatives, declaratives and
    # questions carrying a clause of their own.
    ('오사카 여행 준비물 뭐 챙기면 좋을까?',False), ('What should I pack for Osaka?',False),
    ('내 직업이 뭔지 맞혀 봐.',False), ('부산 이사했는데 맛집 어디야?',False), ('부산으로 이사했고 맛집 어디야?',False),
    ('나 서울 살고 근처 맛집 뭐 있어?',False), ('다음 달 오사카 가는데 준비물 뭐 챙길까?',False),
    ('나 부산 이사했어. 맛집 어디야?',False), ('주말에 친구랑 등산 갈 건데 뭐 입어?',False),
    ('회사 옮겼어, 이제 카카오 다녀.',False), ('부산은 바다가 가까워서 좋더라.',False)])
def test_question_only_turns_skip_extraction(text,skip):
    from memory.service import question_only
    assert question_only(text) is skip

def test_skipped_extraction_is_recorded_without_a_model_call(tmp_path):
    store = Store(tmp_path/'skip.db'); calls = []
    service = MemoryService(store,FakeGraph(),lambda *a: calls.append(a) or {'claims':[]})
    store.record_turn('q','내가 어디 산다고 했지?','서울이라고 하셨어요.')
    service.tick()
    assert not calls
    with store.connect() as conn:
        row = dict(conn.execute("SELECT * FROM llm_calls WHERE purpose='memory_extract_skipped'").fetchone())
        status = conn.execute("SELECT status FROM turns WHERE turn_key='q'").fetchone()[0]
    assert row['turn_id']=='q' and row['model']=='rule' and json.loads(row['outcome'])['reason']=='question only' and status=='done'

def test_relation_vocabulary_is_classified():
    # Every relation the extraction prompt prefers has a rule (graph.SINGLE / MULTI); none reaches Claude.
    import re
    from memory.graph import SINGLE, MULTI
    from memory.service import RELATION_RULES
    preferred = set(re.findall(r'\b[A-Z][A-Z_]{2,}\b', RELATION_RULES.split('Prefer these relations:')[1].split('Otherwise')[0]))
    assert preferred and preferred <= SINGLE | MULTI and not SINGLE & MULTI

def test_expansion_candidates_suggest_and_promote(tmp_path):
    store,graph,service = setup(tmp_path)
    ids = [add(store,f'k{i}') for i in range(7)]
    store.observe([('DRIVES',mid,'contradicts') for mid in ids[:5]] + [('DRIVES',ids[5],'first'),
                  ('VISITED',ids[0],'coexists'),('VISITED',ids[1],'contradicts'),('LIKES',ids[2],'coexists')])
    found = {c['relation']:c for c in service.expansion()['candidates']}
    assert 'LIKES' not in found  # built-in relations are never candidates
    assert found['DRIVES']['suggestion']=='single' and found['DRIVES']['evidence']==5 and found['DRIVES']['count']==6
    assert found['VISITED']['suggestion'] is None  # too little and mixed evidence
    assert found['DRIVES']['examples'][0]['statement']=='User works at Acme.'
    for bad in [('LIKES','single'),('NOT_DRIVES','single'),('drives','single'),('DRIVES','other')]:
        with pytest.raises(ValueError): service.promote(*bad)
    service.promote('DRIVES','single')
    assert store.vocabulary()=={'single':{'DRIVES'},'multi':set()}
    data = service.expansion()
    assert 'DRIVES' not in {c['relation'] for c in data['candidates']} and data['learned']==[{'relation':'DRIVES','kind':'single'}]
    service.tick()
    assert graph.learned=={'single':{'DRIVES'},'multi':set()}  # handed to the graph before each projection
    assert store.demote('DRIVES')==1 and 'DRIVES' in {c['relation'] for c in service.expansion()['candidates']}
