"""零依赖 Web 应用（stdlib http.server）：预订页 / 司机车载端 / 运营台。

启动：python3 -m shuttle
颜色仅用白、深蓝 (#0f2c52) 与清晰绿色 (#15944a) 状态标记。
"""
from __future__ import annotations

import html
import sqlite3
import threading
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlparse

from .adapters import MockLocator, MockNotifier
from .db import connect, init_db
from .errors import ShuttleError
from .seed import seed
from .service import ShuttleService

NAVY = "#0f2c52"
GREEN = "#15944a"
GRAY = "#66707d"
RED = "#c0392b"

CSS = f"""
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
       margin:0; background:#fff; color:{NAVY}; }}
header {{ background:{NAVY}; color:#fff; padding:14px 22px; display:flex;
          justify-content:space-between; align-items:center; }}
header a {{ color:#cfe0ff; text-decoration:none; margin-left:16px; }}
.wrap {{ max-width:1060px; margin:0 auto; padding:22px; }}
h1 {{ font-size:20px; margin:0 0 14px; }}
h2 {{ font-size:16px; margin:22px 0 8px; border-left:4px solid {GREEN};
      padding-left:8px; }}
.card {{ border:1px solid #d9e0ea; border-radius:10px; padding:16px;
         margin:12px 0; background:#fff; }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th,td {{ border-bottom:1px solid #e5eaf1; padding:8px; text-align:left; }}
th {{ color:{GRAY}; font-weight:600; }}
.btn {{ display:inline-block; background:{GREEN}; color:#fff; border:0;
        border-radius:7px; padding:8px 14px; font-size:14px; cursor:pointer;
        text-decoration:none; }}
.btn.navy {{ background:{NAVY}; }}
.btn.ghost {{ background:#fff; color:{NAVY}; border:1px solid {NAVY}; }}
.btn.sm {{ padding:4px 9px; font-size:12px; }}
.tag {{ display:inline-block; border-radius:5px; padding:2px 9px; font-size:12px;
        color:#fff; }}
.tag.ok {{ background:{GREEN}; }}
.tag.wait {{ background:{GRAY}; }}
.tag.bad {{ background:{RED}; }}
.tag.navy {{ background:{NAVY}; }}
select,input[type=text] {{ padding:7px; border:1px solid #c4cedb;
        border-radius:6px; font-size:14px; color:{NAVY}; }}
.muted {{ color:{GRAY}; font-size:13px; }}
.err {{ background:#fdecea; border:1px solid {RED}; color:{RED};
        padding:10px 14px; border-radius:8px; margin:10px 0; }}
.okbox {{ background:#e7f6ee; border:1px solid {GREEN}; color:{GREEN};
        padding:10px 14px; border-radius:8px; margin:10px 0; }}
/* 站点顺序图 */
.line {{ position:relative; margin:6px 0 6px 18px;
         border-left:4px solid {NAVY}; padding-left:26px; }}
.node {{ position:relative; padding:10px 0; }}
.node:before {{ content:''; position:absolute; left:-34px; top:14px;
         width:14px; height:14px; border-radius:50%; background:#fff;
         border:4px solid {NAVY}; }}
.node.up:before {{ background:{GREEN}; border-color:{GREEN}; }}
.node.down:before {{ background:{NAVY}; }}
.seg {{ margin:-2px 0 -2px 2px; font-size:12px; color:{GRAY}; }}
.seg b.free {{ color:{GREEN}; }} .seg b.zero {{ color:{RED}; }}
.big-next {{ font-size:46px; font-weight:800; color:{NAVY}; line-height:1.1;}}
.big-list {{ font-size:24px; }}
.big-list li {{ margin:10px 0; }}
.token {{ font-family:ui-monospace,Menlo,monospace; font-size:22px;
         letter-spacing:2px; background:{NAVY}; color:#fff; padding:10px 18px;
         border-radius:8px; display:inline-block; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media(max-width:820px){{ .grid{{grid-template-columns:1fr}} }}
"""


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


