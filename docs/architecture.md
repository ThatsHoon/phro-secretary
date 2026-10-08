# phro-secretary 구조와 운영

대화·기억 기반 캐릭터 비서. 화면은 캐릭터시트 오버레이(`desktop/`), 기억 정책과 지식그래프는 `memory/`가 맡는다.
원본과 그래프 모두 SQLite 파일이고, 임베딩은 같은 프로세스의 로컬 모델(`memory/embedder.py`)이다. LLM 호출은 모두 Claude CLI다.

## 폴더

| 경로 | 책임 |
|---|---|
| `server.py` | 로컬 HTTP API(127.0.0.1). Claude CLI 호출·취소·사용량 기록(`llm_calls`), 요청 검증, 응답 초안과 커밋 |
| `memory/store.py` | SQLite 원본: 대화, 확정 기억, 출처, 작업 상태, 보관·복구·완전삭제, 관계 사전 |
| `memory/service.py` | 기억 추출→독립 검증→확정, 롤링 요약, 워커 재시도, 그래프 반영, 인출 예산 |
| `memory/graph.py` | 지식그래프(SQLite `memory.graph.db`): 관계 반영 규칙(`SINGLE`/`MULTI`/`NOT_X`), 하이브리드 검색, 그래프 수명주기 |
| `memory/embedder.py` | 임베딩: nomic-embed-text v1.5 fp16 ONNX를 onnxruntime으로 실행(평균 풀링, 최대 2048토큰). `python -m memory.embedder`가 해시 고정 다운로드 |
| `models/` | 모델 가중치(Git 제외, 설치 파일에 포함)와 출처·라이선스(`NOTICE.md`, Apache-2.0 `LICENSE`) |
| `memory/trace.py` | 백그라운드 호출을 입력(턴)에 묶는 컨텍스트 변수 |
| `desktop/` | Electron 앱: 오버레이·대화 창·기억 관리·처리 기록 화면, 공용 대화 클라이언트 `pipeline.js`, 설치 파일 빌드 |
| `forget_cli.py` | 실행 중 서버의 기억 검색·보관·복구·완전삭제 |
| `run_checks.py` | 오프라인 회귀 검사 일괄 실행 |
| `tests/` | 단위·HTTP·실제 그래프(opt-in)·E2E 테스트, 수동 벤치(`scale_bench.py`, `token_bench.py`), 실제 Claude 시나리오(`scenario_full.py`) |

```text
desktop/main.cjs (Electron)
  └─ desktop/serve.py (127.0.0.1:8771) = server.Handler + 화면 자산
       ├─ overlay.js / app.js / manage.js / trace.js
       │    └─ pipeline.js: /retrieve → /respond → /commit → /checkpoint_due
       └─ MemoryService
            ├─ Store → SQLite (원본)
            └─ Graph → SQLite memory.graph.db (파생, 재구축 가능)
                 ├─ memory/embedder.py: 임베딩만 (nomic-embed-text v1.5, 768차원, 프로세스 내)
                 └─ Claude CLI: 규칙 밖 관계 충돌 판정
```

## 턴 하나의 흐름

1. 클라이언트가 `turn_id`를 만들고 `/retrieve`로 표시용 기억과 `memory_epoch`를 받는다.
2. `/respond`는 서버에서 **다시 인출**한다. 답변 근거와 출처는 서버 인출에서만 나온다(클라이언트 블록은 표시용).
   프롬프트 = `[기억]` + `[대화]`(요약 + 최근 10개 메시지) + `[사용자]`. 답변의 `[m:ID]`는 인용, `[e:감정]`은 동작으로 뗀다.
   지침은 `server.RESPOND`: 오늘 날짜·시각, 블록의 의미, 기억 사용 원칙(관련된 것만, 없는 사실은 모른다, 늦은 시점과 지금
   대화가 우선, 날짜는 오늘 기준, 남의 사실은 그 사람에게, 민감한 기억은 관련 있을 때만, 쓴 기억만 인용).
   기억 줄은 `[m:ID] (YYYY-MM-DD부터 | 기록 YYYY-MM-DD[, ~끝]) 문장`(말한 성립일 | 말한 날), 끝난 일정·상태는
   `(지난 일, ~YYYY-MM-DD)`.
