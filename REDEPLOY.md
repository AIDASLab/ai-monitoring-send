# ai-monitoring-send 재배포 가이드

원격 GPU 서버에서 sender 코드를 갱신하고 재시작하는 절차. 2026-07-21 수집 로직
수정(아래 "이번 변경") 이후 각 활성 서버에 반영해야 합니다.

> ⚠️ **중앙 서버(ADS-A100)에서는 sender를 실행하지 마세요.** ADS-A100은
> `backend.server`가 로컬(~/.claude2, ~/.codex) + NAS를 직접 수집하는 중앙입니다.
> 여기 `~/Workspace/ai-monitoring-send`는 **편집·배포용 원본 사본**이라 `config.json`이
> 없고(gitignore), `./start.sh`를 돌리면 기본 ssh 트랜스포트가 비밀번호를 요구하며
> `ValueError: transport.mode=ssh needs ssh_password or ssh_key` 로 즉시 죽습니다.
> 실수로 띄웠다면 `./stop.sh` 로 정리하세요. 아래 절차는 **원격 서버 전용**입니다.

## 신규 서버 세팅: 계정 분리 자동화 (`scripts/`)

> 처음부터 끝까지의 단계별 절차와 각 단계의 **예상 출력**, 이미 열려 있는
> 세션 처리까지는 **[DEPLOY-NEW-SERVER.md](DEPLOY-NEW-SERVER.md)** 를 보세요.
> 아래는 각 스크립트가 무엇을 하는지에 대한 설명입니다.

사람별·계정별로 사용량이 집계되려면 계정마다 설정 디렉토리가 분리돼 있어야 합니다.
ADS-A100에서 손으로 했던 작업 전부를 두 프로그램이 대신합니다.

### `scripts/setup-accounts.py` — 디렉토리 리팩토링 + 환경변수/alias + 사이드바

```bash
cd ~/ai-monitoring-send
python3 scripts/setup-accounts.py --dry-run    # 무엇이 바뀌는지 먼저 확인
python3 scripts/setup-accounts.py              # 적용
source ~/.bashrc
```

한 번에 처리하는 것:

| # | 작업 | 결과물 |
|---|---|---|
| 1 | 계정별 설정 디렉토리 생성 (0700) | `~/.claude-lab1`, `~/.codex-lab1`, `~/.claude-lab2`, `~/.codex-lab2` |
| 2 | 자동화용 codex 래퍼 | `~/.local/bin/codex-lab1`, `codex-lab2` |
| 3 | 원격 클라이언트 진입점 | `~/.local/bin/codex` 디스패처 (`app-server`만 랩으로) |
| 4 | 셸 함수(alias) 자동 등록 | `~/.bashrc` 의 `lab1 <cmd>` / `lab2 <cmd>` |
| 5 | VSCode 사이드바 계정 지정 | 확장 번들 바이너리 래핑 + `settings.json` |
| 6 | sender 수집 경로 반영 | `config.json` 의 `claude.config_dirs`, `codex.dirs` |

**멱등**합니다 — 두 번째 실행부터는 전부 `= 이미 최신`, 변경 0건. 바꾸는 파일은 모두
`.bak-<타임스탬프>` 로 백업합니다. 마커 없이 손으로 넣어둔 예전 `lab1()/lab2()` 정의가
있으면 마커 블록으로 통합해 중복을 없앱니다.

주요 옵션: `--labs lab1,lab2` `--sidebar lab1` `--no-vscode` `--no-bashrc`
`--no-sender-config` `--claude-bin/--codex-bin`(PATH에 없을 때)

마지막에 **대화형 로그인 명령**을 출력합니다. 로그인만 사람이 직접 하면 됩니다:
```bash
CODEX_HOME=~/.codex-lab1 codex login
CLAUDE_CONFIG_DIR=~/.claude-lab1 claude auth login
```
> `~/.bashrc` 에 `claude()`/`codex()` 오버라이드가 이미 있으면 스크립트가 경고합니다.
> 그 경우 로그인은 **전체 경로**(`~/.local/bin/claude`)로 실행하세요. 함수가
> 환경변수를 덮어써 엉뚱한 디렉토리에 로그인됩니다.

### `scripts/sidebar-account.py` — 사이드바 계정만 바꾸기

