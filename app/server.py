#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 本机 HTTP 服务（纯标准库）

跑起来：
    python3 app/server.py            # 默认 http://127.0.0.1:8765
    python3 app/server.py 9000       # 换端口

接口：
    GET  /                 两屏页面
    GET  /api/health       拉一次麦当劳工具列表，确认 token 活着
    GET  /api/addresses    读账号里存的外送地址（只读）
    POST /api/plan         填表 → 两套账

设计取舍：
  * 只绑 127.0.0.1，不对外网开放。token 是你自己的，别让局域网里的人用。
  * 不用框架。这台机器上有没有装 Flask 都不该影响能不能跑起来。
"""

import json
import os
import sys
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine      # noqa: E402
import mcpc        # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("AFTERM_PORT", "8765"))

# 一次只算一单：计算过程会改 engine 的模块级查询上下文（预约时间），
# 本机单人使用，串行化比让两次计算互相污染更划算。
_PLAN_LOCK = __import__("threading").Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "AfterM/1.0"

    # -- 输出 --------------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def log_message(self, fmt, *args):
        # 只留一行摘要，别把 token 刷进终端
        sys.stderr.write("  %s %s\n" % (self.command, self.path.split("?")[0]))

    # -- 路由 --------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            p = os.path.join(HERE, "index.html")
            if not os.path.exists(p):
                return self._send(500, "缺 index.html", "text/plain; charset=utf-8")
            with open(p, "r", encoding="utf-8") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        if path == "/api/health":
            return self._health()

        if path == "/api/addresses":
            try:
                j = mcpc.mcd_call("delivery-query-addresses")
                ads = (j.get("data") or {}).get("addresses") or []
                return self._json({"ok": True, "addresses": [
                    {"id": a.get("addressId"), "text": a.get("fullAddress")} for a in ads]})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]}, 500)

        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path.split("?")[0] != "/api/plan":
            return self._send(404, "not found", "text/plain; charset=utf-8")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return self._json({"ok": False, "warnings": ["请求体不是合法 JSON"]}, 400)

        try:
            with _PLAN_LOCK:
                res = engine.compute(req)
            return self._json(res)
        except Exception as e:
            traceback.print_exc()
            return self._json({"ok": False, "warnings": ["计算过程出错：%s" % str(e)[:300]]}, 500)

    # -- 健康检查 ----------------------------------------------------------
    def _health(self):
        info = {"ok": True, "amap": bool(mcpc.amap_key()), "mcd": False}
        try:
            mcpc.mcd().initialize()
            info["tools"] = len(mcpc.mcd().tools())
            info["mcd"] = True
        except Exception as e:
            info["ok"] = False
            info["mcd_error"] = str(e)[:200]
        return self._json(info)


def main():
    if not mcpc.amap_key():
        print("  ! 没找到高德 key：交通这一段会跳过，价格和套餐照常算。")
        print("    想补齐：export AMAP_KEY=你的key   或   放进 ~/.zcode/amap.key")

    url, auth = mcpc._mcd_conf()
    if not auth:
        print("  ! ~/.workbuddy/mcp.json 里没有麦当劳 token，先把它配好。")

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True

    print("")
    print("  麦麦散场局  AfterM")
    print("  ─────────────────────────────────────")
    print("  打开这一个地址：  http://127.0.0.1:%d" % PORT)
    print("  按 Ctrl+C 停")
    print("")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")


if __name__ == "__main__":
    main()
