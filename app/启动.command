#!/bin/bash
# 双击这个文件就能跑起来：起服务 + 自动打开浏览器。
# 停止：回到终端窗口按 Ctrl+C。

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
PORT="${1:-8765}"

PY=""
for cand in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  echo "没找到 python3。装一个再来：https://www.python.org/downloads/"
  read -r -p "按回车关闭…" _
  exit 1
fi

( sleep 1.6; open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 ) &

exec "$PY" server.py "$PORT"