3. 서버가 응답 해시·epoch·출처를 `response_drafts`에 둔다(10분, 최대 256개).
4. `/commit`이 초안과 대조해 대화·작업·`turn_dependencies`를 한 트랜잭션으로 기록한다. 취소됐거나 epoch가 바뀐 응답은 거부.
5. 워커가 **사용자 메시지에서만** 기억을 추출(Claude)하고, 별도 검증 호출(Claude)이 accept한 주장만 확정한다.
   추출은 주장마다 `(주체, 관계, 대상)` 트리플을 함께 내고 `memories.relations`에 저장한다. 오늘 날짜(TODAY)를 받아
   `valid_from`(사실이 성립한 날, 말한 경우만)과 `expires_at`(일정·마감·한시적 상태가 끝난 다음 날)을 날짜로 내고,
   문장 속 상대 날짜는 절대 날짜로 바꾼다(`service.DATE_RULES`). 형식이 틀린 날짜는 그 날짜만 버린다.
   질문만 있는 턴(회상·가정·양자택일)은 추출을 부르지 않는다(`service.question_only`, 기록은 `memory_extract_skipped`).
6. 확정 기억의 트리플을 그래프에 넣는다(`Graph.ingest`). 엣지가 없는 기억(관계 `[]`, 또는 형식 오류로 NULL)은 문장 자체를
   임베딩·FTS로 색인해 문장으로 검색된다. NULL은 기억 관리 화면의 "관계 분석"(`/relations_backfill`)으로 트리플을 채운다.
7. 오래된 메시지가 쌓이면 haiku가 대화 언어로 롤링 요약한다. 보관·삭제가 일어나면 요약은 통째로 다시 만든다.

## 그래프 반영 규칙

- 엔티티는 정확한 이름으로 식별한다(유사도 병합 없음). 사용자는 항상 "사용자", 이름은 `사용자 HAS_NAME 민준`.
- 같은 주어·관계·대상은 한 엣지(출처 추가). `X`와 `NOT_X`는 모순.
- `SINGLE`(새 값이 이전 값 대체): LIVES_IN, HAS_NAME, WORKS_AT, STUDIES_AT, HAS_JOB. 나중 사실이 이긴다(`graph.later`):
  둘 다 사용자가 말한 성립일이 있으면 늦은 날짜, 아니면 나중에 말한 쪽(`memories.valid_from_stated`로 구분).
- `MULTI`(누적): LIKES, DRINKS, EATS, OWNS, PLAYS, STUDIES, FRIEND_OF, COLLEAGUE_OF, FAMILY_OF, PLANS_TO_VISIT.
- 사전 밖 관계(예: DRIVES)만 Claude(haiku)가 중복·모순을 판정한다(`Graph._judge`, 프롬프트 `graph.JUDGE`).
- 사전 확장: 사전 밖 관계의 판정을 `relation_observations`에 쌓고, 처리 기록 화면 "확장 후보" 탭이 근거 5회 이상·90% 이상
  같은 판정이면 대체/누적을 추천한다. 확정은 **사용자 버튼으로만**(`/vocabulary_promote`, 되돌리기 `/vocabulary_demote`).
  확정한 관계는 `vocabulary` 테이블에 남고 다음 반영부터 규칙으로 판정한다. 대화 초기화에도 유지된다.
- 규칙 피드백: 검증(`EVALUATE`)은 거절마다 어긴 규칙을 사유로 낸다(subject·negation·hypothetical·ambiguous·unsupported·
  relation·date, 그 밖은 other, 판정 누락은 omitted). 사유별 개수만 검증 호출의 `outcome.reject_reasons`에 남는다(원문 없음).
  "확장 후보" 탭이 최근 검증 200회에서 사유를 모아 3회 이상이면 **검토 필요**로 올리고 예시 입력(보관한 턴 제외)을 보인다
  (`MemoryService.rejections`). 추출과 검증은 같은 규칙을 따르므로 반복 사유는 규칙의 빈틈이나 모델의 오독이다. 지시문은
  자동으로 바꾸지 않는다.
- 기억별 상태 `linked`/`no_relation`/`missing`을 기록해 `/health`의 `coverage`로 보인다. 반영 완료 ≠ 검색 가능.
- 증분 반영: 보관·영구삭제된 기억과 관계를 다시 분석한 기억(`graph_stale`)의 에피소드만 빼고, 새 확정·복구만 넣는다.
  전체 재구축은 그래프 설정(`GraphConfig.ingestion`, 임베딩 모델) 변경, 쓰기 중 장애(pending 세대), `/reset`, 반영 실패 재시도에서만.
- 빼기(`Graph.remove`)는 출처가 모두 빠진 엣지만 지우고, 공유 엣지는 남은 첫 기억의 문장으로 fact를 다시 쓴다.
  무효화는 엣지 속성 `invalidated_by`로 기록해 원인이 빠지면 되살리거나 대체한 기억에 넘긴다.
- 인출 순서(`Graph.search`): ① 앵커 엔티티의 엣지 ② 앵커와 유효한 엣지로 이어진 사람 엔티티의 엣지 ③ 전체 하이브리드
  (엣지 + 엣지 없는 기억의 문장). 각 목록은 FTS5 bm25(단어 OR)와 cosine 유사도를 2×limit개씩 뽑아 RRF(상수 1)로 합친다.
  유사도 하한 0.6은 ③에만 쓴다(앵커 후보는 이미 질문 대상이라 하한 없이 순위만 매김).
