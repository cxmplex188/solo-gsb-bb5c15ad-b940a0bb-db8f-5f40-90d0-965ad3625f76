"""SQLite 连接与建表。

为便于区间比较，时间统一以 ``YYYY-MM-DDTHH:MM:SSZ``（UTC）文本保存，
该格式的字典序与时间顺序一致，因此可直接用字符串比较。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.getenv("TRACE_DB_PATH", "/data/traceability.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    manufacturer TEXT,
    model       TEXT,
    serial_no   TEXT,
    is_national_standard INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certificates (
    id              TEXT PRIMARY KEY,
    instrument_code TEXT NOT NULL REFERENCES instruments(code),
    cert_number     TEXT NOT NULL,
    issued_at       TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    valid_until     TEXT NOT NULL,
    uncertainty     REAL NOT NULL,
    unit            TEXT NOT NULL DEFAULT '',
    digest_alg      TEXT NOT NULL DEFAULT 'sha256',
    signature_digest TEXT NOT NULL,
    signed_payload  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cert_instrument ON certificates(instrument_code);
CREATE INDEX IF NOT EXISTS idx_cert_interval ON certificates(valid_from, valid_until);

CREATE TABLE IF NOT EXISTS trace_edges (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    child_cert_id    TEXT NOT NULL REFERENCES certificates(id),
    parent_cert_id   TEXT NOT NULL REFERENCES certificates(id),
    valid_from       TEXT NOT NULL,
    valid_until      TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    UNIQUE(child_cert_id, parent_cert_id)
);

CREATE INDEX IF NOT EXISTS idx_edge_child ON trace_edges(child_cert_id);
CREATE INDEX IF NOT EXISTS idx_edge_parent ON trace_edges(parent_cert_id);
"""


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn_ctx() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn_ctx() as conn:
        conn.executescript(SCHEMA)


def get_readonly_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个独立只读连接（用于 dry-run 预检）。

    ``PRAGMA query_only=ON`` 后连接在 SQLite 引擎层面拒绝任何写操作，
    连接关闭时事务回滚，从物理上保证预检不会落库。
    """
    conn = _connect()
    try:
        conn.execute("PRAGMA query_only = ON")
        # 显式开启延迟事务，整个预检读到同一快照
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()
