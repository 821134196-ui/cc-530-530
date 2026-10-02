"""SQLite 存储层：建表、连接工厂、乐观锁版本号。

区间容量不在表里存冗余计数——每次需要时按“边”从 confirmed 预约实时聚合，
避免与预约状态不一致。超卖防护：trips.version 乐观锁 + 写事务串行化。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 角色
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('staff','driver','operator','admin'))
);

CREATE TABLE IF NOT EXISTS drivers (
    id INTEGER PRIMARY KEY REFERENCES employees(id),
    license_no TEXT NOT NULL
);

-- 运营员被授权的线路（只能操作授权线路）
CREATE TABLE IF NOT EXISTS operator_routes (
    operator_id INTEGER NOT NULL REFERENCES employees(id),
    route_id    TEXT    NOT NULL REFERENCES routes(id),
    PRIMARY KEY (operator_id, route_id)
);

-- 线路
CREATE TABLE IF NOT EXISTS routes (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stops (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(id),
    code TEXT NOT NULL,            -- 站点编码（也是二维码内容）
    name TEXT NOT NULL,
    seq  INTEGER NOT NULL,         -- 线路上的顺序，0 起；临停用 1000+k
    -- NULL=线路模板站（所有班次共用）；具体班次 id=该班次专属临停
    trip_scope INTEGER REFERENCES trips(id),
    UNIQUE (route_id, seq, trip_scope),
    UNIQUE (route_id, code, trip_scope)
);

-- 车辆（座位数）
CREATE TABLE IF NOT EXISTS vehicles (
    id TEXT PRIMARY KEY,
    plate TEXT NOT NULL,
    seats INTEGER NOT NULL CHECK (seats > 0),
    active INTEGER NOT NULL DEFAULT 1
);

-- 班次：某线路某天某时刻，绑定车辆与司机
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(id),
    trip_date TEXT NOT NULL,      -- YYYY-MM-DD
    depart_at TEXT NOT NULL,      -- HH:MM
    vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
    driver_id INTEGER REFERENCES drivers(id),
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled','departed','completed','cancelled')),
    -- 0 正常；1 绕行中（有 trip_stop_overrides）
    detour INTEGER NOT NULL DEFAULT 0,
    -- 任何影响容量/区间的变更都 +1，用于乐观并发
    version INTEGER NOT NULL DEFAULT 0,
    UNIQUE (route_id, trip_date, depart_at)
);

-- 班次级别的站点覆盖：绕行时临时跳过(skipped)/新增临停(added)
-- added 站点用 'TMPxx' 编码，同时写进 stops（route 级模板，seq 用 1000+k），
-- 班次的实际顺序 = 模板站点 ∪ override，按 seq 排序后剔除 skipped。
CREATE TABLE IF NOT EXISTS trip_stop_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    stop_id INTEGER NOT NULL REFERENCES stops(id),
    kind TEXT NOT NULL CHECK (kind IN ('skipped','added')),
    -- added：插在哪个站点之后（NULL 表示插到队首）；skipped 不用
    after_stop_id INTEGER REFERENCES stops(id),
    note TEXT DEFAULT '',
    UNIQUE (trip_id, stop_id)
);

-- 预约
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    boarding_stop_id INTEGER NOT NULL REFERENCES stops(id),
    alighting_stop_id INTEGER NOT NULL REFERENCES stops(id),
    status TEXT NOT NULL
        CHECK (status IN ('confirmed','waitlisted','cancelled','noshow')),
    -- 扫码核验
    boarded_at TEXT,
    board_trip_id INTEGER REFERENCES trips(id),   -- 实际上车班次（可能改签）
    scan_count INTEGER NOT NULL DEFAULT 0,
    last_scan_at TEXT,
    -- 候补排序：created_seq 全局；queue_seq 仅候补有效时递增，保证 FIFO 递补
    created_seq INTEGER NOT NULL,
    queue_seq INTEGER,
    promoted_from_waitlist INTEGER NOT NULL DEFAULT 0,
    UNIQUE (trip_id, employee_id)   -- 同一员工同班次只有一条（改站走 UPDATE）
);
CREATE INDEX IF NOT EXISTS idx_bookings_trip_status ON bookings(trip_id, status);
CREATE INDEX IF NOT EXISTS idx_bookings_wait ON bookings(trip_id, status, created_seq);

-- 司机出勤核销（司机本人不能改；运营员在授权线路内核销）
CREATE TABLE IF NOT EXISTS driver_checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL UNIQUE REFERENCES trips(id),
    driver_id INTEGER NOT NULL REFERENCES drivers(id),
    checked_in_at TEXT,               -- 司机自助签到时间（不可自行撤销/核销）
    verified_at TEXT,                 -- 运营核销时间
    verified_by INTEGER REFERENCES employees(id),
    verify_note TEXT DEFAULT ''
);

-- 操作审计（换车/绕行/核销/no-show 等关键动作）
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_id INTEGER REFERENCES employees(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def connect(db_path: str = ":memory:", check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """串行化写事务：SQLite 单写者，BEGIN IMMEDIATE 从源头杜绝并发超卖。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
