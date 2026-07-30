#!/usr/bin/env python3
"""VSCode 사이드바(Claude Code / Codex 확장)가 쓸 계정 선택.

터미널은 lab1/lab2 셸 함수로 계정을 고르지만, 확장은 셸을 거치지 않아 그 함수가
적용되지 않습니다. 두 확장의 사정이 서로 다릅니다.

  Claude : claudeCode.environmentVariables 라는 정식 설정이 있어 그걸 씁니다.
  Codex  : 계정 설정이 없습니다. chatgpt.cliExecutable 은 app-server 실행에
           쓰이지 않고(검증함), 확장은 자기 번들 바이너리를 직접 돌리며 계정은
           상속받은 CODEX_HOME(없으면 ~/.codex)으로만 정해집니다. 확장 호스트에
           그 변수를 넣을 방법이 없어(이 서버 빌드는 server-env-setup 미지원),
           번들 바이너리 자리에 CODEX_HOME 을 고정한 래퍼를 두고 원본은
           codex.real 로 옮깁니다.

    python3 scripts/sidebar-account.py --show
    python3 scripts/sidebar-account.py --lab lab1
    python3 scripts/sidebar-account.py --personal
    python3 scripts/sidebar-account.py --lab lab2 --claude-only

바꾼 뒤에는 VSCode 창을 리로드해야 적용됩니다(서버 재시작은 불필요).
※ Codex 확장을 업데이트하면 래퍼가 덮어써져 조용히 개인 계정으로 돌아갑니다.
   이 스크립트를 다시 실행하면 복구되고, check-routing.py 가 그 상태를 탐지합니다.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import stat
import sys
import time

HOME = os.path.expanduser("~")

# VSCode 계열은 설치 형태마다 경로가 다릅니다. Remote-SSH/터널 서버, 로컬 설치,
# Insiders, code-server 를 모두 훑어 실제로 있는 것을 씁니다. 하나도 없으면
# 그 서버는 사이드바를 안 쓰는 것이므로 이 스크립트가 할 일이 없습니다.
_ROOTS = (
    (".vscode-server", ("data", "Machine", "settings.json"), ("extensions",)),
    (".vscode-server-insiders", ("data", "Machine", "settings.json"), ("extensions",)),
    (".vscode", ("data", "Machine", "settings.json"), ("extensions",)),
    (os.path.join(".local", "share", "code-server"),
     ("Machine", "settings.json"), ("extensions",)),
)


def _roots():
    return [(os.path.join(HOME, r), s, e) for r, s, e in _ROOTS
            if os.path.isdir(os.path.join(HOME, r))]


def settings_path():
    """머신 설정 파일 경로. 설치가 여러 개면 확장이 실제로 있는 쪽을 우선한다."""
    roots = _roots()
    for root, s, e in roots:
        if glob.glob(os.path.join(root, *e, "anthropic.claude-code-*")) or \
           glob.glob(os.path.join(root, *e, "openai.chatgpt-*")):
            return os.path.join(root, *s)
    return os.path.join(roots[0][0], *roots[0][1]) if roots else None


SETTINGS = settings_path() or os.path.join(
    HOME, ".vscode-server", "data", "Machine", "settings.json")
CLAUDE_KEY = "claudeCode.environmentVariables"
DEAD_KEY = "chatgpt.cliExecutable"  # app-server 실행에 안 쓰임 — 있으면 지운다

WRAPPER = """#!/bin/sh
# AIDAS: VSCode Codex 확장용 계정 고정 래퍼.
#
# 확장은 chatgpt.cliExecutable 을 app-server 실행에 쓰지 않고 이 번들 바이너리를
# 직접 실행하며, 계정은 상속받은 CODEX_HOME(없으면 ~/.codex)으로만 정해집니다.
# 확장 호스트에는 그 변수가 없어 항상 개인 계정으로 떨어지므로 여기서 고정합니다.
# 터미널의 codex / lab1 / lab2 에는 영향이 없습니다.
#
# 원본은 같은 디렉토리의 codex.real 입니다. 확장을 업데이트하면 이 래퍼가
# 사라지므로 sidebar-account.py 로 재적용하세요.
exec env CODEX_HOME="{home}" "$(dirname "$0")/codex.real" "$@"
"""


# ------------------------------------------------------------ VSCode settings
def load_settings():
    if not os.path.exists(SETTINGS):
        return {}, False
    raw = open(SETTINGS, encoding="utf-8").read()
    try:
        # 주석(JSONC)을 걷어내고 읽는다. 문자열 안의 // 는 이 설정 파일에 없다.
        return (json.loads(re.sub(r"//.*", "", raw)) or {}), ("//" in raw)
    except ValueError:
        print(f"오류: {SETTINGS} 를 파싱할 수 없습니다. 직접 고쳐주세요.", file=sys.stderr)
        sys.exit(1)


def save_settings(cfg, had_comments):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, f"{SETTINGS}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        if had_comments:
            print("  ⚠ 기존 주석은 사라집니다 (원본은 .bak 로 보관)")
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --------------------------------------------------------- codex 확장 바이너리
def ext_binaries():
    """Codex 확장의 번들 codex 바이너리 경로들 (보통 1개)."""
    out = []
    for root, _s, e in _roots():
        for ext in sorted(glob.glob(os.path.join(root, *e, "openai.chatgpt-*"))):
            out += glob.glob(os.path.join(ext, "bin", "*", "codex"))
    return out


def wrapper_target(binp):
    """이 바이너리가 래퍼면 가리키는 CODEX_HOME, 아니면 None."""
    try:
        head = open(binp, "rb").read(2048)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    for line in head.decode("utf-8", "replace").splitlines():
        if "CODEX_HOME=" in line:
            return line.split("CODEX_HOME=", 1)[1].split()[0].strip("\"'")
    return None


def wrap(binp, codex_home):
    real = f"{binp}.real"
    cur = wrapper_target(binp)
    if cur == codex_home:
        print(f"  = 이미 최신  {binp.replace(HOME, '~')}")
        return False
    if cur is None:                      # 아직 원본 바이너리
        if os.path.exists(real):
            print(f"오류: {real} 가 이미 있는데 {binp} 는 원본입니다. 직접 정리하세요.",
                  file=sys.stderr)
            sys.exit(1)
        os.rename(binp, real)            # 같은 파일시스템 → 즉시, 추가 용량 0
    with open(binp, "w", encoding="utf-8") as f:
        f.write(WRAPPER.format(home=codex_home))
    os.chmod(binp, os.stat(binp).st_mode | stat.S_IRWXU | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  + 래퍼      {binp.replace(HOME, '~')} → {codex_home.replace(HOME, '~')}")
    return True


def unwrap(binp):
    real = f"{binp}.real"
    if wrapper_target(binp) is None:
        print(f"  = 이미 원본  {binp.replace(HOME, '~')}")
        return False
    if not os.path.exists(real):
        print(f"오류: 원본 {real} 이 없습니다. 확장을 재설치하세요.", file=sys.stderr)
        sys.exit(1)
    os.replace(real, binp)
    print(f"  - 래퍼 해제  {binp.replace(HOME, '~')}")
    return True


def share_packages(codex_home):
    """standalone codex 캐시를 개인 홈과 공유해 재다운로드(≈350MB)를 막는다.

    packages/ 는 자격증명 없는 실행 바이너리 캐시라 계정 간 공유해도 안전합니다.
    """
    src = os.path.join(HOME, ".codex", "packages")
    dst = os.path.join(codex_home, "packages")
    if not os.path.isdir(src) or os.path.realpath(src) == os.path.realpath(dst):
        return
    if os.path.exists(dst) and not os.path.islink(dst):
        return                            # 이미 실제 디렉토리면 건드리지 않는다
    if os.path.islink(dst) and os.readlink(dst) == src:
        return
    if os.path.islink(dst):
        os.unlink(dst)
    os.symlink(src, dst)
    print(f"  + 캐시 공유  {dst.replace(HOME, '~')} → {src.replace(HOME, '~')}")


# ------------------------------------------------------------------- describe
def describe(cfg):
    claude = (cfg.get(CLAUDE_KEY) or {}).get("CLAUDE_CONFIG_DIR")
    print(f"  Claude 사이드바 : {claude or '설정 없음 → ~/.claude (개인)'}")
    bins = ext_binaries()
    if not bins:
        print("  Codex  사이드바 : 확장이 설치돼 있지 않음")
    for b in bins:
        tgt = wrapper_target(b)
        print(f"  Codex  사이드바 : {tgt or '래퍼 없음 → ~/.codex (개인)'}")


def main(argv=None):
    p = argparse.ArgumentParser(description="VSCode 사이드바 계정 선택")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--lab", help="사용할 랩 이름 (예: lab1)")
    g.add_argument("--personal", action="store_true", help="개인 계정으로 되돌림")
    g.add_argument("--show", action="store_true", help="현재 설정만 출력")
    p.add_argument("--claude-only", action="store_true")
    p.add_argument("--codex-only", action="store_true")
    a = p.parse_args(argv)

    cfg, had_comments = load_settings()
    if a.show:
        print("현재 VSCode 사이드바 계정:")
        describe(cfg)
        return 0

    do_claude, do_codex = not a.codex_only, not a.claude_only
    changed = False

    if a.personal:
        if do_claude and cfg.pop(CLAUDE_KEY, None) is not None:
            changed = True
            print("  - Claude 설정 제거 → ~/.claude")
        if do_codex:
            for b in ext_binaries():
                changed |= unwrap(b)
    else:
        cdir = os.path.join(HOME, f".claude-{a.lab}")
        xdir = os.path.join(HOME, f".codex-{a.lab}")
        if do_claude:
            if not os.path.isdir(cdir):
                print(f"오류: {cdir} 가 없습니다 (setup-accounts.py 를 먼저 실행하세요).",
                      file=sys.stderr)
                return 1
            if cfg.get(CLAUDE_KEY) != {"CLAUDE_CONFIG_DIR": cdir}:
                cfg[CLAUDE_KEY] = {"CLAUDE_CONFIG_DIR": cdir}
                changed = True
                print(f"  + Claude 설정 → {cdir.replace(HOME, '~')}")
        if do_codex:
            if not os.path.isdir(xdir):
                print(f"오류: {xdir} 가 없습니다 (setup-accounts.py 를 먼저 실행하세요).",
                      file=sys.stderr)
                return 1
            bins = ext_binaries()
            if not bins:
                print("  ! Codex 확장이 설치돼 있지 않아 건너뜁니다")
            for b in bins:
                changed |= wrap(b, xdir)
            if bins:
                share_packages(xdir)

    if cfg.pop(DEAD_KEY, None) is not None:
        changed = True
        print(f"  - {DEAD_KEY} 제거 (app-server 실행에 쓰이지 않음)")
    if changed:
        save_settings(cfg, had_comments)

    print()
    describe(cfg)
    print("\n→ VSCode 창을 리로드하면 적용됩니다 (서버 재시작 불필요)." if changed
          else "\n변경 없음.")
    print("  터미널의 claude / codex / lab1 / lab2 에는 영향이 없습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
