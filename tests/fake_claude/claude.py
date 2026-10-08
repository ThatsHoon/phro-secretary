"""Stand-in for the `claude` CLI in desktop E2E runs (tests/e2e_desktop.mjs): no account, no cost.

Answers by system prompt. Markers in the user text drive edge cases: SLOW waits (cancel race), FAIL exits 1.
"""
import base64
import json
import os
import re
import sys
import time

# The E2E compiles a small claude.exe stub (a .cmd would mangle multi-line arguments) that forwards
# its parsed arguments here base64-encoded, one per line.
args = ([base64.b64decode(a).decode('utf-8') for a in os.environ['FAKE_CLAUDE_ARGS'].split('\n')]
        if os.environ.get('FAKE_CLAUDE_ARGS') else sys.argv[1:])
sys.stdout.reconfigure(encoding='utf-8')
prompt = args[args.index('-p') + 1]
system = args[args.index('--system-prompt') + 1]


def reply(text):
    print(json.dumps({'result': text, 'usage': {'input_tokens': 1, 'output_tokens': 1}}, ensure_ascii=False))


if system.startswith('너는 사용자의 개인 AI 비서'):
    user = prompt.rsplit('[사용자]\n', 1)[-1]
    if 'FAIL' in user:
        sys.exit(1)
    if 'SLOW' in user:
        time.sleep(30)
    reply(f'대역 답변: {user[:40]} [e:happy]')
elif system.startswith('Extract only durable'):
    messages = json.loads(prompt)
    claims = []
    for m in messages:
        place = re.search(r'나는 (\S+)에 살아', m['text']) if m['role'] == 'user' else None
        if place:
            claims.append({'statement': f'사용자는 {place[1]}에 살고 있다.', 'holder': '사용자', 'kind': 'profile',
                           'importance': 6, 'certainty': 'high', 'source_ids': [m['id']],
                           'relations': [{'subject': '사용자', 'subject_type': 'Person', 'relation': 'LIVES_IN',
                                          'object': place[1], 'object_type': 'Place'}]})
    reply(json.dumps({'claims': claims}, ensure_ascii=False))
elif system.startswith('Independently verify'):
    count = prompt.split('PROPOSALS:\n', 1)[1]
    reply(json.dumps({'verdicts': [{'index': i, 'verdict': 'accept'} for i in range(len(json.loads(count)))]}))
else:
    reply(json.dumps({'summary': '대역 요약'}, ensure_ascii=False))
