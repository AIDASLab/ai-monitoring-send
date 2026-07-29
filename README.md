# ai-monitoring-send (송신 에이전트)

다른 서버에서 **Claude Code + Codex** 사용량을 수집해 **Synology NAS로 SSH 전송**하는
경량 에이전트. 서버 간 직접 통신이 안 되고 **NAS도 마운트되어 있지 않은** GPU 서버에서,
`scp`로 NAS에 파일을 떨구면 중앙 대시보드([aidas-ai-monitoring](../aidas-ai-monitoring))가
NAS 마운트를 폴링해 읽어갑니다.

- **Python 3 표준 라이브러리만** 사용 (sshpass/paramiko 등 설치 불필요)
- `setup.sh` **하나만 실행**하면 설정 생성 + 백그라운드 시작
- **SSH 비밀번호 자동 입력**(stdlib `pty` 사용) — 또는 SSH 키 사용 가능
- Claude(`~/.claude*` 자동탐지) + Codex(`~/.codex`)를 모두 수집

```
GPU 서버 A ─┐ scp (pw 자동)
GPU 서버 B ─┼─▶ NAS: /volume1/nas-nfs/yunseok/ai-monitoring/inbox/<host>/batch-*.json.gz
GPU 서버 C ─┘                          ▲ (= 중앙의 /mnt/nas/yunseok/... 마운트)
                                       └ 중앙 대시보드가 30초마다 폴링해서 ingest
```

---

## 빠른 시작 (각 송신 서버에서)

```bash
git clone <this-repo> ai-monitoring-send
cd ai-monitoring-send
SSH_PASSWORD='<NAS_PASSWORD>' ./setup.sh        # 끝. 비번 자동입력으로 NAS에 전송 시작
```

> `-bash: ./setup.sh: Permission denied` 가 뜨면 클론/복사 과정에서 실행 비트가
> 떨어진 것입니다. 다음 중 하나로 해결하세요 (setup.sh가 이후 나머지 스크립트
> 권한은 자동 복구합니다):
> ```bash
> bash setup.sh --host <이름>          # 실행 비트 없이 바로 실행, 또는
> chmod +x *.sh && ./setup.sh --host <이름>
> ```

`SSH_PASSWORD`를 안 주면 한 번 물어봅니다(이후 `config.json`에 저장, chmod 600).
기본 SSH 대상은 `synologynas@aidaslab.synology.me:2244`,
원격 경로는 `/volume1/nas-nfs/yunseok/ai-monitoring` 입니다.

자주 쓰는 옵션:

```bash
./setup.sh --host gpu7 --password '<NAS_PASSWORD>'        # 호스트(노드) 이름 지정
./setup.sh --host gpu7 --claude-dir /data/work/.claude   # .claude 폴더 직접 지정
./setup.sh --host gpu7 --claude-dir ~/.claude --claude-dir ~/.claude2  # 여러 개(반복)
./setup.sh --key ~/.ssh/id_ed25519                  # 비번 대신 SSH 키 사용(권장)
./setup.sh --ssh-host 10.8.0.1 --ssh-port 22        # SSH 대상 변경
./setup.sh --local --nas /mnt/nas/yunseok/ai-monitoring  # NAS가 마운트된 서버
./setup.sh --systemd                                # systemd 유닛 출력
```

> `--claude-dir`를 주면 자동탐지(`~/.claude*`) 대신 **지정한 폴더만** 수집합니다.
> 홈 밖(예: `CLAUDE_CONFIG_DIR=/data/x`)에 있어 자동탐지로 못 찾는 경우에 쓰세요.
> Codex도 `--codex-dir`로 동일하게 지정 가능. (이미 설치돼 `config.json`이 있으면
> 직접 `claude.config_dirs`를 편집 후 `bash stop.sh && bash start.sh`)

관리: `./status.sh` (상태/최근배치/로그) · `./stop.sh` · `./start.sh`

> **비밀번호보다 SSH 키를 권장**합니다. 한 번만:
> `ssh-keygen -t ed25519 && ssh-copy-id -p 2244 synologynas@aidaslab.synology.me`
> 후 `./setup.sh --key ~/.ssh/id_ed25519` 로 비번 없이 동작합니다.

---

## 동작 방식

매 `interval_seconds`(기본 300초=5분)마다:

1. `~/.claude*` / `~/.codex` 에서 **변경된 파일만** 증분 파싱 (size/mtime 스킵)
2. 계정·세션·토큰 사용량을 하나의 배치(JSON gzip)로 묶어 **로컬 outbox**에 기록
3. outbox의 배치들을 **SSH로 NAS에 전송**:
   `scp` 로 `up-<name>` 임시 업로드 → 원격 `mv` 로 `batch-<name>` 원자적 전환
   (중앙은 `batch-*` 만 읽으므로 반쪽 파일을 보지 않음)
