"""Web 冒烟测试：真实起 HTTP 服务，覆盖三端页面与关键 POST 流程。"""
from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from shuttle.app import AppState, make_handler
from shuttle.seed import seed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class WebSession:
    def __init__(self, base: str):
        self.base = base
        self.cj = CookieJar()
        self.opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPCookieProcessor(self.cj))

    def get(self, path: str) -> tuple[int, str]:
        try:
            r = self.opener.open(self.base + path)
            return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def post(self, path: str, data: dict) -> tuple[int, str, str]:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        try:
            r = self.opener.open(req)
            return r.status, r.read().decode("utf-8"), r.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8"), e.headers.get("Location", "")


class WebSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        import os
        os.remove(cls.tmp.name)  # AppState 自己建库播种
        cls.state = AppState(cls.tmp.name)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.state))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.state.conn.close()

    def _login(self, uid: int) -> WebSession:
        s = WebSession(self.base)
        s.post("/login", {"uid": uid})
        return s

    def test_home_and_pages_render(self):
        s = self._login(21)
        code, home = s.get("/")
        self.assertEqual(code, 200)
        self.assertIn("今日班次", home)
        for path in ("/trip/1", "/trip/2", "/trip/3",
                     "/driver/1", "/ops/1", "/ops/3"):
            code, body = s.get(path)
            self.assertEqual(code, 200, path)

    def test_booking_waitlist_on_full_trip(self):
        s = self._login(22)
        code, body, loc = s.post("/api/book", {
            "trip": 2,
            "board": self._stop_id("R1", 0),
            "alight": self._stop_id("R1", 4)})
        self.assertEqual(code, 303)
        self.assertIn("候补", urllib.parse.unquote(loc))
        code, page = s.get("/trip/2")
        self.assertIn("候补中", page)

    def test_driver_scan_idempotent_flow(self):
        s = self._login(1)  # 张伟
        # BK6 为班次2(08:00)的第一张确认票
        code, _, loc1 = s.post("/api/scan", {"trip": 2, "token": "BK6"})
        self.assertIn("scan=ok", loc1)
        code, _, loc2 = s.post("/api/scan", {"trip": 2, "token": "BK6"})
        self.assertIn("scan=dup", loc2)
        # BK12 属于班次3(08:10 西山线)，在班次2扫 → 不属于本班次
        code, _, loc3 = s.post("/api/scan", {"trip": 2, "token": "BK12"})
        self.assertIn("scan=no", loc3)
        self.assertIn("不属于本班次", urllib.parse.unquote(loc3))

    def test_driver_cannot_verify_operator_can(self):
        d = self._login(1)
        code, body, loc = d.post("/api/verify", {"trip": 2})
        self.assertEqual(code, 200)
        self.assertIn("仅运营员可执行", body)
        op = self._login(2)  # 李娜有 R1 授权
        code, _, loc = op.post("/api/verify", {"trip": 2})
        self.assertEqual(code, 303)

    def test_unauthorized_operator_blocked(self):
        op = self._login(2)  # 仅 R1
        code, body, loc = op.post("/api/change-vehicle",
                                  {"trip": 3, "vehicle": "VAN01"})
        self.assertEqual(code, 200)
        self.assertIn("未被授权操作线路 R2", body)

    def test_shrink_from_ops_page(self):
        op = self._login(3)  # 全线
        code, _, loc = op.post("/api/change-vehicle",
                               {"trip": 3, "vehicle": "BUS02"})
        self.assertEqual(code, 303)
        code, page = op.get("/ops/3")
        self.assertIn("8/8", page)  # 缩容后满载边

    def test_detour_sample_visible(self):
        s = self._login(10)
        code, page = s.get("/trip/1")
        self.assertIn("桥东岸(临停)", page)
        self.assertIn("绕行飞站：江景公寓", page)

    def _stop_id(self, route: str, order: int) -> int:
        row = self.state.conn.execute(
            "SELECT id FROM stops WHERE route_id=? AND trip_scope IS NULL "
            "ORDER BY seq LIMIT 1 OFFSET ?", (route, order)).fetchone()
        return row["id"]


if __name__ == "__main__":
    unittest.main()
