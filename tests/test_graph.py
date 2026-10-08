"""SQLite graph (memory/graph.py) without Ollama: a bag-of-words embedding stands in for the local model."""
import hashlib

import numpy as np
import pytest

from memory.graph import Graph, GraphConfig, fuse, keyword_query


def bag_of_words(self, text, usage):
    vector = np.zeros(self.config.dimension, dtype=np.float32)
    for word in text.split():
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.config.dimension] += 1
    return vector


@pytest.fixture
def graph(tmp_path, monkeypatch):
    monkeypatch.setattr(Graph, '_embed', bag_of_words)
    return Graph(tmp_path / 'memory.db', GraphConfig())


def memory(mid, statement, subject, relation, obj, when='2026-01-01T00:00:00Z', kind='Place'):
    return {'id': mid, 'statement': statement, 'valid_from': when,
            'relations': [{'subject': subject, 'subject_type': 'Person', 'relation': relation,
                           'object': obj, 'object_type': kind}]}


def test_single_valued_relation_supersedes_and_removal_revalidates(graph):
    group = graph.new_group()
    first = graph.ingest(group, [memory(1, '민수는 서울에 산다.', '민수', 'LIVES_IN', '서울')], create=True)
    second = graph.ingest(group, [memory(2, '민수는 부산으로 이사했다.', '민수', 'LIVES_IN', '부산',
                                         '2026-02-01T00:00:00Z')])
    facts = {e['fact']: e for e in graph.search(group, '민수는 어디 살아?')}
    assert facts['민수는 서울에 산다.']['invalid_at'] == '2026-02-01T00:00:00+00:00'
    assert facts['민수는 부산으로 이사했다.']['invalid_at'] is None
    statements = {ep: '민수는 서울에 산다.' for ep in first}
    assert graph.remove(group, list(second), statements) == {'episodes': 1, 'edges_deleted': 1, 'edges_kept': 0}
    [left] = graph.search(group, '민수는 어디 살아?')
    assert (left['fact'], left['invalid_at']) == ('민수는 서울에 산다.', None)
    assert graph.edge_counts(group, list(first)) == {next(iter(first)): 1}


def test_repeated_fact_is_one_edge_and_keeps_the_remaining_wording(graph):
    group = graph.new_group()
    a = graph.ingest(group, [memory(1, '지아는 커피를 좋아한다.', '지아', 'LIKES', '커피', kind='Thing')], create=True)
    b = graph.ingest(group, [memory(2, '지아는 커피를 정말 좋아한다.', '지아', 'LIKES', '커피', kind='Thing')])
    [edge] = graph.search(group, '지아는 커피를 좋아한다.')
    assert set(edge['episodes']) == set(a) | set(b)
    graph.remove(group, list(a), {ep: '지아는 커피를 정말 좋아한다.' for ep in b})
    [edge] = graph.search(group, '지아는 커피를 좋아한다.')
    assert edge['episodes'] == list(b) and edge['fact'] == '지아는 커피를 정말 좋아한다.'


def test_relations_outside_the_vocabulary_go_to_the_judge(graph):
    prompts = []
    graph.llm = lambda system, prompt, model: prompts.append(prompt) or 'thinking [1] {"duplicate_facts":[],"contradicted_facts":[0]}'
    group = graph.new_group()
    graph.ingest(group, [memory(1, '하린은 아반떼를 몬다.', '하린', 'DRIVES', '아반떼', kind='Thing')], create=True)
    graph.ingest(group, [memory(2, '하린은 소나타를 몬다.', '하린', 'DRIVES', '소나타', '2026-03-01T00:00:00Z', 'Thing')])
    assert '아반떼' in prompts[0] and '소나타' in prompts[0]
    assert graph.take_observations() == [('DRIVES', 1, 'first'), ('DRIVES', 2, 'contradicts')]
    old = next(e for e in graph.search(group, '하린은 아반떼를 몬다.') if '아반떼' in e['fact'])
    assert old['invalid_at'] == '2026-03-01T00:00:00+00:00'


def test_memories_without_triples_get_an_empty_episode(graph):
    group = graph.new_group()
    mapping = graph.ingest(group, [{'id': 1, 'statement': '오늘 피곤하다.', 'valid_from': '2026-01-01', 'relations': None}],
                           create=True)
    assert list(mapping.values()) == [[1]] and graph.edge_counts(group, list(mapping)) == {next(iter(mapping)): 0}


def test_lifecycle_and_hostile_queries(graph):
    group = graph.new_group()
    graph.ingest(group, [memory(1, '"서울" OR * NEAR(a b)', '민수', 'LIVES_IN', '서울')], create=True)
    for text in ['"', '* OR AND NOT', 'NEAR(', '-', 'a' * 5000, ' '.join(['단어'] * 200)]:
        graph.search(group, text)
    with pytest.raises(ValueError):
        graph.ingest(group, [], create=True)
    with pytest.raises(ValueError):
        graph.search('phro_ai_000000000000_' + '0' * 32, '서울')
    graph.delete(group)
    with pytest.raises(ValueError, match='active projection missing'):
        graph.search(group, '서울')


def test_fusion_and_keyword_query():
    assert fuse(['a', 'b'], ['b', 'c']) == ['b', 'a', 'c']
    assert keyword_query('the 서울에, "살아"?') == '"서울에" OR "살아"'
    assert keyword_query('the a') is None


def test_judge_reply_without_json_fails_the_ingest(graph):
    graph.llm = lambda *a: 'no json here'
    group = graph.new_group()
    graph.ingest(group, [memory(1, '하린은 아반떼를 몬다.', '하린', 'DRIVES', '아반떼', kind='Thing')], create=True)
    with pytest.raises(ValueError, match='no JSON'):
        graph.ingest(group, [memory(2, '하린은 소나타를 몬다.', '하린', 'DRIVES', '소나타', kind='Thing')])
    assert graph.edge_counts(group, ['x']) == {}  # the failed memory left nothing behind
    assert len(graph.search(group, '하린은 소나타를 몬다.')) == 1


def test_moved_database_removes_its_old_graph_file(tmp_path, monkeypatch):
    import os
    from memory.graph import graph_path
    monkeypatch.setattr(Graph, '_embed', bag_of_words)
    old = Graph(tmp_path / 'old.db', GraphConfig())
    group = old.new_group()
    old.ingest(group, [], create=True)
    Graph(tmp_path / 'new.db', GraphConfig()).delete_previous(group, tmp_path / 'old.db')
    assert not os.path.exists(graph_path(tmp_path / 'old.db'))


def test_failed_ingest_drops_its_judgements(graph):
    replies = iter(['{"duplicate_facts":[],"contradicted_facts":[0]}'])
    graph.llm = lambda *a: next(replies)  # the second judgement raises StopIteration
    group = graph.new_group()
    first = memory(1, '하린은 아반떼를 몬다.', '하린', 'DRIVES', '아반떼', kind='Thing')
    first['relations'].append({**first['relations'][0], 'relation': 'RIDES', 'object': '킥보드'})
    graph.ingest(group, [first], create=True)
    second = memory(2, '하린은 소나타를 몬다.', '하린', 'DRIVES', '소나타', kind='Thing')
    second['relations'].append({**second['relations'][0], 'relation': 'RIDES', 'object': '자전거'})
    with pytest.raises(StopIteration):
        graph.ingest(group, [second])
    assert graph.take_observations() == [('DRIVES', 1, 'first'), ('RIDES', 1, 'first')]
