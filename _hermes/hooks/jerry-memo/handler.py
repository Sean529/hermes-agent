"""
Jerry-memo hook: capture messages in Discord #灵感 channel.

On each message:
  1. Insert SQLite row → get auto-increment idea_id
  2. Spawn background thread to fetch URLs, summarize via LLM, write
     markdown to ~/code/jerry-memo, git commit + push.
  3. Short-circuit the agent by returning {"decision": "handled", ...}
     so the user sees a deterministic ack instead of an LLM ramble.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB_PATH = HERMES_HOME / "jerry_memo.db"
TARGET_CHANNEL_ID = "1503323760034582538"
HOOK_DIR = Path(__file__).parent

URL_RE = re.compile(r"https?://[^\s<>\"\)\]]+")


def _ensure_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_msg_id TEXT,
            chat_id TEXT,
            user_id TEXT,
            user_name TEXT,
            content TEXT NOT NULL,
            urls TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT,
            summary TEXT,
            tags TEXT,
            file_path TEXT,
            commit_sha TEXT,
            error TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON ideas(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON ideas(created_at)")
    conn.commit()
    return conn


async def handle(event_type: str, context: dict):
    if event_type != "agent:start":
        return None
    if (context.get("platform") or "") != "discord":
        return None

    # Match either direct posts in #灵感 or any thread under it
    chat_id = str(context.get("chat_id") or "")
    parent_chat_id = str(context.get("parent_chat_id") or "")
    if chat_id != TARGET_CHANNEL_ID and parent_chat_id != TARGET_CHANNEL_ID:
        return None

    message = (context.get("message") or "").strip()
    if not message:
        return None

    urls = URL_RE.findall(message)

    try:
        conn = _ensure_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ideas (discord_msg_id, chat_id, user_id, user_name,
                               content, urls, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(context.get("message_id") or ""),
                chat_id,
                str(context.get("user_id") or ""),
                str(context.get("user_name") or ""),
                message,
                json.dumps(urls, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        idea_id = cur.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        return {
            "decision": "handled",
            "message": f"⚠️ jerry-memo 入库失败: {e}",
        }

    # Spawn background worker (daemon thread so process exit kills it)
    if str(HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(HOOK_DIR))
    try:
        from worker import process_idea
    except Exception as e:
        return {
            "decision": "handled",
            "message": f"💡 灵感#{idea_id} ✅ 已入库（worker 导入失败，将留到下次处理）: {e}",
        }
    threading.Thread(target=process_idea, args=(idea_id,), daemon=True).start()

    url_note = f"（含 {len(urls)} 个链接）" if urls else ""
    return {
        "decision": "handled",
        "message": (
            f"💡 灵感#{idea_id} ✅ 已入队{url_note}\n"
            f"→ 后台处理（fetch → 中文总结 → 归档 → push）"
        ),
    }