STATUS_TAG = {
    "confirmed": '<span class="tag ok">已确认</span>',
    "waitlisted": '<span class="tag wait">候补中</span>',
    "cancelled": '<span class="tag wait">已取消</span>',
    "noshow": '<span class="tag bad">未上车</span>',
}


class AppState:
    def __init__(self, db_path: str):
        self.lock = threading.RLock()
        self.conn = connect(db_path, check_same_thread=False)
        init_db(self.conn)
        self.notifier = MockNotifier()
        self.locator = MockLocator()
        self.svc = ShuttleService(self.conn, self.notifier, self.locator)
        if self.conn.execute("SELECT COUNT(*) c FROM routes").fetchone()["c"] == 0:
            self.seed_info = seed(self.conn, self.svc)
        else:
            self.seed_info = None


def make_handler(state: AppState):
    svc = state.svc
    conn = state.conn

    class Handler(__import__("http.server", fromlist=["BaseHTTPRequestHandler"])
                  .BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # ---------------- 基础 ----------------
        def _uid(self) -> int | None:
            ck = SimpleCookie(self.headers.get("Cookie", ""))
            if "uid" in ck:
                try:
                    return int(ck["uid"].value)
                except ValueError:
                    return None
            return None

        def _emp(self, uid: int):
            return conn.execute("SELECT * FROM employees WHERE id=?", (uid,)
                                ).fetchone()

        def send_html(self, body: str, status: int = 200,
                      extra_headers: list[tuple[str, str]] | None = None):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra_headers or []:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, path: str):
            self.send_html("", 303, [("Location", path)])

        def parse_form(self) -> dict[str, str]:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            return {k: v[0] for k, v in parse_qs(raw).items()}

        def page(self, title: str, body: str, uid: int | None) -> str:
            emp = self._emp(uid) if uid else None
            role_name = {"staff": "员工", "driver": "司机",
                         "operator": "运营员", "admin": "管理员"}
            who = (f'{esc(emp["name"])}（{role_name[emp["role"]]}）'
                   if emp else "未登录")
            return f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>{esc(title)}</title><style>{CSS}</style></head><body>
