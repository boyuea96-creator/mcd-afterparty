#!/usr/bin/env python3
"""把「工作副本」的 skill 同步进仓库，并顺手做脱敏。

为什么要这个脚本
----------------
skill 在这个项目里有两份：

  .workbuddy/skills/AfterM/    ← WorkBuddy 真正加载的那份，日常改的是它（不进仓库）
  skill/                       ← 仓库里那份，评审看的是它

两份不会自动同步。只改工作副本、忘了同步，仓库里就还是一份旧的 SKILL.md，
页面演示和文档全对、偏偏参赛主体是过期的 —— 这种错最难发现。

用法
----
  python3 tools/sync_skill.py           把工作副本同步进 skill/（会打印改了哪些文件）
  python3 tools/sync_skill.py --check    只检查，不同步；有差异时退出码 1

  git 的 pre-commit 钩子调的就是 --check，所以「改了没同步就提交」会被拦住。
"""

import argparse
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, ".workbuddy", "skills", "AfterM")
DST = os.path.join(ROOT, "skill")

# 仓库是公开的，个人住址门牌不能进去。只打这几处（公共地标如飞虹路3号/滨江大道2727号要保留，
# 那是案例里的真实场馆地址，本来就是公开信息）。
_SCRUB = [
    (re.compile(r"市心北路\d+号"), "市心北路×××号"),
    (re.compile(r"海陵南路\d+号"), "海陵南路×××号"),
    (re.compile(r"海陵北路\d+号"), "海陵北路×××号"),
    (re.compile(r"坡子街\d+号"), "坡子街×××号"),
    (re.compile(r"迎春东路\d+号"), "迎春东路×××号"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "（手机号已隐去）"),
]


def scrub(text):
    for rx, rep in _SCRUB:
        text = rx.sub(rep, text)
    return text


def pairs():
    """返回 [(源路径, 目标路径)]，保持目录结构。"""
    out = [(os.path.join(SRC, "SKILL.md"), os.path.join(DST, "SKILL.md"))]
    ref_dir = os.path.join(SRC, "references")
    if os.path.isdir(ref_dir):
        for name in sorted(os.listdir(ref_dir)):
            if name.endswith(".md"):
                out.append((os.path.join(ref_dir, name),
                            os.path.join(DST, "references", name)))
    return out


def main():
    ap = argparse.ArgumentParser(description="同步 skill 到仓库（含脱敏）")
    ap.add_argument("--check", action="store_true", help="只检查是否有差异，不写文件")
    args = ap.parse_args()

    if not os.path.isfile(os.path.join(SRC, "SKILL.md")):
        print("找不到工作副本：%s" % SRC, file=sys.stderr)
        print("（skill 可能放在别的 harness 目录下，改 SRC 常量）", file=sys.stderr)
        return 2

    drift, touched = [], []
    for src, dst in pairs():
        with open(src, encoding="utf-8") as f:
            want = scrub(f.read())
        old = None
        if os.path.isfile(dst):
            with open(dst, encoding="utf-8") as f:
                old = f.read()
        name = os.path.relpath(dst, ROOT)

        if old == want:
            continue
        drift.append(name)
        if not args.check:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(want)
            touched.append(name)

    if args.check:
        if drift:
            print("skill 与仓库副本不一致（改了工作副本但没同步）：")
            for n in drift:
                print("  - %s" % n)
            print("\n先跑一次: python3 tools/sync_skill.py")
            return 1
        print("skill 已同步：%d 个文件与工作副本一致" % len(pairs()))
        return 0

    if not drift:
        print("已是最新，没有文件需要同步。")
        return 0
    for n in touched:
        print("  写入 %s" % n)
    print("\n共同步 %d 个文件。接着 `git add -A && git commit && git push` 就生效。"
          % len(touched))
    return 0


if __name__ == "__main__":
    sys.exit(main())
