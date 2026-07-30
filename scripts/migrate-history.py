#!/usr/bin/env python3
"""기존 ~/.codex · ~/.claude 의 대화 이력을 랩 계정 디렉토리로 옮깁니다.

계정 분리(setup-accounts.py) 이후 과거 대화는 옛 디렉토리에 남아 `resume` 목록에
안 뜹니다. 그 이력을 랩 디렉토리로 넘겨 이어서 쓸 수 있게 합니다.

    python3 scripts/migrate-history.py --to lab1 --dry-run   # 무엇이 옮겨지는지만
    python3 scripts/migrate-history.py --to lab1             # 복사 (원본 유지)
    python3 scripts/migrate-history.py --to lab1 --move      # 이동 (원본 비움)
    python3 scripts/migrate-history.py --to lab1 --only codex
    python3 scripts/migrate-history.py --to lab1 --extras    # 설정·skills·plugins 도

안전장치
  * 원본의 로그인 계정과 목표 랩 계정이 다르면 **중단**합니다. 개인 계정 이력을
    랩으로 옮기면 사용량이 잘못된 사람에게 귀속됩니다 (--force 로만 강행).
  * auth.json / .credentials.json 은 절대 건드리지 않습니다. 계정 자체는 각
    디렉토리의 로그인으로 정해져야 합니다.
  * 같은 파일이 이미 있으면 건너뜁니다 (멱등). --move 는 같은 파일시스템이면
    rename 이라 추가 용량이 들지 않습니다.

옮겨도 **과거 사용량이 새로 집계되지는 않습니다**: 수집기는 처음 읽는 파일의
기록을 계정 미확정(assumed)으로 버리고, 대시보드는 tracking.count_from_ms 이후만
셉니다. 목적은 `resume` 로 이어 쓰는 것입니다.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys

HOME = os.path.expanduser("~")

# (하위경로, 설명). 대화 이력과 그것을 찾는 데 필요한 색인만 기본으로 옮깁니다.
CODEX_CORE = [("sessions", "대화 기록"),
              ("archived_sessions", "보관된 대화"),
              ("session_index.jsonl", "thread 이름 색인"),
              ("history.jsonl", "프롬프트 이력")]
CODEX_EXTRA = [("config.toml", "설정"), ("skills", "skills"), ("plugins", "plugins")]

CLAUDE_CORE = [("projects", "대화 기록 (작업경로별)"),
               ("history.jsonl", "프롬프트 이력"),
               ("todos", "todo 상태"),
               ("tasks", "task 상태")]
CLAUDE_EXTRA = [("file-history", "파일 편집 이력"), ("plugins", "plugins"),
                ("settings.json", "설정"), ("CLAUDE.md", "사용자 지침")]

# 절대 옮기지 않는 것 — 계정 정체성, 재생성 가능한 캐시, 실행 바이너리
NEVER = {"auth.json", ".credentials.json", "packages", "cache", "tmp",
         "installation_id", "models_cache.json", "statsig", "telemetry"}

# JSONL 색인은 파일을 덮어쓰지 말고 항목 단위로 합칩니다. (파일명, 중복 판정 키)
MERGE_JSONL = {"session_index.jsonl": "id", "history.jsonl": None}


def codex_account(d):
    """~/.codex* 의 로그인 계정 (auth.json 의 id_token JWT 에서)."""
    try:
        tok = ((json.load(open(os.path.join(d, "auth.json"),
                               encoding="utf-8")).get("tokens") or {}).get("id_token") or "")
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("email")
    except (OSError, ValueError, IndexError, KeyError):
        return None


def claude_account(d):
    """~/.claude* 의 로그인 계정. oauthAccount 위치는 설치 형태마다 다릅니다."""
    for p in (os.path.join(d, ".claude.json"), f"{d}.json",
              os.path.join(d, "claude.json")):
        try:
            acct = (json.load(open(p, encoding="utf-8")).get("oauthAccount") or {})
        except (OSError, ValueError):
            continue
        if acct.get("emailAddress"):
            return acct["emailAddress"]
    return None


def merge_jsonl(src, dst, key, move, dry, log):
    """색인 파일을 항목 단위로 합친다. 덮어쓰면 목표의 기존 항목이 사라진다."""
    def load(p):
        out = []
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip():
                        out.append(line.rstrip("\n"))
        except OSError:
            pass
        return out

    have = load(dst)
    seen = set()
    if key:
        for line in have:
            try:
                seen.add(json.loads(line).get(key))
            except ValueError:
                pass
    else:
        seen = set(have)

    add = []
    for line in load(src):
        if key:
            try:
                k = json.loads(line).get(key)
            except ValueError:
                continue
        else:
            k = line
        if k not in seen:
            seen.add(k)
            add.append(line)
    if not add:
        log(f"  = 이미 최신  {os.path.basename(src)}")
        return 0
    log(f"  + 항목 {len(add)}개 추가  {os.path.basename(src)}")
    if not dry:
        with open(dst, "a", encoding="utf-8") as f:
            for line in add:
                f.write(line + "\n")
        if move:
            os.remove(src)
    return len(add)


def copy_tree(src, dst, move, dry, log):
    """파일 단위로 옮긴다. 이미 있는 동일 파일(크기+mtime)은 건너뛴다."""
    moved = skipped = 0
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        for name in files:
            s = os.path.join(root, name)
            t = os.path.join(target_dir, name)
            try:
                ss = os.stat(s)
            except OSError:
                continue
            if os.path.exists(t):
                ts = os.stat(t)
                if ts.st_size == ss.st_size and int(ts.st_mtime) == int(ss.st_mtime):
                    skipped += 1
                    continue
            if not dry:
                os.makedirs(target_dir, exist_ok=True)
                if move:
                    try:
                        os.replace(s, t)       # 같은 fs 면 즉시, 추가 용량 0
                    except OSError:
                        shutil.copy2(s, t)
                        os.remove(s)
                else:
                    shutil.copy2(s, t)
            moved += 1
    log(f"  {'+ ' if moved else '= '}{moved}개 {'이동' if move else '복사'}"
        + (f", {skipped}개 이미 있음" if skipped else ""))
    return moved


def migrate(kind, src, dst, items, move, dry, log):
    total = 0
    for name, desc in items:
        s = os.path.join(src, name)
        if name in NEVER or not os.path.exists(s):
            continue
        log(f"  [{desc}] {name}")
        t = os.path.join(dst, name)
        if name in MERGE_JSONL and os.path.isfile(s):
            total += merge_jsonl(s, t, MERGE_JSONL[name], move, dry, log)
        elif os.path.isdir(s):
            total += copy_tree(s, t, move, dry, log)
        else:
            if os.path.exists(t) and os.stat(t).st_size == os.stat(s).st_size:
                log("  = 이미 있음")
                continue
            if not dry:
                os.makedirs(dst, exist_ok=True)
                shutil.copy2(s, t)
                if move:
                    os.remove(s)
            log("  + 1개 " + ("이동" if move else "복사"))
            total += 1
    return total


def du(path):
    n = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                n += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="기존 이력을 랩 계정 디렉토리로 이관")
    ap.add_argument("--to", required=True, metavar="LAB", help="목표 랩 이름 (예: lab1)")
    ap.add_argument("--move", action="store_true", help="이동 (기본은 복사)")
    ap.add_argument("--only", choices=("codex", "claude"), help="한쪽만")
    ap.add_argument("--extras", action="store_true", help="설정·skills·plugins 도 포함")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="계정이 달라도 강행 (사용량 귀속이 틀어집니다)")
    a = ap.parse_args(argv)

    plans = []
    if a.only in (None, "codex"):
        plans.append(("codex", os.path.join(HOME, ".codex"),
                      os.path.join(HOME, f".codex-{a.to}"), codex_account,
                      CODEX_CORE + (CODEX_EXTRA if a.extras else [])))
    if a.only in (None, "claude"):
        plans.append(("claude", os.path.join(HOME, ".claude"),
                      os.path.join(HOME, f".claude-{a.to}"), claude_account,
                      CLAUDE_CORE + (CLAUDE_EXTRA if a.extras else [])))

    print(f"{'[DRY-RUN] ' if a.dry_run else ''}"
          f"이력 이관 → {a.to}  ({'이동' if a.move else '복사'})")

    lines, problems, planned = [], [], 0
    for kind, src, dst, acct_fn, items in plans:
        lines.append(f"\n── {kind}: {src.replace(HOME, '~')} → {dst.replace(HOME, '~')}")
        if not os.path.isdir(src):
            lines.append("  원본 없음 — 건너뜀")
            continue
        if not os.path.isdir(dst):
            problems.append(f"{dst} 가 없습니다. setup-accounts.py 를 먼저 실행하세요.")
            lines.append("  목표 디렉토리 없음 — 건너뜀")
            continue
        sa, da = acct_fn(src), acct_fn(dst)
        lines.append(f"  원본 계정 {sa or '(불명)'}   목표 계정 {da or '(로그인 안 됨)'}")
        if not da:
            problems.append(f"{dst} 에 아직 로그인하지 않았습니다. 먼저 로그인하세요 "
                            f"(그러면 계정 일치를 검사할 수 있습니다).")
            lines.append("  ! 목표가 로그인 안 됨 — 건너뜀")
            continue
        if sa != da and not a.force:
            problems.append(
                f"{kind}: 원본({sa or '불명'})과 목표({da}) 계정이 다릅니다. "
                f"옮기면 그 이력이 {da} 사용량으로 귀속됩니다. 정말 옮기려면 --force.")
            lines.append("  ! 계정 불일치 — 건너뜀")
            continue
        if not a.move and not a.dry_run:
            need = du(src)
            free = shutil.disk_usage(dst).free
            lines.append(f"  복사 용량 {need / 2**20:,.0f}MB / 여유 {free / 2**20:,.0f}MB")
            if need > free * 0.9:
                problems.append(f"{kind}: 여유 공간이 부족합니다. --move 를 쓰면 "
                                f"같은 파일시스템에서 추가 용량 없이 옮깁니다.")
                lines.append("  ! 공간 부족 — 건너뜀")
                continue
        planned += migrate(kind, src, dst, items, a.move, a.dry_run, lines.append)

    print("\n".join(lines))
    print("\n" + "=" * 68)
    for p in problems:
        print(f"  ⚠ {p}")
    if a.dry_run:
        print(f"\n{planned}건 예정 — 실제로 적용하려면 --dry-run 없이 실행하세요.")
    elif planned:
        print(f"\n{planned}건 처리 완료.")
        print("  확인: CODEX_HOME=~/.codex-%s codex resume   (목록에 뜨는지)" % a.to)
        print("        CLAUDE_CONFIG_DIR=~/.claude-%s claude --resume" % a.to)
        print("  과거 사용량은 집계되지 않습니다 (처음 읽는 기록은 계정 미확정으로 버려짐).")
    else:
        print("\n변경 없음.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
