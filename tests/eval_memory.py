"""Manual retrieval eval on realistic memories: what a turn's prompt block contains for conversational questions.

One persona (the user, 민준) with memories as extraction stores them (statement, triples or [] when no endpoint
pair exists, dates), mixed into 300 other people's facts. Questions cover direct recall, follow-ups that lean on the
previous turn, memories without relations, aliases, dated plans and corrected facts. The real embedding model runs;
no Claude call. Each question is recorded as a turn after it is asked, as in a conversation, so the previous turn is
the context of the next.

    .venv/Scripts/python.exe tests/eval_memory.py [--out results.jsonl] [--verbose]
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import json
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory.graph import Graph
from memory.service import MemoryService
from memory.store import Store, utcnow
from scale_bench import fact, people

NOW = datetime.now(timezone.utc)
DAY = lambda days: (NOW + timedelta(days=days)).strftime('%Y-%m-%d')
STAMP = lambda days: (NOW + timedelta(days=days)).isoformat()


def rel(subject, relation, obj, subject_type='Person', object_type='Thing'):
    return {'subject': subject, 'subject_type': subject_type, 'relation': relation, 'object': obj, 'object_type': object_type}


# key: (statement, relations, valid_from offset in days, expires_at offset in days or None)
MEMORIES = {
    'home_old': ('사용자는 서울에 살고 있다.', [rel('사용자', 'LIVES_IN', '서울', object_type='Place')], -400, None),
    'home': ('사용자는 부산으로 이사해서 부산에 살고 있다.', [rel('사용자', 'LIVES_IN', '부산', object_type='Place')], -60, None),
    'name': ('사용자의 이름은 민준이다.', [rel('사용자', 'HAS_NAME', '민준')], -300, None),
    'job': ('사용자는 카카오에 다닌다.', [rel('사용자', 'WORKS_AT', '카카오', object_type='Organization')], -200, None),
    'coffee': ('사용자는 아이스 아메리카노를 좋아한다.', [rel('사용자', 'LIKES', '아이스 아메리카노')], -100, None),
    'sua': ('사용자의 친구 수아는 고양이를 키운다.', [rel('사용자', 'FRIEND_OF', '수아', object_type='Person'),
                                             rel('수아', 'OWNS', '고양이')], -90, None),
    'sua_cat': ('수아의 고양이 이름은 나비다.', [rel('수아', 'OWNS', '나비')], -80, None),
    'mom': ('사용자의 어머니 이름은 김영희다.', [rel('사용자', 'FAMILY_OF', '김영희', object_type='Person')], -150, None),
    'mom_job': ('김영희는 초등학교 교사다.', [rel('김영희', 'HAS_JOB', '초등학교 교사')], -150, None),
    'tired': ('사용자는 요즘 야근이 많아서 많이 지쳐 있다.', [], -3, None),
    'exercise': ('사용자는 다시 운동을 시작하기로 결심했다.', [], -10, None),
    'allergy': ('사용자는 땅콩 알레르기가 있다.', [], -120, None),
    'presentation': (f'사용자는 {DAY(6)} 팀 발표를 앞두고 걱정하고 있다.', [], -2, 7),
    'trip_past': (f'사용자는 {DAY(-20)}에 오사카로 여행을 간다.', [rel('사용자', 'PLANS_TO_VISIT', '오사카', object_type='Place')], -50, -19),
    'trip_future': (f'사용자는 {DAY(25)}에 제주도로 여행을 간다.', [rel('사용자', 'PLANS_TO_VISIT', '제주도', object_type='Place')], -5, 26),
    'dentist': (f'사용자는 {DAY(4)}에 치과 예약이 있다.', [], -7, 5),
}

# (category, history: user messages said just before, question, expected memory keys)
CASES = [
    ('direct', [], '나 어디 살아?', ['home']),
    ('direct', [], '내가 다니는 회사가 어디였지?', ['job']),
    ('direct', [], '내가 좋아하는 음료 뭐였지?', ['coffee']),
    ('direct', [], '수아는 뭐 키워?', ['sua']),
    ('direct', [], '김영희 직업이 뭐야?', ['mom_job']),
    ('followup', ['내 친구 수아 기억나?'], '걔 고양이 이름 뭐였지?', ['sua_cat']),
    ('followup', ['제주도 여행 계획 있잖아.'], '그거 언제 가는 거였지?', ['trip_future']),
    ('followup', ['우리 엄마 김영희 알지?'], '그분 무슨 일 하셨지?', ['mom_job']),
    ('no_relation', [], '나 요즘 왜 이렇게 피곤하지?', ['tired']),
    ('no_relation', [], '다음 주에 내가 걱정하던 일 있었나?', ['presentation']),
    ('no_relation', [], '나 못 먹는 음식 있어?', ['allergy']),
    ('no_relation', [], '내가 새로 시작하기로 한 거 뭐였지?', ['exercise']),
    ('alias', [], '우리 엄마 이름이 뭐지?', ['mom']),
    ('alias', [], '어머니는 무슨 일 하셔?', ['mom_job']),
    ('temporal', [], '오사카 여행 언제 갔었지?', ['trip_past']),
    ('temporal', [], '다가오는 여행 일정 있어?', ['trip_future']),
    ('temporal', [], '치과 예약 언제야?', ['dentist']),
    ('correction', [], '나 서울 살지?', ['home']),
]
# Memories that must never appear as current facts.
STALE = {'home_old'}


def seed(store, names):
    rows, keys = [], {}
    for i in range(300):
        statement, _, relations = fact(i, names)
        rows.append((statement, None, STAMP(-500 + i), None, relations))
    for key, (statement, relations, start, end) in MEMORIES.items():
        keys[len(rows)] = key
        rows.append((statement, key, STAMP(start), None if end is None else STAMP(end), relations))
    ids = {}
    with store.connect() as conn:
        for statement, key, start, end, relations in rows:
            mid = conn.execute('INSERT INTO memories(statement,holder,kind,importance,certainty,source_type,valid_from,'
                               'expires_at,created_at,relations) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (statement, '사용자', 'knowledge', 5, 'high', 'user', start, end, utcnow(),
                                json.dumps(relations, ensure_ascii=False))).lastrowid
            if key:
                ids[key] = mid
        conn.execute('UPDATE runtime SET revision=revision+1 WHERE id=1')
    return ids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='phro-eval-') as folder:
        store = Store(Path(folder) / 'memory.db')
        service = MemoryService(store, Graph(store.path))
        ids = seed(store, people())
        service.tick()
        stale = {ids[k] for k in STALE}
        # Memories whose end date has passed must be shown as past, never as upcoming.
        past = {ids[k] for k, (_, _, _, end) in MEMORIES.items() if end is not None and end < 0}
        results, turn = [], 0
        for category, history, question, expected in CASES:
            for said in history:
                turn += 1
                store.record_turn(f'h{turn}', said, '네, 기억하고 있어요.')
            started = time.perf_counter()
            result = service.retrieve(question)
            seconds = time.perf_counter() - started
            got = {m['id'] for m in result['memories']}
            hit = all(ids[k] in got for k in expected)
            leaked = sorted(got & stale)
            lines = result['prompt_block'].splitlines()
            unmarked = [m for m in got & past if not any(l.startswith(f'[m:{m}] (지난 일') for l in lines)]
            leaked += unmarked
            results.append({'category': category, 'question': question, 'hit': hit, 'stale': bool(leaked),
                            'seconds': round(seconds, 3)})
            if args.verbose or not hit or leaked:
                print(('HIT ' if hit else 'MISS') + (' STALE' if leaked else ''), category, question)
                if args.verbose or not hit:
                    print('   ', result['prompt_block'].replace('\n', '\n    ')[:600])
            turn += 1
            store.record_turn(f'q{turn}', question, '확인해 볼게요.')
        service.close()
    summary = {}
    for r in results:
        s = summary.setdefault(r['category'], {'hit': 0, 'n': 0, 'stale': 0})
        s['n'] += 1
        s['hit'] += r['hit']
        s['stale'] += r['stale']
    total = sum(r['hit'] for r in results)
    print(f'TOTAL {total}/{len(results)}', ' '.join(f"{k} {v['hit']}/{v['n']}" for k, v in summary.items()),
          'stale', sum(r['stale'] for r in results))
    if args.out:
        with open(args.out, 'a', encoding='utf-8') as out:
            out.write(json.dumps({'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'total': total, 'n': len(results),
                                  'summary': summary, 'results': results}, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