터미널(`lab1`/`lab2`)과 무관하게, VSCode 사이드바에 뜨는 계정만 전환합니다.
```bash
python3 scripts/sidebar-account.py --show        # 현재 계정
python3 scripts/sidebar-account.py --lab lab2    # 랩2로
python3 scripts/sidebar-account.py --personal    # 개인 계정(~/.codex, ~/.claude)로 복귀
```
**VSCode 창 리로드**하면 적용됩니다(원격 서버 재시작 불필요).

두 확장의 사정이 다릅니다. Claude 는 `claudeCode.environmentVariables` 라는 정식
설정이 있지만, **Codex 확장에는 계정 설정이 없습니다.** `chatgpt.cliExecutable` 은
app-server 실행에 쓰이지 않고(검증함), 확장은 자기 번들 바이너리
`~/.vscode-server/extensions/openai.chatgpt-*/bin/*/codex` 를 직접 실행하며 계정은
상속받은 `CODEX_HOME`(없으면 `~/.codex`)으로만 정해집니다. 확장 호스트에 그 변수를
넣을 방법도 없어(이 서버 빌드는 `server-env-setup` 미지원), **번들 바이너리 자리에
래퍼를 두고 원본을 `codex.real` 로 옮깁니다.**

> ⚠️ Codex 확장을 업데이트하면 래퍼가 덮어써져 **조용히 개인 계정으로 돌아갑니다.**
> 중앙의 `aidas-ai-monitoring/scripts/check-routing.py` 가 그 상태를 탐지하므로
> 주기적으로 돌리세요. 복구는 `sidebar-account.py --lab <lab>` 재실행.

### 확장/터미널/자동화가 각각 어디에 기록되는지

| 실행 주체 | 보는 값 | 기록 위치 |
|---|---|---|
| VSCode Claude 확장 | `claudeCode.environmentVariables` | 지정한 `~/.claude-labN` |
| VSCode Codex 확장 | 확장 번들 바이너리를 래핑한 `CODEX_HOME` | 지정한 `~/.codex-labN` |
| 터미널 `lab1 codex` | 셸 함수 | `~/.codex-lab1` |
| 터미널 맨 `codex` | 미설정 | `~/.codex` (개인) |
| 데스크탑 앱 (SSH 원격) | `~/.local/bin/codex` 디스패처 | 지정한 `~/.codex-labN` |
| **스크립트·Claude Code 등 자동화** | 미설정 | **`~/.codex` (개인) ⚠** |

마지막 줄이 함정입니다. `CODEX_HOME` 을 상속받지 못하는 자동화(cron, 다른 Claude Code
세션이 Bash로 띄우는 `codex` 등)는 랩 계정으로 잡히지 않습니다. **`codex` 대신
`~/.local/bin/codex-lab1` 을 호출**하게 하세요. 이미 떠 있는 `codex resume` TUI도
전환 이전에 시작됐다면 옛 디렉토리에 계속 씁니다 — `lab1 codex resume` 으로 다시 여세요.

데스크탑 ChatGPT 앱이 SSH 로 붙는 경우는 별도 함정입니다. sshd 가
`PATH="$HOME/.local/bin:$PATH"; codex app-server proxy` 를 실행하는데 그 셸에는
`CODEX_HOME` 을 넣을 방법이 없습니다(`PermitUserEnvironment` 는 보통 꺼져 있고
`~/.bashrc` 는 비대화형에서 즉시 return). 그래서 `setup-accounts.py` 가
`~/.local/bin/codex` 를 **디스패처 스크립트**로 바꿔, IDE·원격 클라이언트 전용
진입점인 `app-server` 서브커맨드일 때만 랩 계정으로 고정합니다. 터미널 대화형
(`codex`, `codex resume`)은 그대로 개인 계정입니다.

데스크탑 앱은 `CODEX_HOME` 을 비워 보내지 않고 **개인 기본값(`~/.codex`)을 명시적으로**
넘깁니다(`CODEX_REMOTE_PAYLOAD` 와 함께). 그래서 "미설정일 때만" 규칙으로는 안 잡히고,
개인 기본값과 같을 때도 갈아끼웁니다. `lab1`/`lab2` 처럼 의도적으로 지정한 값은 존중합니다.

> 디스패처를 바꾼 뒤에는 **데스크탑 앱의 원격 연결을 한 번 끊었다 다시 붙여야** 합니다.
> 이미 떠 있는 `app-server proxy` 는 연결 시점의 환경을 그대로 유지합니다.

