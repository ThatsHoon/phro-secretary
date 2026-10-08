from memory.store import Store
import pytest


def test_turn_identity_and_confirmed_memory_provenance(tmp_path):
    store = Store(tmp_path / 'memory.db')
    turn = store.record_turn('turn-1', 'I work at Acme.', 'Noted.')
    assert store.record_turn('turn-1', 'I work at Acme.', 'Noted.') == turn
    with pytest.raises(ValueError):
        store.record_turn('turn-1', 'Different input', 'Noted.')
    ids = store.confirm([{'statement': 'The user works at Acme.', 'kind': 'profile',
                          'source_ids': [turn['user_message']]}], store.epoch())
    assert len(store.visible_memories()) == 1
    assert store.visible_memories()[0]['id'] == ids[0]
    assert store.visible_memories()[0]['source_ids'] == [turn['user_message']]
    with pytest.raises(ValueError):
        store.confirm([{'statement': 'invented', 'source_ids': [999]}], store.epoch())


def test_forget_restore_purge_and_stale_confirmation(tmp_path):
    store = Store(tmp_path / 'memory.db')
    turn = store.record_turn('turn-1', 'private Acme role', 'private reply')
    epoch = store.epoch()
    ids = store.confirm([{'statement': 'private role', 'source_ids': [turn['user_message']]}], epoch)
    batch = store.forget(ids)
    assert store.visible_memories() == []
    assert 'private' not in store.context()
    with pytest.raises(ValueError):
        store.confirm([{'statement': 'late secret', 'source_ids': [turn['user_message']]}], epoch)
    store.restore(batch['batch'])
    assert len(store.visible_memories()) == 1
    batch = store.forget(ids)
    store.purge(batch['batch'])
    assert 'private' not in store.context()
    with pytest.raises(ValueError):
        store.restore(batch['batch'])


def test_projection_publish_rejects_changed_sources(tmp_path):
    store = Store(tmp_path / 'memory.db')
    turn = store.record_turn('one', 'Acme', 'ok')
    store.confirm([{'statement': 'Acme', 'source_ids': [turn['user_message']]}], store.epoch())
    revision, _ = store.projection_input()
    store.forget([1])
    assert not store.publish_projection(revision, 'phro_test_revision', {'episode': [1]})
    assert store.graph_state()['ready'] is False


def test_restart_keeps_pending_turn_and_memory(tmp_path):
    path = tmp_path / 'memory.db'
    Store(path).record_turn('one', 'Acme', 'ok')
    store = Store(path)
    turn = store.claim_turn()
    assert turn['turn_key'] == 'one'
    assert store.claim_turn() is None
    store.finish_turn(turn['turn_key'], turn['lease'], [], store.epoch())
    assert Store(path).claim_turn() is None


def test_relations_are_validated_stored_and_added_to_old_databases(tmp_path):
    import sqlite3
    path = tmp_path / 'memory.db'
    store = Store(path)
    turn = store.record_turn('one', '나는 서울에 살아.', 'ok')
    rel = {'subject': '사용자', 'subject_type': 'Person', 'relation': 'lives in', 'object': '서울', 'object_type': 'City'}
    store.confirm([{'statement': '사용자는 서울에 살고 있다.', 'source_ids': [turn['user_message']], 'relations': [rel]}], store.epoch())
    saved = store.visible_memories()[0]['relations']
    assert saved == [{'subject': '사용자', 'relation': 'LIVES_IN', 'object': '서울',
                      'subject_type': 'Person', 'object_type': 'Entity'}]
    for bad in ([{**rel, 'object': '사용자'}], [{**rel, 'relation': 'X; DROP'}], [rel] * 6, 'x'):
        with pytest.raises(ValueError):
            store.confirm([{'statement': 'other', 'source_ids': [turn['user_message']], 'relations': bad}], store.epoch())
    # A database from before the column existed gains it; its memories count as never analysed.
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE memories DROP COLUMN relations')
        conn.execute('ALTER TABLE runtime DROP COLUMN graph_status')
    reopened = Store(path)
    assert reopened.visible_memories()[0]['relations'] is None
    assert reopened.graph_state()['memory_status'] == {}


