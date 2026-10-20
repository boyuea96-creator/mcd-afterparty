#!/usr/bin/env python3
"""跑真实计算，把三份结果存成快照，供 tools/build_demo.py 烤演示页。

    python3 tools/snap.py            # 跑全部
    python3 tools/snap.py A C        # 只跑指定场景

只读：全程只用 query-* 和 calculate-price，不落单、不建地址。
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

import engine  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "demo", "snap")

# 三个场景都用真存过的外送地址（App 里已有），所以 to_addr 直接对得上
SCENES = {
    "A": {
        "label": "杭州 · 大莲花演唱会散场",
        "req": {
            "from_addr": "浙江省杭州市滨江区飞虹路3号",
            "to_addr": "浙江省杭州市滨江区飞虹路208号",
            "when": "2026-10-24 21:15",
            "people": 3,
            "form": "undecided",
            "taste": "",
            "budget": "",
            "rel": "同学",
            "tired": "适中",
        },
    },
    "B": {
        "label": "武汉 · 江汉路步行街散场",
        "req": {
            "from_addr": "湖北省武汉市江汉区江汉路步行街",
            "to_addr": "湖北省武汉市江岸区黎黄陂路",
            "when": "2026-10-23 22:30",
            "people": 4,
            "form": "dinein",
            "taste": "想吃麦辣鸡翅 / 有人不吃冰的冷的",
            "budget": "",
            "rel": "同学",
            "tired": "疲劳",
        },
    },
    "C": {
        "label": "上海 · 陆家嘴峰会散会",
        "req": {
            "from_addr": "上海市浦东新区滨江大道2727号",
            "to_addr": "上海市浦东新区富城路33号",
            "when": "2026-10-22 17:00",
            "people": 6,
            "form": "delivery",
            "taste": "",
            "budget": "30",
            "rel": "同事",
            "tired": "疲劳",
        },
    },
}


def run(sid):
    sc = SCENES[sid]
    print("\n== %s  %s" % (sid, sc["label"]))
    print("   %s" % json.dumps(sc["req"], ensure_ascii=False))
    t0 = time.time()
    out = engine.compute(dict(sc["req"]))
    took = time.time() - t0
    ok = {k: bool((v or {}).get("ok")) for k, v in (out.get("panes") or {}).items()}
    print("   %.1f 秒   panes=%s  calls=%s" % (took, ok, out.get("calls")))
    if out.get("warnings"):
        for w in out["warnings"]:
            print("   ! %s" % w)
    for k, v in (out.get("panes") or {}).items():
        b = (v or {}).get("best")
        if b:
            print("   %-8s %s  %s" % (k, b.get("price"), (b.get("names") or b.get("title") or "")[:70]))
    os.makedirs(SNAP_DIR, exist_ok=True)
    p = os.path.join(SNAP_DIR, sid + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("   -> %s  %.0f KB" % (p, os.path.getsize(p) / 1024.0))
    return out


if __name__ == "__main__":
    want = [a.upper() for a in sys.argv[1:]] or list(SCENES)
    for sid in want:
        run(sid)