<header><div>🚌 企业通勤班车 <span class=muted style=color:#9db6dd>{esc(title)}</span></div>
<div>{who} <a href=/>首页/切换身份</a></div></header>
<div class=wrap>{body}</div></body></html>"""

        def err_page(self, msg: str, back: str, uid):
            body = (f'<div class=err>⚠ {esc(msg)}</div>'
                    f'<a class="btn navy" href="{back}">返回</a>')
            self.send_html(self.page("操作失败", body, uid))

        def do_GET(self):
            try:
                with state.lock:
                    self.route_get()
            except ShuttleError as e:
                self.err_page(str(e), "/", self._uid())
            except Exception as e:  # noqa: BLE001
                self.err_page(f"服务器错误: {e}", "/", self._uid())

        def do_POST(self):
            try:
                with state.lock:
                    self.route_post()
            except ShuttleError as e:
                ref = self.headers.get("Referer", "/")
                self.err_page(str(e), ref, self._uid())
            except Exception as e:  # noqa: BLE001
                ref = self.headers.get("Referer", "/")
                self.err_page(f"服务器错误: {e}", ref, self._uid())

        # ---------------- GET 路由 ----------------
        def route_get(self):
            p = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(p.query).items()}
            path = p.path
            if path == "/":
                self.home(q)
            elif path.startswith("/trip/"):
                self.booking_page(int(path.split("/")[-1]), q)
            elif path.startswith("/driver/"):
                self.driver_page(int(path.split("/")[-1]))
            elif path.startswith("/ops/"):
                self.ops_page(int(path.split("/")[-1]))
            else:
                self.send_html(self.page("404", "页面不存在", self._uid()), 404)

        def home(self, q):
            uid = self._uid()
            emps = conn.execute("SELECT * FROM employees ORDER BY role,id").fetchall()
            opts = "".join(
                f'<option value={e["id"]} {"selected" if uid==e["id"] else ""}>'
                f'{esc(e["name"])}｜{e["role"]}</option>' for e in emps)
            trips = conn.execute(
                "SELECT t.*, r.name rname, v.plate, v.seats, e.name dname "
                "FROM trips t JOIN routes r ON r.id=t.route_id "
                "JOIN vehicles v ON v.id=t.vehicle_id "
                "LEFT JOIN employees e ON e.id=t.driver_id "
                "ORDER BY t.depart_at"
            ).fetchall()
            rows = ""
            for t in trips:
                badge = '<span class="tag navy">绕行样例</span>' if t["detour"] else ""
                rows += (
                    f'<tr><td>{esc(t["rname"])}</td><td>{t["depart_at"]}</td>'
                    f'<td>{esc(t["plate"])}（{t["seats"]}座）</td>'
                    f'<td>{esc(t["dname"] or "")}</td>'
                    f'<td>{"已发车" if t["status"]=="departed" else t["status"]} {badge}</td>'
                    f'<td><a class="btn sm" href="/trip/{t["id"]}">预订</a> '
                    f'<a class="btn sm navy" href="/driver/{t["id"]}">司机端</a> '
                    f'<a class="btn sm ghost" href="/ops/{t["id"]}">运营台</a></td></tr>')
            msg = ""
            if q.get("ok"):
                msg = f'<div class=okbox>✓ {esc(q["ok"])}</div>'
            body = f"""
{msg}
<div class=card><b>选择身份（演示用）</b>
<form action=/login method=post style=margin-top:10px>
<select name=uid>{opts}</select>
<button class=btn>切换</button></form>
<p class=muted>员工(10-23)可预约；司机：张伟=1、刘勇=4；运营：李娜=2 仅授权滨江线，
王强=3 全线授权；管理员=9。</p></div>
<h1>今日班次</h1>
<table><tr><th>线路</th><th>发车</th><th>车辆</th><th>司机</th><th>状态</th><th>入口</th></tr>
{rows}</table>
<p class=muted>绕行样例 = 07:40 滨江科技园线（江景公寓飞站 + 桥东岸临停）。</p>"""
            self.send_html(self.page("首页", body, uid))

        def _stop_diagram(self, trip_id: int, board_id: int | None,
                          alight_id: int | None) -> str:
            stops = svc.effective_stops(trip_id)
            seg = svc.segment_availability(trip_id)
            ordered_flags = svc._ordered_all_stops(trip_id)[1]
            skipped_names = {s["name"] for s, f in
                             zip(svc._ordered_all_stops(trip_id)[0], ordered_flags) if f}
            nodes = ""
            for i, s in enumerate(stops):
                cls = ""
                mark = ""
                if board_id and s["id"] == board_id:
                    cls, mark = "up", ' <span class="tag ok">上车</span>'
                if alight_id and s["id"] == alight_id:
                    cls, mark = "down", ' <span class="tag navy">下车</span>'
                nodes += f'<div class="node {cls}"><b>{esc(s["name"])}</b>' \
                         f'<span class=muted> {esc(s["code"])}</span>{mark}</div>'
                if i < len(seg):
                    g = seg[i]
                    cls2 = "free" if g["free"] > 0 else "zero"
                    nodes += (f'<div class=seg>区间余座 '
                              f'<b class="{cls2}">{g["free"]}</b>/{g["seats"]} '
                              f'（已占 {g["used"]}）</div>')
            skip_note = (f'<p class=muted>绕行飞站：{esc("、".join(skipped_names))}，'
                         '飞站不在线路图中显示。</p>') if skipped_names else ""
            return f'<div class=line>{nodes}</div>{skip_note}'

        def booking_page(self, trip_id: int, q):
            uid = self._uid()
            if not uid:
                return self.redirect("/")
            trip = svc.get_trip(trip_id)
            stops = svc.effective_stops(trip_id)
            my = conn.execute(
                "SELECT * FROM bookings WHERE trip_id=? AND employee_id=?",
                (trip_id, uid)).fetchone()
            board_id = my["boarding_stop_id"] if my else None
            alight_id = my["alighting_stop_id"] if my else None
            opts = "".join(f'<option value={s["id"]}>{esc(s["name"])}</option>'
                           for s in stops)
            my_card = ""
            if my:
                qpos = ""
                if my["status"] == "waitlisted":
                    qpos = f'候补序号 <b>#{my["queue_seq"]}</b>'
                token = f'<div class=token>BK{my["id"]}</div>'
                token_note = "乘车时请向司机出示此乘车码"
                boarded = (f'<p>上车时间：{esc(my["boarded_at"])}｜'
                           f'扫码次数：{my["scan_count"]}</p>'
                           if my["boarded_at"] else "")
                cancel_btn = ""
                if my["status"] in ("confirmed", "waitlisted"):
                    cancel_btn = (f'<form method=post action=/api/cancel style=display:inline>'
                                  f'<input type=hidden name=booking value={my["id"]}>'
                                  f'<input type=hidden name=trip value={trip_id}>'
                                  f'<button class="btn ghost sm">取消预约</button></form>')
                promoted = ' <span class="tag ok">候补递补</span>' if my["promoted_from_waitlist"] else ""
                my_card = f"""
