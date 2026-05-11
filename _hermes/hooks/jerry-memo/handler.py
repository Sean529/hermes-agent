"""
Jerry-memo hook: capture messages in Discord #灵感 channel.

On each message:
  1. Insert SQLite row → get auto-increment idea_id (now includes any
     image/file attachments via ``media_paths``).
  2. Spawn background thread to fetch URLs, copy media into the repo,
     ask the LLM (with vision) for a Chinese summary, write markdown,
     git commit + push.
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
    # v2: add columns in-place for older DBs.  sqlite has no IF NOT EXISTS
    # for ALTER, so we just try and swallow the duplicate-column error.
    for ddl in (
        "ALTER TABLE ideas ADD COLUMN media_paths TEXT",
        "ALTER TABLE ideas ADD COLUMN raw_text TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON ideas(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON ideas(created_at)")
    conn.commit()
    return conn


async def handle(event_type: str, context: dict):
    if event_type != "agent:start":
        return None
    if (context.get("platform") or "") != "discord":
        return None

    chat_id = str(context.get("chat_id") or "")
    parent_chat_id = str(context.get("parent_chat_id") or "")
    if chat_id != TARGET_CHANNEL_ID and parent_chat_id != TARGET_CHANNEL_ID:
        return None

    # Prefer the raw text the user typed (pre-vision); fall back to the
    # vision-augmented message so attachments-only posts still get
    # recorded.
    raw_text = (context.get("raw_text") or "").strip()
    vision_text = (context.get("message") or "").strip()
    content = raw_text or vision_text
    media_paths = list(context.get("media_urls") or [])
    media_types = list(context.get("media_types") or [])

    if not content and not media_paths:
        return None  # nothing to capture

    urls = URL_RE.findall(content)

    try:
        conn = _ensure_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ideas (discord_msg_id, chat_id, user_id, user_name,
                               content, urls, created_at, raw_text,
                               media_paths)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(context.get("message_id") or ""),
                chat_id,
                str(context.get("user_id") or ""),
                str(context.get("user_name") or ""),
                content,
                json.dumps(urls, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                raw_text,
                json.dumps(
                    list(zip(media_paths, media_types or [""] * len(media_paths))),
                    ensure_ascii=False,
                ),
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

    if str(HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(HOOK_DIR))
    try:
        from worker import process_idea
    except Exception as e:
        return {
            "decision": "handled",
            "message": f"💡 灵感#{idea_id} ✅ 已入库（worker 导入失败）: {e}",
        }
    threading.Thread(target=process_idea, args=(idea_id,), daemon=True).start()

    notes = []
    if urls:
        notes.append(f"{len(urls)} 个链接")
    if media_paths:
        notes.append(f"{len(media_paths)} 张图片/附件")
    extra = f"（含 {'、'.join(notes)}）" if notes else ""
    return {
        "decision": "handled",
        "message": (
            f"💡 灵感#{idea_id} ✅ 已入队{extra}\n"
            f"→ 后台处理（fetch → 视觉描述 → 中文总结 → 归档 → push）"
        ),
    }
