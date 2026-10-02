"""本地启动入口：python3 -m shuttle [--db shuttle.db] [--port 8000] [--reset]

一条命令完成建表、种子数据（含绕行样例）、Web 服务启动。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from http.server import ThreadingHTTPServer

from .app import AppState, make_handler


BANNER = r"""
   ____  _   _ _   _ _____ _____ _   _ ___       __
  / ___|| | | | | | |_   _|_   _| | | / _ \     / /
  \___ \| |_| | | | | | |   | | | |_| \_\ \   /_/
   ___) |  _  | |_| | | |   | | |  _  |\ \ \  _
  |____/|_| |_|\___/  |_|   |_| |_| |_| |_|/_/
        企业通勤班车 · 区间容量 / 候补递补 / 车载核验
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="企业通勤班车预约系统")
    ap.add_argument("--db", default="shuttle.db", help="SQLite 文件（默认 shuttle.db）")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reset", action="store_true", help="删除旧库并重新播种")
    args = ap.parse_args()

    if args.reset and os.path.exists(args.db):
        os.remove(args.db)
        for suffix in ("-wal", "-shm"):
            p = args.db + suffix
            if os.path.exists(p):
                os.remove(p)

    state = AppState(args.db)
    info = state.seed_info
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(state))

    print(BANNER)
    print(f"  访问 http://{args.host}:{args.port}  （Ctrl+C 停止）\n")
    if info:
        t = info["trips"]
        print("  已播种演示数据：")
        print(f"    · 班次 #{t['detour']} 07:40 滨江线 = 绕行样例"
              "（江景公寓飞站，滨江花园后加“桥东岸(临停)”）")
        print(f"    · 班次 #{t['full']} 08:00 滨江线 = 8 座车区间满载，"
              "员工19/20 在候补，可取消预约观察区间 FIFO 递补")
        print(f"    · 班次 #{t['shrink']} 08:10 西山线 = 20 座 10 人，"
              "在运营台换成 8 座中巴观察缩容降级")
        print("  身份：员工10-23｜司机 张伟=1 / 刘勇=4｜"
              "运营 李娜=2(仅滨江线) / 王强=3(全线)｜管理员=9\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        state.conn.close()


if __name__ == "__main__":
    main()