- 앵커(`Graph._anchors`): 질문에 나온 이름(가장 긴 일치), 나/내/저/우리 → "사용자". 이름이 없으면 직전 사용자 메시지
  2개의 이름(후속 질문 "그분 무슨 일 하셨지?"), 그것도 없으면 "사용자"(개인 비서의 질문 기본 주어).
- 임베딩은 nomic 작업 접두어를 붙인다(질문 `search_query:`, 저장 문장 `search_document:`).
- 끝난 일정·상태(`expires_at` 경과)는 버리지 않고 "지난 일"로 표시하며 점수를 0.3 낮춘다. 무효화된 사실(`invalid_at`)은 뺀다.
  보관된 기억이 출처인 엣지는 워커가 그래프를 고치기 전에도 버린다. 고정 기억은 항상 포함, 예산 1600은 **UTF-8 바이트**.

## 불변 조건

- 같은 turn_id의 동일 커밋은 멱등. 취소·오래된 epoch 응답은 커밋하지 않는다.
- 보관은 epoch를 바꿔 진행 중 추출·요약·응답·커밋을 무효화한다. 범위: 잊는 기억의 근거 턴은 통째로, 그 메시지를 문맥으로 본
  이후 턴은 **답변만** 숨긴다(`turns`/`reply_turns`). 이관 표시 턴(`untracked=1`)은 가장 이른 잊는 메시지 이후 답변을 모두 숨긴다.
  복구·완전삭제는 같은 배치에 적용한다. `/forget_preview`가 범위를 미리 보여 준다.
- 그래프는 원본이 아니라 파생물이다. 원본 revision과 그래프 발행 revision이 다르면 발행하지 않는다. 쓰기 전에 pending 세대를 기록하고,
  응답 유실·재시작 시 중복 append 대신 그 세대를 폐기·재구축한다. 정리가 밀리면 `/health`에 `cleanup_pending`.
- 한 DB당 서버 하나. DB 옆 `.lock` 파일로 두 번째 서버를 거부한다(프로세스가 끝나면 OS가 해제).
- 그래프 파일은 DB 옆 `<DB 이름>.graph.db`, 그래프 이름은 DB 경로 해시(`phro_ai_<hash>_`, 내부 식별자라 개명 후에도 유지).
  DB는 그래프를 만든 경로를 `runtime.graph_owner`에 기록하고, 다른 경로에서 열리면 자기 그래프를 다시 만든다
  (이동이면 옛 경로의 그래프와 빈 그래프 파일 삭제, 복사본이면 원본용으로 둔다).
- 추출·반영은 5회 실패하면 dead(지수 백오프). 원인을 고친 뒤 `POST /memory_retry {}`.
- 로컬 Host/Origin 검증, Electron sandbox/contextIsolation, 허용 자산 경로 제한을 유지한다.
- `llm_calls`에는 시간·토큰·비용·오류·턴·결과(id·개수·다음 단계)만 기록한다. **프롬프트·응답 원문은 저장하지 않는다.**

## 처리 기록

모든 `llm_calls` 행은 처리한 턴(`turns`)과 결과(`outcome`)를 남긴다. 백그라운드 작업은 `memory/trace.py`의 컨텍스트 변수로
턴을 넘겨받는다(그래프는 호출한 스레드에서 돈다). 여러 턴이 함께 쓴 호출은 각 턴에 "공유 n"으로
표시하고 합계에는 1/n만 더한다. 출처 턴이 50개를 넘는 재구축은 특정 입력에 묶지 않는다.
조회 `GET /trace?limit=30`, 화면은 메뉴 "처리 기록 보기". `PHRO_TOKEN_DEBUG=1`이면 각 행을 stderr에 JSON으로 찍는다.

## 실행

