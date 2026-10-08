# 데스크톱 앱 (`desktop/`)

create-pet 규격의 캐릭터시트를 재생하는 Electron 앱. V1(1536×1872), V2(1536×2288), 192×208 셀, 9개 애니메이션,
V2는 16방향. 시트는 한 번 디코딩하고 프레임이 바뀔 때만 Canvas를 갱신하며 프레임 재생에 LLM 호출이 없다.

| 파일 | 책임 |
|---|---|
| `main.cjs` | 창·트레이·메뉴, 백엔드 기동/종료, 샌드박스, 외부 탐색·권한 차단, 시트 가져오기 IPC |
| `preload.cjs` | 렌더러에 노출하는 최소 API |
| `pets.cjs` | 가져온 시트 검증·스테이징·원자적 목록 |
| `serve.py` | `server.Handler` 확장, 허용된 화면 자산과 시트만 제공 |
| `pipeline.js` | DOM/Electron 없는 공용 대화 클라이언트(인출→응답→커밋→요약). 새 화면도 이것을 쓴다 |
| `overlay.*` | 투명·항상 위 캐릭터 오버레이, 입력창, 진행/답변 카드 |
| `index.html`, `app.js`, `style.css` | 대화 창 |
| `manage.js` | 기억 관리(검색·보관 미리보기·복구·완전삭제) |
| `trace.*` | 처리 기록(입력별 단계·모델·토큰·다음 이동, 관계 사전 확장 후보) |
| `sprite.js` | 시트 프레임/방향 좌표, 감정→동작 매핑(`EMOTION_MOTION`) |
| `download_pets.py`, `pets/catalog.json` | 카탈로그에 지정된 커뮤니티 시트 다운로드·해시 검증 |
| `build-python.mjs` | 설치 파일용 embeddable Python 준비 |
| `test-*.mjs` | Node 단위 테스트(`pipeline`, `sprite`, `pets`, 페이지 스크립트 구문) |

## 실행

```powershell
.venv/Scripts/python.exe desktop/download_pets.py
cd desktop
npm ci
npm start
```

Electron이 백엔드(`serve.py`, 8771)를 직접 시작·종료한다. 같은 포트에 다른 서버를 띄우지 않는다.
테스트할 때는 `PHRO_MEMORY_DB`로 별도 DB를 지정한다.

## 한 번에 켜고 한 번에 끄기

| 구성 요소 | 시작 | 종료 |
|---|---|---|
| 백엔드(`serve.py`) | Electron | Electron이 `POST /shutdown`(실행마다 새 토큰, 헤더 `X-Phro-Shutdown`). 30초 넘으면 강제 종료 |
| WSL 세션(`wsl.exe ... cat`) | 백엔드 | 백엔드와 함께 |
| FalkorDB(`phro-falkor` 서비스) | distro 부팅 시 + `systemctl start` | **백엔드가 켰을 때만** `systemctl stop`(AOF 기록). 이미 떠 있던 것은 둔다 |
| Ollama(`ollama serve`) | 11434가 비어 있으면 백엔드 | **백엔드가 켰을 때만** 종료 |
| Claude CLI | 호출마다 | 호출 종료 또는 백엔드와 함께 |

강제 종료·크래시에도 남는 프로세스가 없다: Electron의 자식은 libuv kill-on-close job, 백엔드의 자식은 백엔드가 만든 job
(`kill_children_with_me`)에 들어 있다. WSL VM은 WSL 자체 유휴 시간(약 1분) 뒤 내려간다.
최초 1회 설치(WSL `phro-falkor` 서비스, Ollama + `nomic-embed-text`, Claude CLI 로그인)는 필요하다. 첫 실행 15초 뒤 빠진 구성 요소를 알려 준다.

## 오버레이

- 이동: 끌면 `running-left/right`, 놓으면 `jumping`. 포커스 후 방향키(Shift 50px). 드래그는 JS+IPC로 처리한다.
- 클릭 통과: 불투명 픽셀과 패널만 마우스를 받는다(`setIgnoreMouseEvents` + forward).
- 대화: 연필·더블클릭·Enter·`Ctrl+Shift+Space`(전역). 진행 중 전송 버튼이 중지가 되고 Esc도 취소. `+`는 대화 창.
- 카드: 단계와 경과 초, 답변은 커밋 전에 바로 표시, 추출이 끝나면 "기억 N개 저장됨".
- 상태 점(10초마다 `/health`): 초록 정상, 주황 KG 대기/오류/dead/관계 누락, 빨강 서버 응답 없음.
- 메뉴(우클릭·트레이): 보이기/숨기기, 글쓰기, 대화 창, 캐릭터, 크기 1x/1.5x/2x, 커서 바라보기(V2), 전체화면 위 표시,
  처리 기록 보기, 사용법, 종료.
- 저장: 위치·크기·옵션은 userData `overlay.json`, 캐릭터는 localStorage. 사라진 모니터의 위치는 주 모니터로 되돌린다.
- 캐릭터 가져오기: create-pet/Codex 펫 폴더(`pet.json` + `spritesheet.webp`). id 형식·내장 충돌·경로 이탈·20MB·WebP 규격을
  검사하고 userData `pets/`에 스테이징 후 교체한다. 실패하면 기존 캐릭터 유지. ZIP은 미지원.
- 두 창은 `BroadcastChannel`(`phro-motion`, `phro-activity`)로 동작·진행·메시지를 공유한다. 대화 창 닫기는 숨김.
- 고대비(`forced-colors`, `prefers-contrast`)에서는 시스템 색. `prefers-reduced-motion`이면 애니메이션을 끈다.

## 설치 파일

```powershell
cd desktop
npm ci
npm run dist
```

`build-python.mjs`가 python.org embeddable CPython 3.12.10(해시 고정)에 런타임 패키지(requirements.txt에서 pytest 제외)를 넣고,
pip 실행 파일(`site-packages/bin`)은 지운다(빌드 PC 경로가 박힘). electron-builder가 `build/dist/phro-secretary Setup 0.1.0.exe`
(사용자별 설치, 약 131MB)를 만든다. 실행 중인 phro-secretary가 `win-unpacked`를 잠그므로 빌드 전에 앱을 끈다.
앱 설정(userData)은 `%APPDATA%\phro-secretary`다. 이전 이름(Phro-AI)으로 설치했던 위치·크기·가져온 캐릭터는 따라오지 않으니
다시 설정하거나 `%APPDATA%\Phro-AI`의 `overlay.json`, `pets/`를 옮긴다. 기억 DB(`%LOCALAPPDATA%\phro-demo`)는 그대로 쓴다.
포함: 백엔드 소스, `memory_engine/`(LICENSE·NOTICE 포함), Python. 미포함: 커뮤니티 시트, Claude CLI, FalkorDB, Ollama.

서명 없이 배포한다. 받는 사람에게 함께 전할 것:
- SHA-256: `Get-FileHash "build\dist\phro-secretary Setup 0.1.0.exe"` 값을 설치 파일과 다른 경로로 보내 대조하게 한다.
- SmartScreen "Windows의 PC 보호"가 뜨면 **추가 정보 → 실행**. 브라우저가 막으면 "유지".

## 시트 출처

- [月薪喵 · xiaoyyy · V2](https://codex-pets.net/#/pets/monthly-salary-cat-fix)
- [Clawd · Han1 · V1](https://codex-pets.net/#/pets/clawd)

커뮤니티 공유 자산이며 재배포 허가를 확인하지 않았다. 시트 파일은 Git에서 제외하고 출처·URL·SHA-256만 `pets/catalog.json`에 둔다.
