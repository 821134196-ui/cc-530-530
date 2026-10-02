"""零依赖 HTTP 服务：JSON API + 静态页面。

身份用请求头 X-User-Id 传递（演示用，页面右上角切换身份）。
所有写操作走 core 中的加锁事务。
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import core, db, seed as seed_mod

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

ACTOR_HEADER = "X-User-Id"


class Handler(BaseHTTPRequestHandler):
    server_version = "ShuttleBus/1.0"

    # ------------------------------------------------ 工具
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=dict).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self, conn):
        uid = self.headers.get(ACTOR_HEADER, "").strip()
        if not uid:
            raise core.ShuttleError("unauthorized", "缺少身份（X-User-Id）")
        return core.get_user(conn, uid)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise core.ShuttleError("bad_json", "请求体不是合法 JSON")

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ------------------------------------------------ 路由
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            with db.connect() as conn:
                if path == "/api/users":
                    rows = conn.execute("SELECT * FROM users ORDER BY role, id").fetchall()
                    return self._send_json([dict(r) for r in rows])
                if path == "/api/vehicles":
                    rows = conn.execute("SELECT * FROM vehicles ORDER BY seats DESC").fetchall()
                    return self._send_json([dict(r) for r in rows])
                if path == "/api/routes":
                    routes = [dict(r) for r in core.list_routes(conn)]
                    for r in routes:
                        r["stops"] = [dict(s) for s in core.list_stops(conn, r["id"])]
                    return self._send_json(routes)
                if path == "/api/trips":
                    q = self._query()
                    trips = []
                    for t in core.list_trips(conn):
                        d = dict(t)
                        if q.get("detail"):
                            d = core.trip_detail(conn, t["id"], q.get("viewer"))
                        trips.append(d)
                    return self._send_json(trips)
                if path.startswith("/api/trips/"):
                    tid = path.rsplit("/", 1)[-1]
                    q = self._query()
                    d = core.trip_detail(conn, tid, q.get("viewer"))
                    return self._send_json(d)
                if path == "/api/inbox":
                    uid = self._query().get("uid", "")
                    return self._send_json(core.inbox(conn, uid))
                if path == "/api/records":
                    q = self._query()
                    return self._send_json(core.ride_records(conn, q.get("trip")))
                if path == "/api/events":
                    q = self._query()
                    return self._send_json(core.events(conn, q.get("trip")))
            return self._serve_static(path)
        except core.ShuttleError as e:
            return self._send_json({"error": e.code, "message": e.message}, 400)
        except FileNotFoundError as e:
            return self._send_json({"error": "not_found", "message": str(e)}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            with db.connect() as conn:
                actor = self._actor(conn)

                if path == "/api/seed":
                    seed_mod.seed()
                    return self._send_json({"ok": True, "message": "演示数据已重置"})

                # /api/trips/{tid}/...
                if path.startswith("/api/trips/"):
                    parts = path.strip("/").split("/")
                    # api/trips/T1/book
                    if len(parts) == 4:
                        _, _, tid, action = parts
                        if action == "book":
                            row, waitlisted = core.book(
                                None, actor, tid,
                                body.get("employee_id", actor["id"]),
                                body["board_seq"], body["alight_seq"])
                            return self._send_json({"waitlisted": waitlisted,
                                                    "reservation": row})
                        if action == "advance":
                            return self._send_json(core.advance(None, actor, tid))
                        if action == "vehicle":
                            return self._send_json(
                                core.replace_vehicle(None, actor, tid, body["vehicle_id"]))
                        if action == "detour":
                            return self._send_json(
                                core.detour_skip_stops(None, actor, tid, body.get("seqs", [])))
                        if action == "restore":
                            return self._send_json(
                                core.restore_stops(None, actor, tid, body.get("seqs", [])))
                        if action == "scan-board":
                            return self._send_json(
                                core.scan_board(None, actor, tid, str(body.get("ticket", "")).strip()))
                        if action == "scan-alight":
                            return self._send_json(
                                core.scan_alight(None, actor, tid, str(body.get("ticket", "")).strip()))
                        if action == "attendance":
                            return self._send_json(
                                core.mark_attendance(None, actor, tid, body["driver_id"]))
                        if action == "detail":
                            pass

                # /api/reservations/{rid}/...
                if path.startswith("/api/reservations/"):
                    parts = path.strip("/").split("/")
                    if len(parts) == 4:
                        _, _, rid_s, action = parts
                        rid = int(rid_s)
                        if action == "cancel":
                            return self._send_json(
                                core.cancel(None, actor, rid, body.get("reason", "员工取消")))
                        if action == "change":
                            row, waitlisted = core.change_stops(
                                None, actor, rid, body["board_seq"], body["alight_seq"])
                            return self._send_json({"waitlisted": waitlisted,
                                                    "reservation": row})
                        if action == "ack":
                            row = core.ack_remap(
                                None, actor, rid,
                                body.get("board_seq"), body.get("alight_seq"))
                            return self._send_json({"reservation": row})

            return self._send_json({"error": "not_found", "message": f"未知接口 {path}"}, 404)
        except core.ShuttleError as e:
            status = 403 if e.code == "forbidden" else 400
            return self._send_json({"error": e.code, "message": e.message}, status)
        except (ValueError, KeyError) as e:
            return self._send_json({"error": "bad_request", "message": str(e)}, 400)

    # ------------------------------------------------ 静态
    def _serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        # 防目录穿越
        rel = os.path.normpath(path.lstrip("/")).replace("..", "")
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("404".encode())
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="启动时重置并灌入种子数据")
    args = ap.parse_args()

    if args.reset or not os.path.exists(db.DB_PATH):
        seed_mod.seed()
    else:
        db.init_db()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"班车预约系统已启动：http://{args.host}:{args.port}")
    print("  员工预订 /book.html   司机端 /driver.html   运营端 /ops.html")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
