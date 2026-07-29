# 신규 서버 배포 — 처음부터 끝까지

`~/.codex` 와 `~/.claude` 만 있는 서버를 계정 분리·집계 가능 상태로 만드는 절차.
각 단계에 **실제로 출력되는 내용**을 같이 적었습니다. 시뮬레이션으로 검증한
출력이므로 그대로 나오지 않으면 그 단계에서 멈추고 원인을 보세요.

전제: 랩 계정 2개(`aidaslab.snu@gmail.com`, `aidaslab2.snu@gmail.com`)를
`lab1`/`lab2` 로 쓰고, 기존 `~/.codex`·`~/.claude` 는 **개인 계정 자리로 남깁니다.**

---

## Step 0 — 레포 받기 (비공개 레포)

`YunseokHan/ai-monitoring-send` 는 **비공개**라 인증 없이 clone 되지 않습니다.
둘 중 하나를 쓰세요.

**(a) Personal Access Token** — 서버에 GitHub 자격증명이 없을 때
```bash
git clone https://<TOKEN>@github.com/YunseokHan/ai-monitoring-send.git ~/ai-monitoring-send
cd ~/ai-monitoring-send
```

**(b) ADS-A100 에서 밀어넣기** — 토큰을 서버에 두고 싶지 않을 때
```bash
# ADS-A100 에서 실행
rsync -av --exclude data --exclude config.json \
      ~/Workspace/ai-monitoring-send/ <원격>:~/ai-monitoring-send/
```

이미 받아둔 서버라면:
```bash
cd ~/ai-monitoring-send && git pull
```
> 출력: `Updating <old>..<new>` 또는 `Already up to date.`

---

## Step 1 — sender 설치·기동 (`setup.sh`)

**이 단계를 먼저 해야 합니다.** `setup.sh` 가 `config.json` 을 만들고, 다음 단계인
`setup-accounts.py` 가 그 파일의 수집 경로를 고쳐 씁니다. 순서를 바꾸면
`⚠ sender config.json 이 아직 없습니다` 경고가 뜨고 수집 경로가 안 맞춰집니다.

NAS 가 **마운트 안 된** 서버 (기본값 = SSH 전송):
```bash
SSH_PASSWORD='<NAS_PASSWORD>' ./setup.sh --host <서버이름>
```

NAS 가 **마운트된** 서버:
```bash
./setup.sh --local --nas /mnt/nas/yunseok/ai-monitoring --host <서버이름>
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--host` / `HOST_ID` | `$(hostname)` | 대시보드에 뜨는 노드 이름. **반드시 지정**하세요(안 하면 `servername` 같은 이름이 붙습니다) |
| `--interval` / `INTERVAL` | `300` | 수집 주기(초) |
| `--password` / `SSH_PASSWORD` | — | NAS 비밀번호 (SSH 모드 필수) |
| `--key` / `SSH_KEY` | — | 비밀번호 대신 SSH 키 |
| `--local` + `--nas <경로>` | — | NAS 마운트 서버용 |
| `--ssh-host/--ssh-port/--ssh-user/--remote-root` | synology 기본값 | NAS 접속 정보 |
| `--systemd` | — | systemd 유닛 파일을 출력 (상시 기동용) |

> 확인: `./status.sh` 로 프로세스와 최근 로그, NAS 최신 배치가 보이면 성공입니다.

---

## Step 2 — 계정 분리 (`setup-accounts.py`)

먼저 무엇이 바뀌는지 봅니다.
```bash
python3 scripts/setup-accounts.py --dry-run
```

그대로 적용:
```bash
python3 scripts/setup-accounts.py
```