Python 3.12, Node.js 22.12+, Claude Code CLI(로그인; 루트 `setup.ps1`이 설치). 그 밖의 서비스는 없다.
임베딩 모델은 `.venv/Scripts/python.exe -m memory.embedder`로 `models/`에 받는다(해시 고정, 이미 있으면 건너뜀).

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe server.py          # API만, 127.0.0.1:8770
```

데스크톱 앱은 [desktop.md](desktop.md). 모델은 첫 임베딩 때 한 번 읽는다(메모리 약 0.5GB).

| 설정 | 기본값 |
|---|---|
| 원본 DB | `%LOCALAPPDATA%/phro-demo/memory.db`, `PHRO_MEMORY_DB` |
| API / 데스크톱 포트 | 8770 / 8771(`PHRO_DESKTOP_PORT`) |
| Claude 모델 | 응답·관계 소급 `claude-sonnet-5-5`, 기억 추출·검증·요약·관계 판정 `claude-haiku-5-5` (`server.MODELS`), `--effort medium` |
| 임베딩 모델 폴더 | `PHRO_MODEL_DIR`, 기본 `models/nomic-embed-text-v1.5` |
| Python(데스크톱) | `PHRO_PYTHON`, 기본 루트 `.venv` |

진단 순서: `/health`(HTTP·작업 상태, `memory.error`) → 모델 파일(`python -m memory.embedder`가 해시 확인) → `/retrieve`의 degraded/error.
`ready=true`는 반영 상태이지 실시간 연결 보증이 아니다.

## 기억 제어 CLI

```powershell
.venv/Scripts/python.exe forget_cli.py search "서울"
.venv/Scripts/python.exe forget_cli.py forget 12 --reason "위치 기록"
.venv/Scripts/python.exe forget_cli.py list
.venv/Scripts/python.exe forget_cli.py restore 3
.venv/Scripts/python.exe forget_cli.py purge 3 --yes
.venv/Scripts/python.exe forget_cli.py list --server http://127.0.0.1:8771   # 데스크톱 서버 대상
```

완전삭제는 원본 기억과 출처 대화 내용을 지운다. 외부 백업의 삭제는 별도다.

## 백업과 복구

원본 SQLite 한 파일만 백업한다. `memory.graph.db`는 저장된 트리플로 재구축되는 파생물이라 백업하지 않아도 된다
(재구축에 기억 추출용 Claude 재호출 없음, 사전 밖 관계 판정만 다시 부른다). 실행 중에도 일관된 사본:

```powershell
.venv/Scripts/python.exe -c "import sqlite3,sys;sqlite3.connect(sys.argv[1]).backup(sqlite3.connect(sys.argv[2]))" C:/path/memory.db C:/path/memory-backup.db
```

복구는 서버를 멈추고 파일을 되돌리거나 `PHRO_MEMORY_DB`를 지정한다. 영구삭제 뒤 이전 백업을 복구하면 지운 원문이 돌아온다.

## 검증

```powershell
.venv/Scripts/python.exe run_checks.py            # 오프라인: pytest + desktop Node 테스트
.venv/Scripts/python.exe run_checks.py --e2e      # + Electron E2E(가짜 claude, 합성 DB, 격리 프로필)
$env:PHRO_LIVE_TEST='1'; .venv/Scripts/python.exe -m pytest -q tests/test_graph_live.py              # 실제 임베딩 모델
$env:PHRO_CLAUDE_TEST='1'                                                                             # + 실제 Claude 판정(유료)
.venv/Scripts/python.exe tests/scenario_full.py   # 실제 Claude 전체 시나리오(유료, 약 $0.26)
.venv/Scripts/python.exe tests/eval_memory.py    # 대화형 인출 평가 18문항(후속 질문·관계 없는 기억·별칭·시간·정정), Claude 없음
```

live 테스트는 `models/`에 모델이 있어야 한다. 테스트는 임시 DB와 그 옆 그래프 파일만 건드린다. 사용자 DB(`%LOCALAPPDATA%\phro-demo\memory.db`)로 테스트하지 않는다.

측정 기준치(합성 DB, `tests/scale_bench.py`, SQLite 그래프 + 프로세스 내 임베딩): 1만 개 기억에서 보관·복구·추가 1초 미만,
반영 기억당 0.035s, 인출 p50 0.39s / p95 0.44s(앵커·사람 확장·문장 검색 포함), 근거 포함률 100%(질의 200개).
대화형 인출 평가(`tests/eval_memory.py`) 18/18. (이전 FalkorDB+Ollama: 반영 0.11s, 인출 p50 0.4s, 대화형 평가 7/18.)
실제 Claude 시나리오 21개 검사 통과, 한 번에 약 $0.26.

## 남은 작업

- 음성(마이크→STT→대화→TTS): 공급자 미정으로 보류. `pipeline.js`의 `opts.commit=false`(추측 실행 후 커밋 결정)가 그 자리다.
- 배율이 다른 모니터 혼합, 실제 고대비 테마는 수동 확인 필요.
- 커뮤니티 시트 재배포 권리 미확인(설치 파일에 시트 미포함, 첫 실행에 출처에서 받음).
- `WORKS_AT`이 단일값이라 동시에 두 직장은 나중 것만 남는다. 앵커 인출은 이름이 질문에 그대로 나와야 걸린다(별칭 불가).
- 코드 서명 없음(결정). 배포 시 SHA-256을 따로 전달한다([desktop.md](desktop.md)).
