"""SQLite 接続ヘルパ（全プロセス共通: CLI/API/MCP）。

- connect(): PRAGMA(WAL/busy_timeout/synchronous/foreign_keys)を発行した接続を返す。
- init_db(): schema.sql を冪等適用（CREATE IF NOT EXISTS）。
- transaction(): 短命トランザクション（with を抜けると commit、例外で rollback、必ず close）。

WAL は同一ホストのプロセス間でのみ安全。DB は必ずローカルFSに置く
（SMB/NFS=NAS 上だと WAL/ロックが壊れる）。
スレッド安全のため接続は共有せず、操作ごとに開いて閉じる（短命）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_initialized: set[str] = set()


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = str(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """schema.sql を冪等適用。プロセス内で同一パスは一度だけ。"""
    key = str(Path(db_path).resolve())
    if key in _initialized:
        return
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
    _initialized.add(key)


def _migrate(conn) -> None:
    """既存DBへ後付け列を冪等に追加（CREATE IF NOT EXISTS は列追加しないため）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(oco_episodes)")}
    if "cast_roster_text" not in cols:
        conn.execute("ALTER TABLE oco_episodes ADD COLUMN cast_roster_text TEXT")
    if "story_check_text" not in cols:      # ① 検証パスの出力（監査用）
        conn.execute("ALTER TABLE oco_episodes ADD COLUMN story_check_text TEXT")
    if "serial_next" not in cols:           # 単発/連続の判定（NULL=旧データ・未判定）
        conn.execute("ALTER TABLE oco_episodes ADD COLUMN serial_next INTEGER")
    if "serial_next_reason" not in cols:
        conn.execute("ALTER TABLE oco_episodes ADD COLUMN serial_next_reason TEXT")


@contextmanager
def transaction(db_path: str | Path):
    """短命トランザクション。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
