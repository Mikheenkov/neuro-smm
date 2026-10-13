"""SQLite-хранилище (§6, упрощённо для фазы 1)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  tg_chat_id INTEGER NOT NULL,
  tg_msg_id INTEGER NOT NULL,              -- первое сообщение альбома
  media_group_id TEXT,
  origin TEXT NOT NULL DEFAULT 'live',     -- live | import
  first_seen_at TEXT NOT NULL,
  last_edit_at TEXT,
  publish_at TEXT,
  state TEXT NOT NULL,                     -- buffering | publishing | done | partial | skipped | held
  reason TEXT,
  text TEXT NOT NULL DEFAULT '',
  entities_json TEXT NOT NULL DEFAULT '[]',
  media_json TEXT NOT NULL DEFAULT '[]',   -- [{type, file_id, file_unique_id, width, height, size, msg_id}]
  content_hash TEXT NOT NULL DEFAULT '',
  gen_hash TEXT,                           -- content_hash версии, по которой сделана адаптация
  gen_json TEXT,                           -- готовые payload по сетям + анализ
  report_sent INTEGER NOT NULL DEFAULT 0,
  UNIQUE (tg_chat_id, tg_msg_id)
);
CREATE INDEX IF NOT EXISTS posts_group ON posts(tg_chat_id, media_group_id);

CREATE TABLE IF NOT EXISTS publications (
  id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL,
  network TEXT NOT NULL,
  state TEXT NOT NULL,                     -- pending | publishing | published | failed | skipped
  attempts INTEGER NOT NULL DEFAULT 0,
  attempt_started_at TEXT,
  remote_id TEXT,
  remote_url TEXT,
  extra_json TEXT,
  error TEXT,
  published_at TEXT,
  UNIQUE (post_id, network)
);

CREATE TABLE IF NOT EXISTS llm_runs (
  id INTEGER PRIMARY KEY,
  post_id INTEGER,
  provider TEXT, model TEXT, started_at TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cost_usd_est REAL, duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS feedback (            -- замечания автора к версиям: материал для правил и эталонов
  id INTEGER PRIMARY KEY, post_id INTEGER, network TEXT, notes TEXT,
  before_json TEXT, after_json TEXT, created_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # --- posts ---------------------------------------------------------------

    def post_by_msg(self, chat_id: int, msg_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM posts WHERE tg_chat_id=? AND tg_msg_id=?", (chat_id, msg_id)).fetchone()

    def post_by_group(self, chat_id: int, group_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM posts WHERE tg_chat_id=? AND media_group_id=?", (chat_id, group_id)).fetchone()

    def post(self, post_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()

    def insert_post(self, **fields) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(f"INSERT INTO posts ({cols}) VALUES ({marks})", tuple(fields.values()))
        return cur.lastrowid

    def update_post(self, post_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE posts SET {sets} WHERE id=?", (*fields.values(), post_id))

    def posts_in_state(self, *states: str) -> list[sqlite3.Row]:
        marks = ", ".join("?" for _ in states)
        return self.conn.execute(f"SELECT * FROM posts WHERE state IN ({marks}) ORDER BY id", states).fetchall()

    def recent_posts(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM posts WHERE origin='live' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # --- publications --------------------------------------------------------

    def publication(self, post_id: int, network: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM publications WHERE post_id=? AND network=?", (post_id, network)).fetchone()

    def publications(self, post_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM publications WHERE post_id=? ORDER BY id", (post_id,)).fetchall()

    def upsert_publication(self, post_id: int, network: str, **fields) -> None:
        if self.publication(post_id, network) is None:
            self.conn.execute("INSERT INTO publications (post_id, network, state) VALUES (?, ?, 'pending')", (post_id, network))
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE publications SET {sets} WHERE post_id=? AND network=?", (*fields.values(), post_id, network))

    def copy_url(self, tg_msg_id: int, network: str) -> str | None:
        """URL копии своего TG-поста в сети (для замены ссылок на свои посты)."""
        row = self.conn.execute(
            "SELECT pub.remote_url FROM publications pub JOIN posts p ON p.id = pub.post_id "
            "WHERE p.tg_msg_id=? AND pub.network=? AND pub.state='published'", (tg_msg_id, network)).fetchone()
        return row["remote_url"] if row else None

    def own_post_text(self, tg_msg_id: int) -> str | None:
        row = self.conn.execute("SELECT text FROM posts WHERE tg_msg_id=? ORDER BY id LIMIT 1", (tg_msg_id,)).fetchone()
        return row["text"] if row else None

    # --- прочее --------------------------------------------------------------

    def log_llm(self, post_id: int | None, run) -> None:
        self.conn.execute(
            "INSERT INTO llm_runs (post_id, provider, model, started_at, input_tokens, output_tokens, cost_usd_est, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (post_id, run.provider, run.model, now_iso(), run.input_tokens, run.output_tokens, run.cost_usd_est, run.duration_ms))

    def month_cost(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd_est), 0) AS c FROM llm_runs WHERE started_at >= ?",
            (datetime.now(timezone.utc).strftime("%Y-%m-01"),)).fetchone()
        return float(row["c"])

    def add_feedback(self, post_id: int, network: str, notes: str, before, after) -> None:
        self.conn.execute("INSERT INTO feedback (post_id, network, notes, before_json, after_json, created_at) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (post_id, network, notes, dumps(before), dumps(after), now_iso()))

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
