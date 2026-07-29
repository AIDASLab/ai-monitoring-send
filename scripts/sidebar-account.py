#!/usr/bin/env python3
"""VSCode 사이드바(Claude Code / Codex 확장)가 쓸 계정 선택.

터미널은 lab1/lab2 셸 함수로 계정을 고르지만, 확장은 셸을 거치지 않아 그 함수가
적용되지 않습니다. Claude 확장은 전용 설정이 있고, Codex 확장은 계정 설정이 없어
(process.env.CODEX_HOME 만 봄) CODEX_HOME 을 고정한 래퍼를 실행 파일로 지정합니다.

    python3 scripts/sidebar-account.py --show          # 현재 사이드바 계정
    python3 scripts/sidebar-account.py --lab lab1      # 랩1로
    python3 scripts/sidebar-account.py --personal      # 개인 계정으로 (설정 제거)
    python3 scripts/sidebar-account.py --lab lab2 --claude-only

바꾼 뒤에는 VSCode 창을 리로드해야 적용됩니다(서버 재시작은 불필요).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".vscode-server", "data", "Machine", "settings.json")
CLAUDE_KEY = "claudeCode.environmentVariables"
CODEX_KEY = "chatgpt.cliExecutable"


def load():
    if not os.path.exists(SETTINGS):
        return {}, False
    raw = open(SETTINGS, encoding="utf-8").read()
    try:
        return (json.loads(re.sub(r"//.*", "", raw)) or {}), ("//" in raw)
    except ValueError:
        print(f"오류: {SETTINGS} 를 파싱할 수 없습니다. 직접 고쳐주세요.", file=sys.stderr)
        sys.exit(1)


def save(cfg, had_comments):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, f"{SETTINGS}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        if had_comments:
            print("  ⚠ 기존 주석은 사라집니다 (원본은 .bak 로 보관)")
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def describe(cfg):
    claude = (cfg.get(CLAUDE_KEY) or {}).get("CLAUDE_CONFIG_DIR")
    codex_bin = cfg.get(CODEX_KEY)
    codex = None
    if codex_bin and os.path.exists(codex_bin):
        m = re.search(r'CODEX_HOME="?([^"\s]+)"?', open(codex_bin, encoding="utf-8").read())
        codex = m.group(1).replace("$HOME", HOME) if m else f"(래퍼: {codex_bin})"
    print(f"  Claude 사이드바 : {claude or '설정 없음 → ~/.claude (개인)'}")
    print(f"  Codex  사이드바 : {codex or '설정 없음 → ~/.codex (개인)'}")


def main(argv=None):
    p = argparse.ArgumentParser(description="VSCode 사이드바 계정 선택")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--lab", help="사용할 랩 이름 (예: lab1)")
    g.add_argument("--personal", action="store_true", help="개인 계정으로 되돌림")
    g.add_argument("--show", action="store_true", help="현재 설정만 출력")
    p.add_argument("--claude-only", action="store_true")
    p.add_argument("--codex-only", action="store_true")
    a = p.parse_args(argv)

    cfg, had_comments = load()
    if a.show:
        print("현재 VSCode 사이드바 계정:")
        describe(cfg)
        return 0

    do_claude = not a.codex_only
    do_codex = not a.claude_only

    if a.personal:
        removed = [k for k, do in ((CLAUDE_KEY, do_claude), (CODEX_KEY, do_codex))
                   if do and k in cfg]
        for k in removed:
            cfg.pop(k)
        if not removed:
            print("이미 개인 계정입니다.")
            return 0
        save(cfg, had_comments)
        print("사이드바를 개인 계정으로 되돌렸습니다:", ", ".join(removed))
    else:
        lab = a.lab
        cdir = os.path.join(HOME, f".claude-{lab}")
        wrapper = os.path.join(HOME, ".local", "bin", f"codex-{lab}")
        problems = []
        if do_claude and not os.path.isdir(cdir):
            problems.append(f"{cdir} 가 없습니다 (setup-accounts.py 를 먼저 실행하세요)")
        if do_codex and not os.path.exists(wrapper):
            problems.append(f"{wrapper} 래퍼가 없습니다 (setup-accounts.py 를 먼저 실행하세요)")
        if problems:
            for x in problems:
                print(f"오류: {x}", file=sys.stderr)
            return 1
        if do_claude:
            cfg[CLAUDE_KEY] = {"CLAUDE_CONFIG_DIR": cdir}
        if do_codex:
            cfg[CODEX_KEY] = wrapper
        save(cfg, had_comments)
        print(f"사이드바 계정을 {lab} 로 설정했습니다.")

    print()
    describe(cfg)
    print("\n→ VSCode 창을 리로드하면 적용됩니다 (서버 재시작 불필요).")
    print("  터미널의 claude / codex / lab1 / lab2 에는 영향이 없습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