<h2>我的预约 {STATUS_TAG[my["status"]]} {promoted} {qpos}</h2>
<div class=card>
乘车码：{token} <span class=muted>{token_note}</span>
{boarded}
{cancel_btn}
</div>"""
            notices = state.notifier.for_employee(uid)
            notice_html = ""
            if notices:
                items = "".join(f'<li><b>{esc(m["title"])}</b>：{esc(m["body"])}</li>'
                                for m in notices[-6:][::-1])
                notice_html = f'<h2>通知（模拟推送）</h2><div class=card><ul>{items}</ul></div>'

            flash = ""
            if q.get("err"):
                flash = f'<div class=err>⚠ {esc(q["err"])}</div>'
            body = f"""
{flash}
<div class=grid>
<div>
<h2>站点顺序与区间余座</h2>
<div class=card>{self._stop_diagram(trip_id, board_id, alight_id)}</div>
{my_card}
{notice_html}
</div>
<div>
<h2>{'改站' if my and my['status'] in ('confirmed','waitlisted') else '预约'}</h2>
<div class=card>
<form method=post action=/api/book>
<input type=hidden name=trip value={trip_id}>
<p>上车站<br><select name=board>{opts}</select></p>
<p>下车站<br><select name=alight>{opts}</select></p>
<button class=btn>{'提交改站' if my else '确认预约（满员自动候补）'}</button>
</form>
<p class=muted>容量按“上车→下车”覆盖的每个区间分别计算；
任一区间满员即进入候补，取消/换车后按区间 FIFO 自动递补。</p>
</div>
</div>
</div>"""
            self.send_html(self.page("班次预订", body, uid))

        def driver_page(self, trip_id: int):
            uid = self._uid()
            if not uid:
                return self.redirect("/")
            trip = svc.get_trip(trip_id)
            stops = svc.effective_stops(trip_id)
            nxt = state.locator.next_stop_order(trip_id, len(stops))
            next_name = stops[nxt]["name"] if nxt is not None else "已到终点"
            blist = svc.boarding_list(trip_id)
            here = [b for b in blist if nxt is not None and b["boarding_order"] == nxt]
            later = [b for b in blist if nxt is None or b["boarding_order"] > nxt]
            chk = conn.execute(
                "SELECT * FROM driver_checkins WHERE trip_id=?", (trip_id,)
            ).fetchone()
            if chk is None:
                chk_html = '<span class="tag wait">未签到</span>'
            elif chk["verified_at"]:
                chk_html = f'<span class="tag ok">已签到并核销</span>'
            else:
                chk_html = ('<span class="tag navy">已签到·待运营核销</span> '
                            '<span class=muted>（司机无权自行核销）</span>')

            stop_btns = "".join(
                f'<form method=post action=/api/depart-stop style=display:inline>'
                f'<input type=hidden name=trip value={trip_id}>'
                f'<input type=hidden name=stop value={s["id"]}>'
                f'<button class="btn ghost sm" '
                f'{"disabled" if nxt is None or i < nxt else ""}>'
                f'驶离：{esc(s["name"])}</button> </form>'
                for i, s in enumerate(stops))
            here_li = "".join(
                f'<li>🧍 {esc(b["employee_name"])} '
                f'<span class=muted>下于 {esc(b["alighting_stop"])}</span> '
                f'<span class=token style=font-size:15px;padding:3px 9px>BK{b["booking_id"]}</span></li>'
                for b in here) or '<li class=muted>本站无待上车乘客</li>'
            later_li = "".join(
                f'<li>{esc(b["employee_name"])} @ {esc(b["boarding_stop"])} '
                f'<span class=token style=font-size:14px;padding:2px 8px>BK{b["booking_id"]}</span></li>'
                for b in later) or '<li class=muted>无</li>'
            q = parse_qs(urlparse(self.path).query)
            scan_msg = ""
            if q.get("scan"):
                kind, txt = q["scan"][0], q.get("m", [""])[0]
                cls = {"ok": "okbox", "dup": "okbox", "no": "err"}.get(kind, "err")
                scan_msg = f'<div class={cls}>{esc(txt)}</div>'
            body = f"""
