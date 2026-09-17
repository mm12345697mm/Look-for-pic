#!/usr/bin/env bash
# 靜態伺服器：在本資料夾以 8787 埠提供檔案（供 iPhone Safari 連線）
cd "$(dirname "$0")"
echo "Serving $(pwd) at http://0.0.0.0:8787/"
echo "同一區網請用電腦的 IP，例如 http://192.168.x.x:8787/"
exec python3 -m http.server 8787 --bind 0.0.0.0