def test_forgetting_reaches_later_untracked_imported_replies(tmp_path):
    store = Store(tmp_path / 'memory.db')
    def legacy(key, user, reply, stamp):
        store.record_turn(key, user, reply, created_at=stamp)
        with store.connect() as conn:
            conn.execute('UPDATE turns SET untracked=1 WHERE turn_key=?', (key,))
    legacy('before', '안녕', '안녕하세요', '2026-01-01T00:00:00+00:00')
    legacy('source', '나는 서울에 살아.', '알겠어요', '2026-01-02T00:00:00+00:00')
    legacy('echo', '날씨 어때?', '서울은 맑아요', '2026-01-03T00:00:00+00:00')
    store.record_turn('tracked', '고마워', '천만에요', created_at='2026-01-04T00:00:00+00:00')
    source = store.record_turn('source', '나는 서울에 살아.', '알겠어요')['user_message']
    mid = store.confirm([{'statement': '사용자는 서울에 산다.', 'source_ids': [source]}], store.epoch())[0]
    preview = store.forget([mid], dry_run=True)
    assert preview['untracked_turns'] == 2 and preview['turns'] == 1 and preview['reply_turns'] == 1 and '서울' in store.context()
    result = store.forget([mid])
    assert result['untracked_turns'] == 2 and result['message_ids'] == preview['message_ids']
    context = store.context()
    assert '서울' not in context and '안녕하세요' in context and '천만에요' in context and '날씨 어때?' in context


def test_forgetting_keeps_memories_confirmed_in_later_dependent_turns(tmp_path):
    # Forgetting one fact must not take a later, unrelated correction with it (docs/troubleshooting.md).
    store = Store(tmp_path / 'memory.db')
    cat_turn = store.record_turn('cat', '친구 수아는 고양이를 키워.', '기억할게요.')
    cat = store.confirm([{'statement': '수아는 고양이를 키운다.', 'source_ids': [cat_turn['user_message']]}], store.epoch())[0]
    _, dependencies = store.context_snapshot()
    move_turn = store.record_turn('move', '나 부산으로 이사했어.', '수아 고양이도 잘 지내죠?', context_ids=dependencies)
    move = store.confirm([{'statement': '사용자는 부산에 산다.', 'source_ids': [move_turn['user_message']]}], store.epoch())[0]
    scope = store.forget([cat])
    assert scope['memory_ids'] == [cat] and scope['turns'] == 1 and scope['reply_turns'] == 1
    assert [m['id'] for m in store.visible_memories()] == [move]
    assert store.context() == 'user: 나 부산으로 이사했어.'
    # Forgetting the correction later still works: its turn's hidden reply stays in the first batch.
    second = store.forget([move])['batch']
    assert store.visible_memories() == [] and store.context() == '(없음)'
    # Restoring the first batch brings back the cat turn, not the reply of a turn the second batch holds.
    store.restore(1)
    assert store.context() == 'user: 친구 수아는 고양이를 키워.\nassistant: 기억할게요.'
    store.restore(second)
    assert '수아 고양이도 잘 지내죠?' in store.context()


def test_reopen_renames_graph_library_labels(tmp_path):
    store = Store(tmp_path / 'memory.db')
    with store.connect() as conn:
        conn.execute("INSERT INTO llm_calls(purpose,model,ms) VALUES('graphiti','haiku',1),('memory_engine','haiku',1)")
        conn.execute("INSERT INTO relation_observations(relation,memory_id,judgement,observed_at) VALUES('DRIVES',1,'graphiti-extracted','2026-10-05')")
    store = Store(tmp_path / 'memory.db')
    with store.connect() as conn:
        assert {r[0] for r in conn.execute('SELECT purpose FROM llm_calls')} == {'graph_judge'}
        assert conn.execute('SELECT judgement FROM relation_observations').fetchone()[0] == 'engine-extracted'
