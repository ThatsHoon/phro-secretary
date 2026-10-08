"""Manual bench: where a conversation turn spends tokens and time, and what the second retrieval costs.

Runs the real server on a synthetic DB with the real SQLite graph + embedding model and the stand-in `claude` (no account cost),
seeds N confirmed memories with relations, waits for the graph, then drives turns the way the client does
(/retrieve -> /respond -> /commit). Reads llm_calls back and prints per-purpose and per-turn totals.
Claude token counts from the stand-in are placeholders; real ones come from tests/scenario_full.py.

    .venv/Scripts/python.exe tests/token_bench.py --memories 20 --turns 10
Graphs this DB created are deleted at the end.
"""
import argparse
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PEOPLE = ['지연', '민호', '수아', '태오', '하린', '도윤', '서진', '유나', '건우', '나래']
THINGS = [('LIKES', '커피'), ('LIKES', '녹차'), ('OWNS', '고양이'), ('OWNS', '자전거'), ('PLAYS', '테니스'),
          ('PLAYS', '기타'), ('WORKS_AT', '카카오'), ('LIVES_IN', '부산'), ('LIVES_IN', '대전'), ('STUDIES', '일본어')]
VERB = {'LIKES': '좋아한다', 'OWNS': '가지고 있다', 'PLAYS': '친다', 'WORKS_AT': '다닌다', 'LIVES_IN': '산다',
        'STUDIES': '공부한다'}
QUESTIONS = ['지연이 좋아하는 게 뭐였지?', '민호는 어디 살아?', '수아가 키우는 동물 기억나?', '태오 취미가 뭐야?',
             '하린은 어디 다녀?', '도윤이 공부하는 언어는?', '서진이랑 테니스 얘기 했었나?', '유나는 뭘 가지고 있어?',
             '건우가 좋아하는 음료는?', '나래는 어디 살더라?']


def post(port, path, body):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', json.dumps(body).encode(),
                                 {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def get(port, path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=60) as r:
        return json.loads(r.read())


def seed(db, count):
    from memory.store import Store
    store = Store(db)
    rows = []
    for i in range(count):
        person = PEOPLE[i % len(PEOPLE)] + ('' if i < len(PEOPLE) * len(THINGS) else str(i))
        relation, thing = THINGS[(i // len(PEOPLE) + i) % len(THINGS)]
        statement = f'사용자의 친구 {person}은 {thing}{"에" if relation in ("LIVES_IN", "WORKS_AT") else "을"} {VERB[relation]}.'
        rows.append((statement, '사용자', 'knowledge', 5, 'high', 'user', '2026-10-01', '2026-10-01',
                     json.dumps([{'subject': person, 'subject_type': 'Person', 'relation': relation,
                                  'object': thing, 'object_type': 'Thing'}], ensure_ascii=False)))
    with store.connect() as conn:
        conn.executemany('INSERT INTO memories(statement,holder,kind,importance,certainty,source_type,valid_from,'
                         'created_at,relations) VALUES(?,?,?,?,?,?,?,?,?)', rows)
        conn.execute('UPDATE runtime SET revision=revision+1,rebuild=1 WHERE id=1')
    return store.path


def stub(bin_dir):
    bin_dir.mkdir()
    subprocess.run(['powershell', '-NoProfile', '-Command',
                    f"Add-Type -TypeDefinition (Get-Content -Raw '{ROOT / 'tests' / 'fake_claude' / 'stub.cs'}') "
                    f"-OutputAssembly '{bin_dir / 'claude.exe'}' -OutputType ConsoleApplication"], check=True)


def report(db, seeded_at, turn_ids):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute('SELECT * FROM llm_calls ORDER BY id')]
    conn.close()
    def total(rs):
        return {'calls': len(rs), 'ms': sum(r['ms'] or 0 for r in rs),
                'chat_in': sum(r['input_tokens'] or 0 for r in rs), 'chat_out': sum(r['output_tokens'] or 0 for r in rs),
                'embed': sum(r['embed_tokens'] or 0 for r in rs), 'requests': sum(r['requests'] or 0 for r in rs)}
    purposes = {}
    for r in rows:
        purposes.setdefault(r['purpose'], []).append(r)
    by_purpose = {p: total(rs) for p, rs in purposes.items()}
    turns = []
    for tid in turn_ids:
        rs = [r for r in rows if r['turn_id'] == tid]
        pick = lambda p: [r for r in rs if r['purpose'] == p]
        turns.append({'client_retrieve_ms': total(pick('retrieve_client'))['ms'],
                      'server_retrieve_ms': total(pick('retrieve_respond'))['ms'],
                      'graph_search': total(pick('graph_search')), 'respond_ms': total(pick('respond'))['ms']})
    return {'seed_projection_s': seeded_at, 'by_purpose': by_purpose, 'turns': turns}


def cleanup_graphs(db):
    from memory.graph import graph_path
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(graph_path(db) + suffix):
            os.remove(graph_path(db) + suffix)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--memories', type=int, default=20)
    parser.add_argument('--turns', type=int, default=10)
    parser.add_argument('--port', type=int, default=8790)
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix='phro-tokens-'))
    db = seed(tmp / 'memory.db', args.memories)
    stub(tmp / 'bin')
    env = {**os.environ, 'PATH': str(tmp / 'bin') + os.pathsep + os.environ['PATH'], 'PHRO_MEMORY_DB': db,
           'PHRO_DESKTOP_PORT': str(args.port), 'PHRO_TOKEN_DEBUG': '1', 'FAKE_CLAUDE_PY': sys.executable,
           'FAKE_CLAUDE_SCRIPT': str(ROOT / 'tests' / 'fake_claude' / 'claude.py')}
    log = open(tmp / 'server.log', 'w', encoding='utf-8')
    server = subprocess.Popen([sys.executable, str(ROOT / 'desktop' / 'serve.py')], cwd=ROOT, env=env,
                              stdout=log, stderr=subprocess.STDOUT)
    turn_ids = []
    try:
        started = time.monotonic()
        while True:
            time.sleep(2)
            if server.poll() is not None:
                raise RuntimeError('server exited: ' + (tmp / 'server.log').read_text(encoding='utf-8')[-2000:])
            try:
                memory = get(args.port, '/health')['memory']
            except OSError:
                continue
            if memory['ready'] and sum(memory['coverage'].values()) == args.memories:
                break
            if time.monotonic() - started > 60 * 60:
                raise TimeoutError(json.dumps(memory))
        seeded = round(time.monotonic() - started, 1)
        print('projection ready', seeded, 's', memory['coverage'], flush=True)
        for i in range(args.turns):
            tid = str(uuid.uuid4())
            text = QUESTIONS[i % len(QUESTIONS)]
            got = post(args.port, '/retrieve', {'turn_id': tid, 'text': text})
            said = post(args.port, '/respond', {'turn_id': tid, 'text': text, 'prompt_block': got['prompt_block'],
                                                'memory_epoch': got['memory_epoch']})
            post(args.port, '/commit', {'turn_id': tid, 'user_text': text, 'reply': said['reply'], 'cited': said['cited'],
                                        'memory_epoch': said['memory_epoch'], 'context_ids': said['context_ids']})
            turn_ids.append(tid)
        result = report(db, seeded, turn_ids)
        print(json.dumps(result, ensure_ascii=False, indent=1))
    finally:
        server.terminate()
        server.wait(20)
        log.close()
        try:
            cleanup_graphs(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
