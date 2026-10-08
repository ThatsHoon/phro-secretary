"""Manual scale bench: does the memory layer stay incremental at thousands of memories?

Grows one synthetic DB (real SQLite graph + Ollama, no Claude) to --target confirmed memories with verified relations,
then measures at that size: build throughput of the added batch, retrieval latency and recall, forget / restore /
add as incremental graph updates, and the SQLite-side reads the UI polls. Re-run with a larger --target on the
same --db to grow it; each run appends one JSON line to --out.

    .venv/Scripts/python.exe tests/scale_bench.py --db C:/tmp/scale/memory.db --target 1000
    .venv/Scripts/python.exe tests/scale_bench.py --db C:/tmp/scale/memory.db --cleanup
"""
import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SURNAMES = list('김이박최정강조윤장임한오서신권황안송류홍전고문양손배백허유남심노하곽성차주우구민진나지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용')
GIVEN = ['민준', '서연', '도윤', '하은', '시우', '지아', '주원', '서윤', '예준', '하린', '지호', '수아', '건우', '지우', '현우',
         '채원', '유준', '다은', '정우', '예린', '승현', '소율', '준서', '윤서', '태오', '나래', '은호', '가온', '로아', '이안']
CITIES = ['서울', '부산', '대구', '인천', '광주', '대전', '울산', '수원', '창원', '청주', '전주', '제주', '춘천', '포항', '여수']
FACTS = [  # relation, object pool, sentence template, question template
    ('LIKES', ['커피', '녹차', '홍차', '콜라', '우유', '맥주', '와인', '주스'], '{p}은 {o}를 좋아한다.', '{p}이 좋아하는 음료는?'),
    ('OWNS', ['고양이', '강아지', '자전거', '카메라', '피아노', '오토바이', '앵무새'], '{p}은 {o}를 가지고 있다.', '{p}이 가진 건 뭐야?'),
    ('PLAYS', ['테니스', '축구', '농구', '기타', '바둑', '체스', '배드민턴'], '{p}은 {o}를 한다.', '{p}의 취미는?'),
    ('WORKS_AT', ['카카오', '네이버', '삼성', '현대', '토스', '쿠팡', '배민', 'LG'], '{p}은 {o}에 다닌다.', '{p}은 어디 다녀?'),
    ('STUDIES', ['일본어', '중국어', '스페인어', '독일어', '프랑스어', '파이썬', '통계학'], '{p}은 {o}를 공부한다.', '{p}이 공부하는 건?'),
]


def people():
    return [s + g for s in SURNAMES for g in GIVEN]


