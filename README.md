# phro-secretary

장기 기억을 가진 캐릭터 비서. 캐릭터시트 오버레이로 대화하고, 대화에서 확정한 사실을 지식그래프로 기억한다.
LLM은 Claude CLI, 그래프는 FalkorDB, 임베딩은 로컬 Ollama.

## 설치 (Windows 10/11)

1. **필수 구성 요소** — PowerShell에서 한 줄 실행. WSL Ubuntu-24.04 + FalkorDB(`phro-falkor` 서비스), Ollama +
   `nomic-embed-text`, Claude Code CLI를 설치한다. 이미 있는 것은 건너뛰므로 여러 번 실행해도 된다.

   ```powershell
   irm https://raw.githubusercontent.com/ThatsHoon/phro-secretary/main/setup.ps1 | iex
   ```

   WSL을 처음 설치하면 Ubuntu 사용자 만들기(필요하면 재부팅) 후 같은 명령을 한 번 더 실행한다.
2. **Claude 로그인** — 새 터미널에서 `claude`를 한 번 실행하고 로그인한다(Claude 구독 또는 Console 계정).
3. **앱 설치** — [phro-secretary Setup 0.1.0.exe](GOOGLE_DRIVE_LINK) 받아 실행.
   - SHA-256 `cf752afb3b8446f5caf273ce1af75560cc8858a8c2a6773bae3531a84b1e62ca` —
     `Get-FileHash "phro-secretary Setup 0.1.0.exe"`로 대조.
   - 서명 없는 설치 파일이라 SmartScreen이 뜨면 **추가 정보 → 실행**.
4. 처음 실행하면 기본 캐릭터 시트를 출처에서 받고, 빠진 구성 요소가 있으면 15초 뒤 알려 준다.

기억은 `%LOCALAPPDATA%\phro-demo`, 앱 설정은 `%APPDATA%\phro-secretary`에 저장된다. 모든 서비스는 127.0.0.1에서만 열린다.

## 개발

```text
server.py        로컬 HTTP API와 외부 서비스 수명주기
memory/          기억 정책: 원본 SQLite, 추출·검증, 그래프 반영 규칙, 처리 기록
memory_engine/   그래프 기억 엔진 (출처·라이선스: NOTICE.md, LICENSE)
desktop/         Electron 앱과 설치 파일 빌드
tests/           테스트와 수동 벤치
docs/            문서
setup.ps1        필수 구성 요소 설치
```

`setup.ps1` 실행 후 Python 3.12, Node.js 22.12+ 필요:

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe desktop/download_pets.py
cd desktop; npm ci; npm start
```

문서:
- [구조와 운영](docs/architecture.md) — 흐름, 불변 조건, 설정, 검증, 남은 작업
- [데스크톱 앱](docs/desktop.md) — 오버레이, 프로세스 수명주기, 설치 파일
- [시행착오 기록](docs/troubleshooting.md) — 겪은 문제와 해결

## 라이선스

MIT ([LICENSE](LICENSE)). 단 `memory_engine/`은 Graphiti(Zep Software) 파생이라 Apache-2.0
([memory_engine/LICENSE](memory_engine/LICENSE), [NOTICE](memory_engine/NOTICE.md)).
기본 캐릭터 시트는 codex-pets.net 커뮤니티 자산으로 이 저장소와 설치 파일에 포함하지 않는다.
