#!/usr/bin/env python3
"""계정별 디렉토리 리팩토링 — 배포용.

한 서버에서 랩 계정과 개인 계정을 섞어 쓰면 사용량 귀속이 오염됩니다
(같은 설정 디렉토리 안에서 로그아웃/로그인하면 과거 기록이 현재 계정으로
재라벨됨). 이 스크립트는 "디렉토리 하나 = 계정 하나" 구조를 만들어 그 문제를
구조적으로 없애고, sender 가 랩 디렉토리만 수집하도록 설정을 맞춥니다.

만드는 것:
  ~/.claude-<lab>  ~/.codex-<lab>      계정별 설정 디렉토리 (비어 있음; 로그인은 사람이)
  ~/.local/bin/codex-<lab>             VSCode Codex 확장용 래퍼 (CODEX_HOME 고정)
  ~/.bashrc 의 lab1/lab2 함수          터미널에서 계정 전환
  VSCode Machine settings.json         사이드바가 쓸 계정 지정
  sender config.json                   claude.config_dirs / codex.dirs 를 랩 디렉토리로

건드리지 않는 것:
  ~/.claude, ~/.codex (개인 계정 경로) — 기존 기록·로그인 그대로 둡니다.
  로그인 — 대화형이라 사람이 직접 해야 합니다. 마지막에 명령을 출력합니다.

    python3 scripts/setup-accounts.py --dry-run     # 무엇이 바뀌는지만 출력
    python3 scripts/setup-accounts.py               # 적용 (수정 전 .bak 백업)
    python3 scripts/setup-accounts.py --labs lab1   # 랩 계정 1개만
    python3 scripts/setup-accounts.py --no-vscode   # VSCode 설정은 건너뜀
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import time

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASHRC = os.path.join(HOME, ".bashrc")
BEGIN = "# >>> aidas lab account switcher >>>"
END = "# <<< aidas lab account switcher <<<"
VSCODE_SETTINGS = os.path.join(HOME, ".vscode-server", "data", "Machine", "settings.json")

changes: list[str] = []
warnings: list[str] = []


def say(msg):
    print(msg)


def backup(path):
    """Copy path aside once per run so a re-run never destroys prior state."""
    if os.path.exists(path):
        dst = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, dst)
        return dst
    return None


def which(name):
    for d in (os.path.join(HOME, ".local", "bin"), "/usr/local/bin", "/usr/bin"):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return shutil.which(name)


# ---------------------------------------------------------------- directories
def make_dirs(labs, dry):
    for lab in labs:
        for base in (".claude-", ".codex-"):
            d = os.path.join(HOME, base + lab)
            if os.path.isdir(d):
                say(f"  = 이미 있음  {d}")
                continue
            changes.append(f"mkdir {d}")
            if not dry:
                os.makedirs(d, mode=0o700, exist_ok=True)
            say(f"  + 생성      {d}")


# ------------------------------------------------------------------- wrappers
WRAPPER = """#!/bin/sh
# Codex CLI 래퍼 — {lab} 계정(~/.codex-{lab})으로 고정 실행.
# VSCode Codex 확장은 계정 지정 설정이 없고 process.env.CODEX_HOME 만 보므로,
# 확장 설정 "chatgpt.cliExecutable" 에 이 경로를 지정해 사이드바 계정을 정합니다.
# 확장이 spawn 하는 프로세스에만 적용되어 터미널 codex/lab 함수에는 영향 없음.
exec env CODEX_HOME="$HOME/.codex-{lab}" {codex_bin} "$@"
"""


def make_wrappers(labs, codex_bin, dry):
    if not codex_bin:
        warnings.append("codex 실행 파일을 찾지 못해 래퍼를 만들지 못했습니다 "
                        "(--codex-bin 으로 지정하세요).")
        return {}
    out = {}
    bindir = os.path.join(HOME, ".local", "bin")
    if not dry:
        os.makedirs(bindir, exist_ok=True)
    for lab in labs:
        p = os.path.join(bindir, f"codex-{lab}")
        out[lab] = p
        body = WRAPPER.format(lab=lab, codex_bin=codex_bin)
        if os.path.exists(p) and open(p, encoding="utf-8").read() == body:
            say(f"  = 이미 최신  {p}")
            continue
        changes.append(f"write {p}")
        if not dry:
            backup(p)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        say(f"  + 래퍼      {p}")
    return out


# --------------------------------------------------------------------- bashrc
def bashrc_block(labs):
    lines = [BEGIN,
             "# 디렉토리 하나 = 계정 하나. export 하지 마세요(개인 작업이 랩으로 기록됨).",
             "#   lab1 claude / lab1 codex -> 랩1,   claude / codex -> 개인",
             "# `command` 를 쓰는 이유: claude() 같은 기존 함수 오버라이드를 우회해",
             "# 여기서 지정한 CLAUDE_CONFIG_DIR 가 덮어써지지 않게 하기 위함입니다."]
    for lab in labs:
        lines.append(
            f'{lab}() {{ CODEX_HOME=~/.codex-{lab} CLAUDE_CONFIG_DIR=~/.claude-{lab} command "$@"; }}')
    lines.append(END)
    return "\n".join(lines) + "\n"


def patch_bashrc(labs, dry):
    block = bashrc_block(labs)
    old = open(BASHRC, encoding="utf-8").read() if os.path.exists(BASHRC) else ""
    if BEGIN in old and END in old:
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", block, old, flags=re.S)
        verb = "갱신"
    else:
        # 마커 없이 손으로 넣었던 예전 블록이 있으면 걷어낸다. 우리가 만든 정확한
        # 형태(labN() { CODEX_HOME=... })와 그 머리말 주석만 지우므로 사용자가 쓴
        # 다른 설정은 건드리지 않는다. 안 지우면 정의가 중복된다.
        cleaned = re.sub(
            r"^\s*#\s*-+\s*AIDAS lab account switcher\s*-+\n(?:^\s*#.*\n)*", "",
            old, flags=re.M)
        cleaned = re.sub(
            r"^lab\w+\(\)\s*\{\s*CODEX_HOME=[^\n]*\}\s*\n", "", cleaned, flags=re.M)
        if cleaned != old:
            say("  ~ 기존 lab 함수 정의를 마커 블록으로 통합")
            old = cleaned
        new = old + ("\n" if old and not old.endswith("\n") else "") + "\n" + block
        verb = "추가"
    if new == old:
        say("  = 이미 최신  ~/.bashrc")
        return
    changes.append(f"{verb} ~/.bashrc")
    if not dry:
        b = backup(BASHRC)
        with open(BASHRC, "w", encoding="utf-8") as f:
            f.write(new)
        say(f"  + {verb}      ~/.bashrc  (백업: {os.path.basename(b) if b else '-'})")
    else:
        say(f"  + {verb}      ~/.bashrc")

    # 기존 오버라이드가 있으면 알려준다 — 로그인 명령이 엉뚱한 곳으로 갈 수 있음
    for m in re.finditer(r"^\s*(claude|codex)\s*\(\)\s*\{", old, flags=re.M):
        warnings.append(
            f"~/.bashrc 에 이미 {m.group(1)}() 함수 오버라이드가 있습니다. "
            f"로그인·수동 실행은 전체 경로로 하세요(그 함수가 환경변수를 덮어씁니다).")
        break


# --------------------------------------------------------------------- vscode
def patch_vscode(sidebar_lab, wrappers, dry):
    if not os.path.isdir(os.path.join(HOME, ".vscode-server")):
        say("  = VSCode 원격 서버 없음 — 건너뜀")
        return
    want = {"claudeCode.environmentVariables":
            {"CLAUDE_CONFIG_DIR": os.path.join(HOME, f".claude-{sidebar_lab}")}}
    if sidebar_lab in wrappers:
        want["chatgpt.cliExecutable"] = wrappers[sidebar_lab]

    cur, had_comments = {}, False
    if os.path.exists(VSCODE_SETTINGS):
        raw = open(VSCODE_SETTINGS, encoding="utf-8").read()
        had_comments = "//" in raw
        try:
            cur = json.loads(re.sub(r"//.*", "", raw)) or {}
        except ValueError:
            warnings.append(f"{VSCODE_SETTINGS} 를 파싱하지 못해 건드리지 않았습니다. "
                            f"수동으로 추가하세요: {json.dumps(want, ensure_ascii=False)}")
            return
    if all(cur.get(k) == v for k, v in want.items()):
        say(f"  = 이미 최신  {VSCODE_SETTINGS}")
        return
    merged = dict(cur)
    merged.update(want)
    changes.append(f"merge {VSCODE_SETTINGS}")
    if had_comments:
        warnings.append("VSCode settings.json 의 주석은 병합 과정에서 사라집니다 "
                        "(원본은 .bak 로 남습니다).")
    if not dry:
        os.makedirs(os.path.dirname(VSCODE_SETTINGS), exist_ok=True)
        backup(VSCODE_SETTINGS)
        with open(VSCODE_SETTINGS, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
            f.write("\n")
    say(f"  + 설정      {VSCODE_SETTINGS}  (사이드바 → {sidebar_lab})")


# --------------------------------------------------------------- sender config
def patch_sender_config(labs, dry):
    path = os.path.join(ROOT, "config.json")
    if not os.path.exists(path):
        warnings.append("sender config.json 이 아직 없습니다. setup.sh 로 만든 뒤 "
                        "이 스크립트를 다시 실행하면 수집 경로가 맞춰집니다.")
        return
    cfg = json.load(open(path, encoding="utf-8"))
    claude_dirs = [f"~/.claude-{l}" for l in labs]
    codex_dirs = [f"~/.codex-{l}" for l in labs]
    before = (cfg.get("claude", {}).get("config_dirs"), cfg.get("codex", {}).get("dirs"))
    cfg.setdefault("claude", {})["config_dirs"] = claude_dirs
    cfg.setdefault("codex", {})["dirs"] = codex_dirs
    if before == (claude_dirs, codex_dirs):
        say("  = 이미 최신  sender config.json")
        return
    changes.append("update sender config.json")
    if not dry:
        backup(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
    say(f"  + 수집 경로  claude={claude_dirs}  codex={codex_dirs}")


# ------------------------------------------------------------------------ main
def main(argv=None):
    p = argparse.ArgumentParser(description="계정별 디렉토리 리팩토링")
    p.add_argument("--labs", default="lab1,lab2",
                   help="랩 계정 이름들 (쉼표 구분, 기본 lab1,lab2)")
    p.add_argument("--sidebar", default=None,
                   help="VSCode 사이드바가 쓸 랩 (기본: 첫 번째)")
    p.add_argument("--claude-bin", default=None)
    p.add_argument("--codex-bin", default=None)
    p.add_argument("--no-vscode", action="store_true")
    p.add_argument("--no-bashrc", action="store_true")
    p.add_argument("--no-sender-config", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="변경 없이 계획만 출력")
    a = p.parse_args(argv)

    labs = [x.strip() for x in a.labs.split(",") if x.strip()]
    if not labs:
        print("--labs 가 비었습니다", file=sys.stderr)
        return 2
    sidebar = a.sidebar or labs[0]
    claude_bin = a.claude_bin or which("claude")
    codex_bin = a.codex_bin or which("codex")
    dry = a.dry_run

    say(f"{'[DRY-RUN] ' if dry else ''}계정별 디렉토리 리팩토링  labs={labs}  sidebar={sidebar}")
    say(f"  claude={claude_bin or '못 찾음'}   codex={codex_bin or '못 찾음'}")
    say("\n[1] 설정 디렉토리")
    make_dirs(labs, dry)
    say("\n[2] VSCode 확장용 codex 래퍼")
    wrappers = make_wrappers(labs, codex_bin, dry)
    if not a.no_bashrc:
        say("\n[3] 셸 함수 (~/.bashrc)")
        patch_bashrc(labs, dry)
    if not a.no_vscode:
        say("\n[4] VSCode 사이드바 계정")
        patch_vscode(sidebar, wrappers, dry)
    if not a.no_sender_config:
        say("\n[5] sender 수집 경로")
        patch_sender_config(labs, dry)

    say("\n" + "=" * 68)
    if warnings:
        say("확인 필요:")
        for w in warnings:
            say(f"  ⚠ {w}")
        say("")
    if dry:
        say(f"변경 예정 {len(changes)}건 — 실제 적용하려면 --dry-run 없이 실행하세요.")
        return 0
    say("남은 작업: 계정별 로그인 (대화형이라 직접 실행해야 합니다)")
    for lab in labs:
        say(f"\n  # {lab}")
        say(f"  CODEX_HOME=~/.codex-{lab} {codex_bin or 'codex'} login")
        say(f"  CLAUDE_CONFIG_DIR=~/.claude-{lab} {claude_bin or 'claude'} auth login")
    say("\n로그인 확인:")
    for lab in labs:
        say(f"  CODEX_HOME=~/.codex-{lab} {codex_bin or 'codex'} login status")
        say(f"  CLAUDE_CONFIG_DIR=~/.claude-{lab} {claude_bin or 'claude'} auth status")
    say("\n적용: 새 셸을 열거나 `source ~/.bashrc` → lab 함수 사용")
    say("      VSCode 는 창 리로드 후 사이드바가 지정한 계정으로 뜹니다")
    say("      sender 재시작: ./stop.sh && ./start.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