> ⚠️ codex 가 자기 자신을 업데이트하면 이 파일이 원래 심볼릭 링크로 되돌아가
> 조용히 새기 시작합니다. `check-routing.py` 가 탐지하고, `setup-accounts.py`
> 재실행으로 복구됩니다.

## 이번 변경 (왜 재배포가 필요한가)

수정된 파일은 **3개뿐**:

| 파일 | 내용 |
|---|---|
| `sender/claude_usage.py` | usage API 401/403(정지)·429(스로틀) 구분, 신 스키마 `limits[]`에서 Fable 주간 한도 파싱 |
| `sender/claude_collector.py` | 정지/인증오류 상태(`usage_status`)를 배치에 포함, stale 재도장 방지 |
| `sender/codex_collector.py` | **rate_limits를 윈도우별로 병합**(5h가 사라지던 버그 수정), `resets_in_seconds` 지원, 비-UTF8 rollout 방어, 계정 전환 시 캐시 격리 |

특히 **codex 5시간 한도가 대시보드에 안 뜨던 문제**는 `codex_collector.py`의 병합 버그가
원인입니다(최신 이벤트가 weekly만 담고 있으면 5h를 통째로 덮어써 잃어버림). 이 파일이
핵심입니다.

## 활성 서버: 코드 갱신 + 재시작

각 원격 서버에 접속해 sender 디렉토리(예: `~/ai-monitoring-send`)에서 실행합니다.

### 방법 A — git 사용 시
```bash
cd ~/ai-monitoring-send
git pull
./stop.sh && ./start.sh
./status.sh          # 프로세스 + 최근 로그 + NAS 최신 배치 확인
```

### 방법 B — git 미사용 시 (ADS-A100에서 파일 동기화)
ADS-A100(중앙, 소스 원본)에서 각 원격으로 3개 파일만 밀어넣습니다.
```bash
# ADS-A100에서 실행 (원격에 SSH 접근이 되는 경우)
SRC=~/Workspace/ai-monitoring-send/sender
for host in <원격1> <원격2> ...; do
  rsync -av "$SRC/"{claude_usage.py,claude_collector.py,codex_collector.py} \
        "$host:~/ai-monitoring-send/sender/"
done
```
그 후 각 원격에서:
```bash
cd ~/ai-monitoring-send && ./stop.sh && ./start.sh && ./status.sh
```

원격→ADS-A100 방향만 되는 경우(원격에서 pull):
```bash
# 각 원격 서버에서
cd ~/ai-monitoring-send/sender
scp <ads-a100>:~/Workspace/ai-monitoring-send/sender/{claude_usage.py,claude_collector.py,codex_collector.py} .
cd .. && ./stop.sh && ./start.sh && ./status.sh
```

### systemd로 돌리는 경우
```bash
sudo systemctl restart ai-monitoring-send   # 유닛명은 setup.sh --systemd 출력 참고
journalctl -u ai-monitoring-send -n 30 --no-pager
```

## 재시작 확인 포인트

`./status.sh` 또는 `tail -f data/sender.log` 에서:
- `[sender] batch ... accounts=[...]` 에 해당 계정이 보이는지
- codex 계정이면 몇 분 뒤 대시보드 카드에 **5시간 + 주간 게이지가 둘 다** 뜨는지
  (병합 수정이 반영됐다는 신호)

## aidaslab2 codex를 쓰는 서버 (`servername`)

이 서버가 aidaslab2 codex의 유일한 활성 보고자입니다. 위 방법으로 `codex_collector.py`를
갱신·재시작하면, 다음 rollout 이벤트부터 5시간 한도가 정상 집계됩니다. codex는 별도의
usage API가 없으므로 값은 rollout 파일의 `rate_limits`(= 실제 API 응답 헤더에서 CLI가 기록)
에서 옵니다 — 즉 "실제 API 호출 기반"이며, 재배포로 그 값을 온전히 읽게 됩니다.

## 만료 서버 (RND1 / RND2 / AIIS) — 재배포 대신 정지

이 3대는 중앙에서 `nas.ignore_hosts`로 무시·정리 중입니다. 각 서버의 sender는 **정지**만
하면 됩니다(계속 배치를 떨궈도 중앙이 버리지만, 불필요한 부하·NAS 쓰기를 없애려면):
```bash
cd ~/ai-monitoring-send && ./stop.sh
# systemd면: sudo systemctl disable --now ai-monitoring-send
```
