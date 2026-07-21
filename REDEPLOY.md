# ai-monitoring-send 재배포 가이드

원격 GPU 서버에서 sender 코드를 갱신하고 재시작하는 절차. 2026-07-21 수집 로직
수정(아래 "이번 변경") 이후 각 활성 서버에 반영해야 합니다.

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