**출력 (신규 서버 기준):**
```
계정별 디렉토리 리팩토링  labs=['lab1', 'lab2']  sidebar=lab1
  claude=~/.local/bin/claude   codex=~/.local/bin/codex

[1] 설정 디렉토리
  + 생성      ~/.claude-lab1
  + 생성      ~/.codex-lab1
  + 생성      ~/.claude-lab2
  + 생성      ~/.codex-lab2

[2] 자동화용 codex 래퍼
  + 래퍼      ~/.local/bin/codex-lab1
  + 래퍼      ~/.local/bin/codex-lab2

[3] 원격 클라이언트 진입점 (~/.local/bin/codex)
  + 디스패처  ~/.local/bin/codex  (app-server → ~/.codex-lab1)

[4] 셸 함수 (~/.bashrc)
  + 추가      ~/.bashrc  (백업: .bashrc.bak-YYYYMMDD-HHMMSS)

[5] VSCode 사이드바 계정
  = VSCode 원격 서버 없음 — 건너뜀          ← VSCode 안 쓰는 서버면 정상

[6] sender 수집 경로
  + 수집 경로  claude=['~/.claude-lab1', '~/.claude-lab2']  codex=['~/.codex-lab1', '~/.codex-lab2']
```
이어서 로그인 명령이 출력됩니다(Step 3).

**주요 옵션**

| 옵션 | 용도 |
|---|---|
| `--labs lab1` | 랩 계정 1개만 쓰는 서버 |
| `--sidebar lab2` | VSCode 사이드바를 lab2 로 (기본 lab1) |
| `--no-vscode` / `--no-bashrc` / `--no-sender-config` | 해당 단계 건너뛰기 |
| `--claude-bin` / `--codex-bin` | CLI 가 PATH 에 없을 때 전체 경로 지정 |

**이 단계에서 나올 수 있는 경고**

- `⚠ ~/.bashrc 에 이미 claude() 함수 오버라이드가 있습니다`
  → 로그인은 **전체 경로**(`~/.local/bin/claude`)로 하세요. 그 함수가
  `CLAUDE_CONFIG_DIR` 를 덮어써 엉뚱한 디렉토리에 로그인됩니다.
- `= PATH 에 codex 없음 — 건너뜀`
  → codex 가 `~/.local/bin` 밖에 설치된 서버. 데스크탑 원격 경로가 안 잡히니
  설치 위치를 확인하세요.

---

## Step 3 — 셸 반영 + 로그인 (사람이 직접)

```bash
source ~/.bashrc
```

스크립트가 출력한 명령을 그대로 실행합니다. 대화형이라 자동화할 수 없습니다.
```bash
# lab1
CODEX_HOME=~/.codex-lab1 ~/.local/bin/codex login
CLAUDE_CONFIG_DIR=~/.claude-lab1 ~/.local/bin/claude auth login

# lab2
CODEX_HOME=~/.codex-lab2 ~/.local/bin/codex login
CLAUDE_CONFIG_DIR=~/.claude-lab2 ~/.local/bin/claude auth login
```

확인:
```bash
CODEX_HOME=~/.codex-lab1 ~/.local/bin/codex login status
CLAUDE_CONFIG_DIR=~/.claude-lab1 ~/.local/bin/claude auth status
```
> 출력: `Logged in using ChatGPT` / 계정 이메일. 랩 계정 주소가 맞는지 꼭 보세요.

---

## Step 4 — 수집 경로 반영 후 sender 재시작

```bash
./stop.sh && ./start.sh && ./status.sh
```
> `[sender] batch ... accounts=[...]` 에 랩 계정이 보이면 성공입니다.

---

## Step 5 — 열려 있던 세션 정리 (아래 절 참고)

환경변수는 **프로세스 기동 시점에만** 읽힙니다. 이미 떠 있던 것들은 전부
옛 계정으로 계속 씁니다.

---

## Step 6 — 중앙(ADS-A100)에서 확인

```bash
python3 ~/Workspace/aidas-ai-monitoring/scripts/check-routing.py
python3 ~/Workspace/aidas-ai-monitoring/scripts/check-routing.py --recent 10
```
> 신규 서버는 NAS 를 통해 들어오므로 중앙 대시보드의 노드 표와
> `scripts/people-report.py --nodes` 로 확인하세요. `check-routing.py` 는
> **로컬 홈만** 검사하므로 원격 서버에서도 각각 돌려야 합니다.

