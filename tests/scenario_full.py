"""Manual end-to-end scenario that exercises every phro-secretary workflow with one coherent user (real Claude account).

One persona (민준) talks for ~16 turns: profile, other people's facts, a negation, an ownership trap, a hypothetical,
a dated plan, recall, a correction, small talk until the rolling summary folds, a cancelled turn, then memory
management (preview, archive, restore, purge), relation backfill for a migrated memory and a service restart.
Every step records what each workflow did: retrieval block, reply/emotion/citations, the model's extraction
proposals and verdicts, confirmed memories with relations, graph facts, summary and audit tokens.

    .venv/Scripts/python.exe tests/scenario_full.py    # report: %TEMP%/phro_scenario_full.json
Costs roughly 40-50 Claude calls (respond: sonnet, extraction/evaluation: sonnet, summary: haiku).
Only a temporary DB and the graphs it owns are touched.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import os
import tempfile
import threading
import time
import traceback
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server as api
from memory.graph import Graph
from memory.service import MemoryService
from memory.store import Store, utcnow

EMOTION_MOTION = {'happy': 'jumping', 'laugh': 'jumping', 'surprised': 'jumping', 'sad': 'failed', 'angry': 'failed',
                  'embarrassed': 'waiting', 'thinking': 'review', 'neutral': 'waving'}  # desktop/sprite.js

TURNS = [  # (name, user text, what it exercises)
    ('greeting', '안녕! 오늘 처음 써 보네.', '인사: 기억으로 저장하지 않음'),
    ('profile', '내 이름은 민준이고 서울 성수동에 살아. 회사는 토스에 다녀.', '프로필: 한 턴에서 여러 사실과 관계'),
    ('others', '동료 지연은 커피를 엄청 좋아하는데, 나는 커피를 안 마셔.', '타인 사실과 사용자 부정 구분'),
    ('ownership', '내 친구 수아는 고양이 두 마리를 키워.', '소유 주체: 고양이 주인은 수아'),
    ('hypothetical', '만약 내가 고양이를 키운다면 이름을 뭐로 지을까?', '가정: 사실로 저장하지 않음'),
    ('plan', '다음 달 15일에 오사카로 여행 가기로 했어.', '계획: 날짜 있는 결정'),
    ('recall', '내가 어디 살고 어디 다닌다고 했지?', '인출: 그래프 검색과 인용'),
    ('attribution', '고양이 키우는 사람이 나야, 수아야?', '귀속 확인: 타인 사실 인출'),
    ('correction', '아 정정할게. 지난주에 부산으로 이사했어. 이제 서울에 안 살아.', '정정: 이전 거주지 무효화'),
    ('current', '요즘 나 어디 산다고?', '정정 후 현재 사실 인출'),
    ('guess', '내 직업이 뭔지 맞혀 봐.', '답변의 추측은 기억으로 저장하지 않음'),
    ('job', '참, 회사도 옮겼어. 이제 카카오 다녀.', '이직: 단일값 관계(WORKS_AT) 규칙으로 이전 회사 무효화'),
    ('smalltalk1', '부산은 바다가 가까워서 좋더라.', '잡담'),
    ('smalltalk2', '주말에는 보통 집에서 쉬어.', '잡담: 요약 대상 대화 누적'),
    ('trip_question', '오사카 여행 준비물 뭐 챙기면 좋을까?', '계획 기억을 근거로 한 답변'),
]


def facts(graph, group):
    import sqlite3
    from contextlib import closing
    if not group:
        return []
    with closing(sqlite3.connect(graph.path)) as conn:
        rows = conn.execute('SELECT s.name, e.name, t.name, e.invalid_at IS NULL FROM edges e JOIN entities s'
                            ' ON s.uuid=e.source JOIN entities t ON t.uuid=e.target WHERE e.graph=?', (group,)).fetchall()
    return sorted(f"{s} -{r}-> {o}{'' if v else ' (무효)'}" for s, r, o, v in rows)


def main():
    api.start_ollama(11434)
    output = Path(os.getenv('PHRO_SCENARIO_REPORT', str(Path(tempfile.gettempdir()) / 'phro_scenario_full.json')))
    report = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'real_claude': True, 'steps': [], 'checks': []}
    temporary = tempfile.TemporaryDirectory(prefix='phro-full-')
    store = Store(Path(temporary.name) / 'memory.db')
    graph = Graph(store.path, audit=api.log_call, llm=api.graph_model)
    model_log = []

    def recorded_model(purpose, model, system, prompt):
        result = api.memory_model(purpose, model, system, prompt)
        model_log.append({'purpose': purpose, 'result': {k: v for k, v in result.items() if k != '_ms'}})
        return result
    service = MemoryService(store, graph, recorded_model)
    api.MEMORY = service
    server = api.SERVER_CLASS(('127.0.0.1', 0), api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')

    def call(path, body=None):
        url = 'http://127.0.0.1:' + str(server.server_address[1]) + path
        request = Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}) if body is not None else url
        try:
            with urlopen(request, timeout=310) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(exc.read().decode()) from exc

    def check(name, passed, detail=None):
        report['checks'].append({'name': name, 'passed': bool(passed), 'detail': detail})
        print('CHECK', 'PASS' if passed else 'FAIL', name, flush=True)
        save()

    def settled():
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            state = service.health()
            if state['jobs'].get('dead') or state['graph_attempts'] >= 5:
                with store.connect() as conn:
                    dead = [dict(r) for r in conn.execute("SELECT turn_key,error,attempts FROM turns WHERE status='dead'")]
                raise RuntimeError('worker failed: ' + json.dumps({'state': state, 'dead': dead, 'last_model_calls': model_log[-4:]}, ensure_ascii=False))
            if (not state['jobs'].get('pending') and not state['jobs'].get('running') and state['ready']
                    and state['revision'] == state['graph_revision']):
                return state
            time.sleep(1)
        raise TimeoutError('memory worker did not settle: ' + json.dumps(service.health()))

    def snapshot():
        state = store.graph_state()
        with store.connect() as conn:
            summary = conn.execute('SELECT text, upto_message_id FROM summaries').fetchone()
        return {'memories': [{'id': m['id'], 'statement': m['statement'], 'kind': m['kind'], 'relations': m['relations'],
                              'graph_status': state['memory_status'].get(m['id'], 'pending')} for m in store.visible_memories()],
                'graph': facts(graph, state['graph_group']), 'summary': dict(summary) if summary else None}

    def record(name, purpose, **data):
        before = len(model_log)
        item = {'name': name, 'purpose': purpose, **data}
        item['model_calls'] = model_log[before:]
        report['steps'].append(item)
        save()
        return item

    def turn(name, text, purpose):
        print('TURN', name, text, flush=True)
        tid = 'full-' + name
        before = len(model_log)
        retrieved = call('/retrieve', {'turn_id': tid, 'text': text})
        response = call('/respond', {'turn_id': tid, 'text': text, 'prompt_block': retrieved['prompt_block'],
                                      'memory_epoch': retrieved['memory_epoch']})
        call('/commit', {'turn_id': tid, 'user_text': text, 'reply': response['reply'], 'cited': response['cited'],
                         'memory_epoch': response['memory_epoch'], 'context_ids': response['context_ids']})
        # The client checks for a due summary after each turn (desktop/pipeline.js runCheckpoint).
        due = call('/checkpoint_due', {})
        summarized = call('/summarize', {}) if due['summarize_due'] else None
        settled()
        item = {'name': name, 'purpose': purpose, 'input': text, 'prompt_block': retrieved['prompt_block'],
                'retrieval_degraded': retrieved['degraded'], 'reply': response['reply'], 'emotion': response['emotion'],
                'motion': EMOTION_MOTION.get(response['emotion'], 'waving'), 'cited': response['cited'],
                'summarized': summarized, 'model_calls': model_log[before:], **snapshot()}
        report['steps'].append(item)
        save()
        print('REPLY', response['reply'], flush=True)
        return item

    try:
        report['backend'] = graph.health()
        service.start()
        settled()
        steps = {name: turn(name, text, purpose) for name, text, purpose in TURNS}
        memories = lambda: store.visible_memories()
        joined = lambda: json.dumps(memories(), ensure_ascii=False)
        check('인사는 기억이 되지 않음', not steps['greeting']['memories'])
        check('프로필 여러 사실 확정', all(x in joined() for x in ('민준', '토스')))
        check('고양이 주인은 수아로 저장', any('수아' in m['statement'] and '고양이' in m['statement'] for m in memories()))
        check('가정은 저장되지 않음', len(steps['hypothetical']['memories']) == len(steps['ownership']['memories']))
        check('거주·회사 인출', '토스' in steps['recall']['prompt_block'])
        check('귀속 답변에 수아', '수아' in steps['attribution']['reply'])
        check('정정 후 현재 거주지 부산', '부산' in steps['current']['reply'])
        check('정정이 이전 거주지를 무효화', '사용자 -LIVES_IN-> 서울 (무효)' in steps['current']['graph']
              and '사용자 -LIVES_IN-> 부산' in steps['current']['graph'], steps['current']['graph'])
        check('사용자는 한 노드("사용자")', not any(f.startswith('민준 ') for f in steps['current']['graph']), steps['current']['graph'])
        check('요약이 한국어', any(s.get('summary') and any('가' <= ch <= '힣' for ch in s['summary']['text'][:40])
                                 for s in steps.values()))
        check('추측은 저장되지 않음', len(steps['guess']['memories']) == len(steps['current']['memories']))
        check('이직이 이전 회사를 무효화(관계 규칙)', '사용자 -WORKS_AT-> 토스 (무효)' in steps['job']['graph']
              and '사용자 -WORKS_AT-> 카카오' in steps['job']['graph'], steps['job']['graph'])
        check('요약 접힘', any(s.get('summarized') and s['summarized'].get('folded') for s in steps.values()))

        # Cancel while the reply is being generated: nothing is committed.
        with store.connect() as conn:
            before = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        text = '이번 답변은 취소할 거야. 아주 길게 부산 맛집 열 곳을 설명해 줘.'
        retrieved = call('/retrieve', {'turn_id': 'full-cancel', 'text': text})
        result = {}
        worker = threading.Thread(target=lambda: result.update(call('/respond', {'turn_id': 'full-cancel', 'text': text,
                                  'prompt_block': retrieved['prompt_block'], 'memory_epoch': retrieved['memory_epoch']})))
        worker.start()
        time.sleep(2)
        cancelled = call('/cancel', {'turn_id': 'full-cancel'})
        worker.join()
        late = call('/commit', {'turn_id': 'full-cancel', 'user_text': text, 'reply': result.get('reply', ''),
                                'memory_epoch': store.epoch()})
        with store.connect() as conn:
            after = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        record('cancel', '응답 중 취소: 커밋 없음', input=text, cancel=cancelled, respond=result, commit=late)
        check('취소된 턴은 저장되지 않음', before == after and late.get('cancelled'))

        # Memory management: preview the cascade, archive, check retrieval, restore, then purge a plan.
        cat = next(m for m in memories() if '고양이' in m['statement'])
        preview = call('/forget_preview', {'memory_ids': [cat['id']]})
        archived = call('/forget', {'memory_ids': [cat['id']], 'reason': '시나리오: 수아 정보 보관'})
        immediately = call('/retrieve', {'text': '수아는 뭘 키워?'})
        settled()
        after_archive = snapshot()
        record('archive', '보관: 전파 범위 미리보기, 즉시 인출 제외, 증분 그래프 제거', target=cat['statement'], preview=preview,
               archived=archived, retrieve_before_worker=immediately['prompt_block'], **after_archive)
        check('보관 범위는 그 사실만(이후 턴은 답변만)', preview['memory_ids'] == [cat['id']] and preview['turns'] == 1
              and preview['reply_turns'] > 0, preview)
        check('보관 직후(워커 전) 인출에서 제외', '고양이' not in immediately['prompt_block'], immediately['prompt_block'])
        check('보관 후 그래프에서 제거', not any('고양이' in f for f in after_archive['graph']))
        call('/restore', {'batch': archived['batch']})
        settled()
        restored = snapshot()
        record('restore', '복구: 해당 기억만 다시 반영', **restored)
        check('복구 후 그래프에 다시 반영', any('고양이' in f for f in restored['graph']))
        trip = next(m for m in memories() if '오사카' in m['statement'])
        archived = call('/forget', {'memory_ids': [trip['id']], 'reason': '시나리오: 여행 계획 삭제'})
        call('/purge', {'batch': archived['batch'], 'confirm': True})
        settled()
        with store.connect() as conn:
            leftover = [dict(r) for r in conn.execute("SELECT id, turn_key, role, text FROM messages WHERE text LIKE '%오사카%'")]
        purged = snapshot()
        record('purge', '영구삭제: 근거 턴 원문과 이후 답변 삭제(이후 사용자 발화는 남음)', target=trip['statement'],
               archived=archived, leftover_messages=leftover, **purged)
        # Policy: the source turn and later replies are erased; the user's own later words stay. The source turn is
        # whichever turn the memory was confirmed from (evaluation may reject the plan turn's claim and accept a later one).
        gone = set(archived['message_ids'])
        check('영구삭제 후 근거 턴과 답변 원문 없음', gone and not any(m['id'] in gone for m in leftover)
              and not any('오사카' in m['statement'] for m in purged['memories']), leftover)

        # A migrated memory without relations: coverage reports it, backfill analyses it with the same checks.
        with store.connect() as conn:
            conn.execute('INSERT INTO memories(statement,holder,kind,importance,certainty,source_type,valid_from,created_at)'
                         " VALUES('사용자는 2019년에 대학을 졸업했다.','사용자','profile',5,'high','user',?,?)", (utcnow(), utcnow()))
            conn.execute('UPDATE runtime SET revision=revision+1 WHERE id=1')
        settled()
        coverage_before = service.health()['coverage']
        before = len(model_log)
        backfill = call('/relations_backfill', {})
        settled()
        record('backfill', '관계 소급: 이관 기억의 관계를 2단계 검증으로 추가', coverage_before=coverage_before, backfill=backfill,
               coverage_after=service.health()['coverage'], **snapshot())
        report['steps'][-1]['model_calls'] = model_log[before:]
        check('관계 소급 반영', backfill['accepted'] >= 1)

        # Restart: a new service on the same DB serves the same graph without rebuilding.
        group = store.graph_state()['graph_group']
        before_restart = call('/retrieve', {'text': '나에 대해 기억하는 거 알려 줘.'})
        service.stop_event.set()
        service.worker.join(20)
        service = MemoryService(Store(store.path), graph, recorded_model)
        api.MEMORY = service
        service.start()
        settled()
        again = call('/retrieve', {'text': '나에 대해 기억하는 거 알려 줘.'})
        record('restart', '재시작: 같은 그래프로 바로 인출', same_group=store.graph_state()['graph_group'] == group,
               prompt_block=again['prompt_block'])
        ids = lambda r: sorted(m['id'] for m in r['memories'])
        check('재시작 후 같은 그래프로 같은 인출', store.graph_state()['graph_group'] == group and not again['degraded']
              and ids(again) == ids(before_restart), again['prompt_block'])
        report['health'] = call('/health')
        with store.connect() as conn:
            report['audit'] = [dict(r) for r in conn.execute(
                'SELECT purpose,model,ms,input_tokens,output_tokens,embed_tokens,cost_usd,error FROM llm_calls')]
        report['completed'] = True
    except Exception as exc:
        report['error'] = str(exc)
        report['traceback'] = traceback.format_exc()
        print(report['traceback'], flush=True)
    finally:
        service.stop_event.set()
        if service.worker:
            service.worker.join(20)
        server.shutdown()
        server.server_close()
        thread.join()
        service.close()
        report['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
        save()
        temporary.cleanup()
    return int(not report.get('completed') or any(not c['passed'] for c in report['checks']))


if __name__ == '__main__':
    sys.exit(main())