def fact(i, names):
    """Deterministic i-th memory: mostly distinct facts, every 7th a move (contradiction), every 11th a re-wording."""
    person = names[i % len(names)]
    if i % 7 == 3:
        city = CITIES[(i // len(names) + i) % len(CITIES)]
        return (f'{person}은 {city}에 살고 있다.', f'{person}은 어디 살아?',
                [{'subject': person, 'subject_type': 'Person', 'relation': 'LIVES_IN', 'object': city, 'object_type': 'Place'}])
    relation, objects, sentence, question = FACTS[(i // len(names) + i) % len(FACTS)]
    obj = objects[(i * 31 + i // len(names)) % len(objects)]
    text = sentence.format(p=person, o=obj)
    if i % 11 == 5:
        text = f'{person}에 대해: ' + text
    return text, question.format(p=person), [{'subject': person, 'subject_type': 'Person', 'relation': relation,
                                               'object': obj, 'object_type': 'Thing'}]


def question(memory):
    """The question about a stored memory, from its own relation (ids are not positions: AUTOINCREMENT skips)."""
    r = memory['relations'][0]
    if r['relation'] == 'LIVES_IN':
        return f"{r['subject']}은 어디 살아?"
    return next(q for rel, _, _, q in FACTS if rel == r['relation']).format(p=r['subject'])


def seed(store, start, stop, names):
    from memory.store import utcnow
    rows = []
    for i in range(start, stop):
        statement, _, relations = fact(i, names)
        rows.append((statement, '사용자', 'knowledge', 5, 'high', 'user', utcnow(), utcnow(),
                     json.dumps(relations, ensure_ascii=False)))
    with store.connect() as conn:
        conn.executemany('INSERT INTO memories(statement,holder,kind,importance,certainty,source_type,valid_from,'
                         'created_at,relations) VALUES(?,?,?,?,?,?,?,?,?)', rows)
        conn.execute('UPDATE runtime SET revision=revision+1 WHERE id=1')


def timed(fn):
    started = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - started


def pct(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 3)


def graph_size(graph, group):
    import os
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(graph.path)) as conn:
        nodes = conn.execute('SELECT count(*) FROM entities WHERE graph=?', (group,)).fetchone()[0]
        edges = conn.execute('SELECT count(*) FROM edges WHERE graph=?', (group,)).fetchone()[0]
    return {'nodes': nodes, 'edges': edges, 'graph_file_mb': round(os.path.getsize(graph.path) / 2**20, 1)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--target', type=int)
    parser.add_argument('--queries', type=int, default=40)
    parser.add_argument('--out', default=str(ROOT / 'tests' / 'scale_bench.jsonl'))
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    import server
    from memory.graph import Graph
    from memory.service import MemoryService
    from memory.store import Store
    server.start_ollama(11434)
    store = Store(args.db)
    usage = []
    graph = Graph(store.path, audit=lambda purpose, model, ms, **k: usage.append(k.get('usage') or {}))
    service = MemoryService(store, graph)
    try:
        if args.cleanup:
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(graph.path + suffix):
                    os.remove(graph.path + suffix)
            print('deleted graph file of', store.path)
            return
        names = people()
        with store.connect() as conn:
            have = conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        report = {'target': args.target, 'started': time.strftime('%Y-%m-%d %H:%M:%S')}
        # Build: only the added batch is ingested (incremental), even on a large existing graph.
        if args.target > have:
            seed(store, have, args.target, names)
            usage.clear()
            step, seconds = timed(service.tick)
            report['build'] = {'added': args.target - have, 'seconds': round(seconds, 1),
                               'per_memory_s': round(seconds / (args.target - have), 3), 'rebuild': step.get('rebuild'),
                               'chat_tokens': sum(u.get('input_tokens', 0) + u.get('output_tokens', 0) for u in usage),
                               'embed_tokens': sum(u.get('embed_tokens', 0) for u in usage)}
            print('build', report['build'], flush=True)
        state = store.graph_state()
        report['graph'] = graph_size(graph, state['graph_group'])
        report['coverage'] = service.health()['coverage']
        # Retrieval: latency and whether the asked-about memory reaches the prompt block.
        rng = random.Random(args.target)
        visible = {m['id']: m for m in store.visible_memories()}
        hits, latencies = 0, []
        for mid in rng.sample(sorted(visible), args.queries):
            result, seconds = timed(lambda: service.retrieve(question(visible[mid])))
            latencies.append(seconds)
            hits += any(m['id'] == mid for m in result['memories'])
            assert not result['degraded'], result
        report['retrieve'] = {'p50_s': pct(latencies, .5), 'p95_s': pct(latencies, .95),
                              'recall_in_prompt': round(hits / args.queries, 2)}
        # Incremental maintenance on the large graph.
        forget_one = rng.choice(sorted(visible))
        batch = store.forget([forget_one])['batch']
        step, report['forget_1_s'] = timed(service.tick)
        assert not step['rebuild'] and step['removed'] == 1, step
        store.restore(batch)
        step, report['restore_1_s'] = timed(service.tick)
        assert not step['rebuild'] and step['added'] == 1, step
        ten = rng.sample(sorted(visible), 10)
        batch = store.forget(ten)['batch']
        step, report['forget_10_s'] = timed(service.tick)
        assert not step['rebuild'] and step['removed'] == 10, step
        store.restore(batch)
        step, report['restore_10_s'] = timed(service.tick)
        seed(store, args.target, args.target + 1, names)
        step, report['add_1_s'] = timed(service.tick)
        assert not step['rebuild'] and step['added'] == 1, step
        with store.connect() as conn:  # keep the DB at --target for the next, larger run
            conn.execute('DELETE FROM memories WHERE id=(SELECT MAX(id) FROM memories)')
            conn.execute('UPDATE runtime SET revision=revision+1 WHERE id=1')
        service.tick()
        # SQLite reads behind /health and /graph.
        _, report['health_s'] = timed(service.health)
        _, report['visible_memories_s'] = timed(store.visible_memories)
        for key in ('forget_1_s', 'restore_1_s', 'forget_10_s', 'restore_10_s', 'add_1_s', 'health_s', 'visible_memories_s'):
            report[key] = round(report[key], 3)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        with open(args.out, 'a', encoding='utf-8') as out:
            out.write(json.dumps(report, ensure_ascii=False) + '\n')
    finally:
        service.close()


if __name__ == '__main__':
    main()