---

# 이미 열려 있는 세션 처리

`CODEX_HOME` / `CLAUDE_CONFIG_DIR` 는 기동 시점에만 읽히므로, 설정을 바꿔도
**떠 있는 프로세스는 옛 계정으로 계속 기록합니다.** 전부 재시작해야 합니다.

| 대상 | 처리 | 확인 |
|---|---|---|
| 터미널 codex TUI | 종료 후 `lab1 codex` 또는 `codex-lab1 resume` | `check-routing.py` 에서 `[수집]` |
| 터미널 claude CLI | 종료 후 `lab1 claude` | 〃 |
| VSCode 사이드바 | **창 리로드** (Codex 는 확장 래핑 후) | 〃 |
| 데스크탑 앱 SSH 원격 | 연결 끊고 **재연결** | 〃 |
| cron·스크립트·다른 Claude Code | 호출을 `codex` → `codex-lab1` 로 변경 | 〃 |

떠 있는 것을 한 번에 찾으려면:
```bash
python3 ~/Workspace/aidas-ai-monitoring/scripts/check-routing.py
# "떠 있는 프로세스" 절에서 [누락] 로 표시된 pid 들이 재시작 대상
```

## 진행 중이던 대화를 새 계정에서 이어가기 (선택)

재시작만 하면 **과거 대화는 옛 디렉토리에 남아 `resume` 목록에 안 뜹니다.**
이어가려면 세션 파일을 복사하세요. 복사해도 과거 사용량이 집계되지는 않습니다
(수집기는 계정 경계 이후의 턴만 귀속합니다).

**codex** — rollout 파일 + `session_index.jsonl` 의 해당 줄이 필요합니다.
```bash
SID=019f7e79-0476-7053-9bb5-1073844c1201     # resume 목록에서 확인한 세션 id
SRC=~/.codex; DST=~/.codex-lab1
f=$(cd "$SRC" && find sessions -name "*$SID*.jsonl")
mkdir -p "$DST/$(dirname "$f")" && cp -p "$SRC/$f" "$DST/$f"
grep -F "$SID" "$SRC/session_index.jsonl" >> "$DST/session_index.jsonl"
```
> 확인: `codex-lab1 resume` 목록에 thread 이름이 뜨면 성공.

**claude** — 프로젝트 디렉토리(작업 경로를 인코딩한 이름) 통째로 복사합니다.
```bash
P=-home-yunseok-Workspace-diffusionLM-HiBS-GRPO      # ~/.claude/projects 에서 확인
cp -rp ~/.claude/projects/$P ~/.claude-lab1/projects/
```

> ⚠️ 복사 후 양쪽에서 같은 세션을 열면 **대화가 갈라집니다.** 새 턴은 그때
> 활성인 `CODEX_HOME` 쪽에만 쌓입니다. 한쪽만 쓰세요.

---

# 재실행 안전성

`setup-accounts.py` 는 **멱등**합니다. 두 번째부터는 전부 `= 이미 최신` 이고
변경 0건입니다. 고치는 파일은 모두 `.bak-<타임스탬프>` 로 백업하며,
`~/.local/bin/codex` 는 심볼릭 링크 원본을 `codex.symlink-backup` 으로 남깁니다.

정기적으로 `check-routing.py` 를 돌리세요. 다음 세 가지가 **조용히** 설정을
되돌립니다.

1. **VSCode Codex 확장 업데이트** → 래핑한 번들 바이너리가 덮어써짐
2. **codex 자체 업데이트** → `~/.local/bin/codex` 디스패처가 심볼릭 링크로 복구됨
3. **환경변수 없이 뜬 새 프로세스** → 개인 경로로 기록

모두 `setup-accounts.py` 재실행으로 복구됩니다.
