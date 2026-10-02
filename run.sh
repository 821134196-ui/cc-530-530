#!/usr/bin/env bash
# 一条本地命令启动：重置演示数据（含绕行样例说明）并启动服务。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
export SHUTTLE_DB="${SHUTTLE_DB:-$(pwd)/shuttle.db}"

echo "♻  重置并灌入种子数据（T2 区间满员 + 候补；T3 绕行演示班）..."
python3 -m shuttle.seed

echo ""
echo "▶ 绕行样例（也可直接在运营台 /ops.html 勾选站点执行）："
echo "  python3 -m shuttle.demo_detour"
echo ""
echo "🚀 启动服务： http://127.0.0.1:${PORT}"
echo "   线路图 /   员工预订 /book.html   司机端 /driver.html   运营端 /ops.html"
exec python3 -m shuttle.server --host 127.0.0.1 --port "${PORT}"