4. 전송 성공한 배치는 로컬에서 삭제, 실패하면 **outbox에 남겨 다음 주기에 재시도**
   (NAS/네트워크 일시 장애에도 데이터 유실 없음). 원격의 오래된 배치는 자동 정리.

전송 모드는 `transport.mode`:
- **`ssh`** (기본): NAS 미마운트 서버 → `scp`로 업로드, 비번 자동입력 또는 키
- **`local`**: NAS가 마운트된 서버 → 마운트 경로에 직접 기록

### Codex 토큰 매핑

`~/.codex/sessions/**/rollout-*.jsonl` 의 `token_count` 이벤트에서 턴별
사용량(`last_token_usage`)을 읽어 공용 4-요소 스키마로 환산:

| 공용 필드 | Codex 소스 |
|---|---|
| `cache_read_tokens` | `cached_input_tokens` |
| `input_tokens` | `input_tokens − cached_input_tokens` |
| `output_tokens` | `output_tokens` (reasoning 포함) |
| `cache_creation_tokens` | 0 |

→ `total = input + output + cache_read = codex total_tokens`. 계정 식별(email/plan)은
`~/.codex/auth.json`의 id_token(JWT)에서 **email·plan만** 추출하며,
**토큰/비밀번호/대화 본문은 NAS로 전송하지 않습니다.**

---

## 설정 (`config.json`, setup.sh가 생성·chmod 600)

```json
{
  "node_id": "gpu7",
  "interval_seconds": 300,
  "retain_hours": 72,
  "compress": true,
  "nas_root": "/mnt/nas/yunseok/ai-monitoring",
  "transport": {
    "mode": "ssh",
    "ssh_host": "aidaslab.synology.me",
    "ssh_port": 2244,
    "ssh_user": "synologynas",
    "ssh_password": "********",
    "ssh_key": "",
    "remote_root": "/volume1/nas-nfs/yunseok/ai-monitoring"
  },
  "claude": { "enabled": true, "config_dirs": [] },
  "codex":  { "enabled": true, "dirs": ["~/.codex"], "include_archived": false }
}
```

- `config_dirs`가 비어 있으면 `~/.claude`, `~/.claude2` … 자동 탐지.
- 계정 프로필을 분리했다면 자동탐지 대신 `config_dirs`에 각 디렉터리를 명시하는
  편이 안전합니다. 기본 `~/.claude`만 기존 `~/.claude.json` 로그인 정보를 읽고,
  별도 `CLAUDE_CONFIG_DIR`(예: `~/.ys-claude`)는 **자기 디렉터리 안의**
  `.claude.json`이 있어야 계정을 식별합니다. 별도 프로필이 기본 계정 정보를
  빌려 쓰는 fallback은 하지 않습니다.
- Claude JSONL에는 계정이 없으므로 송신기는 파일별 byte cursor와 계정 정보를
  함께 저장합니다. 처음 발견한 기존 기록, 로그인 전환을 가로지르는 기록, 전환
  직후 첫 폴링은 `assumed`/무계정으로 전송되어 중앙에서 집계하지 않습니다.
  전환 후 새 세션을 시작하면 이후에 추가된 바이트부터 안전하게 귀속됩니다.
- `ssh_key`를 지정하면 비밀번호 대신 키 인증(BatchMode).
- `remote_root`는 **NAS상의 절대경로**(= 중앙 마운트 `/mnt/nas/yunseok/ai-monitoring`).
- Codex 과거 전체를 한 번 백필하려면 `codex.include_archived: true`.

---

## 보안 / 개인정보

- NAS로 가는 것은 **사용량 메타데이터 + 계정 email/plan** 뿐. 토큰·키·대화 본문 없음.
- `config.json`(비밀번호 포함)은 `chmod 600` + `.gitignore`. 키 인증이 더 안전합니다.
- 첫 접속 시 NAS 호스트키를 `data/known_hosts`에 저장(이후 검증).

---

## 파일 구조

```
ai-monitoring-send/
  setup.sh            # ★ 하나만 실행
  start.sh stop.sh status.sh
  config.example.json
  sender/
    main.py            # 수집 루프 + outbox + 전송
    claude_collector.py
    codex_collector.py
    nas_writer.py      # 배치 gzip 생성
    transport.py       # local / ssh 전송 (원자적 업로드 + 보존정리)
    sshcmd.py          # pty 기반 ssh/scp 비밀번호 자동입력
  data/                # outbox/상태/로그/known_hosts (gitignore)
```