<div class=grid>
<div>
<h2>下一站</h2>
<div class=card><div class=big-next>▶ {esc(next_name)}</div>
<p class=muted>车辆定位为模拟适配器，点“驶离”推进。</p>{stop_btns}</div>
<h2>本站待上车</h2><div class=card><ul class=big-list>{here_li}</ul></div>
</div>
<div>
<h2>车载核验</h2>
<div class=card>
{chk_html}
<form method=post action=/api/checkin style=margin:10px 0>
<input type=hidden name=trip value={trip_id}>
<button class="btn navy" {'disabled' if chk and chk['checked_in_at'] else ''}>司机签到</button>
</form>
{scan_msg}
<form method=post action=/api/scan>
<input type=hidden name=trip value={trip_id}>
<input type=text name=token placeholder="输入/扫描乘车码，如 BK5" size=22
 style=font-size:20px;padding:12px>
<button class=btn style=font-size:18px;padding:11px 22px>核验上车</button>
</form>
<p class=muted>重复扫码幂等：只确认第一次，再次扫码提示已上车，不产生重复乘车记录。</p>
</div>
<h2>后续待上车名单（{len(later)}）</h2>
<div class=card><ul class=big-list style=font-size:18px>{later_li}</ul></div>
</div>
</div>"""
            self.send_html(self.page("司机车载端", body, uid))

        def ops_page(self, trip_id: int):
            uid = self._uid()
            if not uid:
                return self.redirect("/")
            trip = svc.get_trip(trip_id)
            # 页面即使无权限也展示，但会显示授权提示；所有写操作在服务层强校验
            emp = self._emp(uid)
            authorized = False
            if emp and emp["role"] == "admin":
                authorized = True
            elif emp and emp["role"] == "operator":
                authorized = conn.execute(
                    "SELECT 1 FROM operator_routes WHERE operator_id=? AND route_id=?",
                    (uid, trip["route_id"])).fetchone() is not None
            auth_banner = ('<div class=okbox>您有权操作该线路</div>' if authorized
                           else '<div class=err>⚠ 您未被授权该线路：'
                                '以下操作将被服务层拒绝（用于验证越权防护）。</div>')

            seg = svc.segment_availability(trip_id)
            seg_rows = "".join(
                f'<tr><td>{esc(s["from_stop"])}</td><td>{esc(s["to_stop"])}</td>'
                f'<td>{s["used"]}/{s["seats"]}</td>'
                f'<td><b style="color:{GREEN if s["free"] else RED}">{s["free"]}</b></td></tr>'
                for s in seg)
            riders = svc.rider_records(trip_id)
            r_rows = "".join(
                f'<tr><td>{esc(r["employee"])}</td><td>{esc(r["board"])}</td>'
                f'<td>{esc(r["alight"])}</td><td>{STATUS_TAG[r["status"]]}</td>'
                f'<td>{"是" if r["promoted"] else ""}</td>'
                f'<td>{esc(r["boarded_at"] or "—")}</td><td>{r["scans"]}</td></tr>'
                for r in riders)
            vehicles = conn.execute("SELECT * FROM vehicles WHERE active=1").fetchall()
            v_opts = "".join(
                f'<option value={v["id"]} {"selected" if v["id"]==trip["vehicle_id"] else ""}>'
                f'{esc(v["plate"])}（{v["seats"]}座）</option>' for v in vehicles)
            stops = svc.effective_stops(trip_id)
            s_opts = "".join(f'<option value={s["id"]}>{esc(s["name"])}</option>'
                             for s in stops)
            chk = conn.execute(
                "SELECT c.*, e.name dname FROM driver_checkins c "
                "JOIN employees e ON e.id=c.driver_id WHERE trip_id=?",
                (trip_id,)).fetchone()
            if chk:
                verify = (f'司机 {esc(chk["dname"])}｜签到 {esc(chk["checked_in_at"])}｜'
                          f'核销状态：'
                          + (f'<span class="tag ok">已核销 by #{chk["verified_by"]}</span>'
                             if chk["verified_at"]
                             else '<span class="tag navy">待核销</span>'))
                if not chk["verified_at"]:
                    verify += (f'<form method=post action=/api/verify style=margin-top:8px>'
                               f'<input type=hidden name=trip value={trip_id}>'
                               f'<button class=btn>核销出勤</button></form>')
            else:
                verify = '<span class=muted>司机尚未签到</span>'
            audit = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT 14").fetchall()
            a_rows = "".join(
                f'<tr><td>{esc(a["at"])}</td><td>{esc(a["action"])}</td>'
                f'<td>{esc(a["detail"])}</td></tr>' for a in audit)
            body = f"""
{auth_banner}
<div class=grid>
<div>
<h2>区间实际占用与空座</h2>
<div class=card><table><tr><th>从</th><th>到</th><th>占用/座位</th><th>余座</th></tr>
{seg_rows}</table></div>
<h2>乘坐记录</h2>
<div class=card><table><tr><th>员工</th><th>上</th><th>下</th><th>状态</th>
<th>候补递补</th><th>上车时间</th><th>扫码次数</th></tr>{r_rows}</table>
<form method=post action=/api/noshow style=margin-top:10px>
<input type=hidden name=trip value={trip_id}>
<button class="btn ghost sm">将未扫码的确认乘客标记为未上车</button></form>
</div>
</div>
<div>
<h2>临时换车</h2>
<div class=card><form method=post action=/api/change-vehicle>
<input type=hidden name=trip value={trip_id}>
<select name=vehicle>{v_opts}</select>
<button class=btn>换车并重排队列</button></form>
<p class=muted>换小车（缩容）时，覆盖超员区间且最晚预约者降级候补，
再按区间 FIFO 重新递补，保证每条边不超座位数。</p></div>
<h2>绕行（飞站 / 临停）</h2>
<div class=card>
<form method=post action=/api/skip-stop style=margin-bottom:8px>
<input type=hidden name=trip value={trip_id}>
<select name=stop>{s_opts}</select>
<button class="btn navy">飞站（不停靠）</button></form>
<form method=post action=/api/add-stop class=muted>
<input type=hidden name=trip value={trip_id}>
临停名称 <input type=text name=name placeholder="桥东岸(临停)"> 插在
<select name=after>{s_opts}</select> 之后
<button class=btn>加临停</button></form>
<form method=post action=/api/clear-detour style=margin-top:8px>
<input type=hidden name=trip value={trip_id}>
<button class="btn ghost sm">解除绕行恢复原线</button></form>
</div>
<h2>司机出勤核销</h2><div class=card>{verify}</div>
<h2>最近操作审计</h2>
<div class=card><table><tr><th>时间</th><th>动作</th><th>详情</th></tr>{a_rows}</table></div>
</div>
</div>"""
            self.send_html(self.page("运营台", body, uid))

        # ---------------- POST 路由 ----------------
        def route_post(self):
            path = urlparse(self.path).path
            f = self.parse_form()
            uid = self._uid()
            if path == "/login":
                self.send_html("", 303, [
                    ("Location", "/"),
                    ("Set-Cookie", f'uid={f["uid"]}; Path=/'),
                ])
                return
            if not uid:
                return self.redirect("/")
            tid = int(f.get("trip", 0))

            if path == "/api/book":
                b, a = int(f["board"]), int(f["alight"])
                exists = conn.execute(
                    "SELECT id,status FROM bookings WHERE trip_id=? AND employee_id=?",
                    (tid, uid)).fetchone()
                if exists and exists["status"] in ("confirmed", "waitlisted"):
                    r = svc.change_stops(tid, uid, b, a)
                    ok = "改站成功" if r["status"] == "confirmed" \
                        else "新区间已满，已进入候补队尾"
                else:
                    r = svc.book(tid, uid, b, a)
                    ok = "预约成功" if r["status"] == "confirmed" \
                        else f'区间已满，已进入候补（序号 #{r.get("queue_seq")}）'
                if r.get("promoted"):
                    ok += f'；同时递补候补 {len(r["promoted"])} 人'
                self.redirect(f"/trip/{tid}?ok={quote(ok)}")
            elif path == "/api/cancel":
                bid = int(f["booking"])
                svc.cancel(bid, uid)
                self.redirect(f"/trip/{tid or '/'}")
            elif path == "/api/checkin":
                svc.driver_self_checkin(tid, uid)
                self.redirect(f"/driver/{tid}")
            elif path == "/api/scan":
                token = f.get("token", "").strip().upper()
                if not token.startswith("BK") or not token[2:].isdigit():
                    self.redirect(f"/driver/{tid}?scan=no&m={quote('乘车码格式应为 BK数字')}")
                    return
                r = svc.boarding_scan(tid, uid, int(token[2:]))
                emp = conn.execute(
                    "SELECT e.name FROM bookings b JOIN employees e ON e.id=b.employee_id "
                    "WHERE b.id=?", (int(token[2:]),)).fetchone()
                name = emp["name"] if emp else "?"
                mp = {
                    "boarded": ("ok", f"✓ 核验通过：{name} 已上车"),
                    "already": ("dup", f"✓ {name} 此前已核验上车（重复扫码，幂等处理）"),
                    "waitlist": ("no", "该乘客仍在候补，当前区间无座，不可上车"),
                    "wrong_trip": ("no", "该乘车码不属于本班次"),
                    "cancelled": ("no", "该预约已取消"),
                    "closed": ("no", "该乘客已被标记未上车/班次已关闭"),
                    "not_found": ("no", "查无此乘车码"),
                }
                kind, txt = mp.get(r["action"], ("no", str(r)))
                self.redirect(f"/driver/{tid}?scan={kind}&m={quote(txt)}")
            elif path == "/api/depart-stop":
                svc.depart_stop(tid, uid, int(f["stop"]))
                self.redirect(f"/driver/{tid}")
            elif path == "/api/change-vehicle":
                r = svc.change_vehicle(tid, uid, f["vehicle"])
                note = (f'已换为 {r["seats"]} 座车辆；降级候补 {len(r["demoted"])} 人，'
                        f'递补 {len(r["promoted"])} 人')
                self.redirect(f"/ops/{tid}")
            elif path == "/api/skip-stop":
                svc.skip_stop(tid, uid, int(f["stop"]), f.get("note", ""))
                self.redirect(f"/ops/{tid}")
            elif path == "/api/add-stop":
                after = int(f["after"])
                svc.add_temporary_stop(tid, uid, f["name"].strip() or "临停", after)
                self.redirect(f"/ops/{tid}")
            elif path == "/api/clear-detour":
                svc.clear_detour(tid, uid)
                self.redirect(f"/ops/{tid}")
            elif path == "/api/verify":
                svc.verify_checkin(tid, uid)
                self.redirect(f"/ops/{tid}")
            elif path == "/api/noshow":
                ids = svc.mark_noshow(tid, uid)
                self.redirect(f"/ops/{tid}")
            else:
                self.send_html(self.page("404", "未知操作", uid), 404)

    return Handler
