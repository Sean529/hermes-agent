"""
Daily digest builder for jerry-memo.

Reads today's done ideas from SQLite, asks the LLM to compose a Chinese
digest, writes ``digests/YYYY-MM-DD.md`` in the jerry-memo repo, commits +
pushes, then prints the digest text on stdout so the cron scheduler can
deliver it back to Discord.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB_PATH = HERMES_HOME / "jerry_memo.db"
REPO_PATH = Path(os.environ.get("JERRY_MEMO_REPO", os.path.expanduser("~/code/jerry-memo")))
DIGEST_CHANNEL_ID = os.environ.get("JERRY_MEMO_CHANNEL", "1503323760034582538")


def _post_to_discord(channel_id: str, content: str) -> None:
    """Post a (potentially chunked) message to a Discord channel."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("⚠️ DISCORD_BOT_TOKEN missing; skipping Discord post", file=sys.stderr)
        return
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "jerry-memo-digest/1.0",
    }
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    # Discord max 2000 chars per message — chunk on paragraph boundaries.
    chunks: list[str] = []
    remaining = content
    while len(remaining) > 1900:
        cut = remaining.rfind("\n\n", 0, 1900)
        if cut < 500:
            cut = 1900
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    with httpx.Client(timeout=30.0) as client:
        for c in chunks:
            try:
                r = client.post(url, headers=headers, json={"content": c})
                if r.status_code >= 300:
                    print(f"⚠️ Discord post {r.status_code}: {r.text[:300]}", file=sys.stderr)
            except Exception as e:
                print(f"⚠️ Discord post error: {e}", file=sys.stderr)


def _load_env() -> None:
    env_path = HERMES_HOME / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _llm_chat(messages: list[dict], timeout: float = 180.0) -> str:
    _load_env()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://jccode.cc/v1").rstrip("/")
    model = os.environ.get("JERRY_MEMO_MODEL", "gpt-5.4")
    with httpx.Client(timeout=timeout) as client:
        r = client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages},
        )
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_PATH, capture_output=True, text=True, check=False
    )


def main() -> int:
    # Determine the day window.  Default: today in local time.  Allow an
    # optional CLI override "YYYY-MM-DD" for manual back-fills.
    if len(sys.argv) > 1:
        day = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        day = datetime.now().astimezone().date()
    day_str = day.strftime("%Y-%m-%d")

    # All ideas created on `day` (UTC stored; compare by local date prefix-ish)
    start = datetime(day.year, day.month, day.day).astimezone(timezone.utc).isoformat(timespec="seconds")
    end = datetime(day.year, day.month, day.day, 23, 59, 59).astimezone(timezone.utc).isoformat(timespec="seconds")

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT id, content, summary, tags, file_path, status
              FROM ideas
             WHERE created_at >= ? AND created_at <= ?
             ORDER BY id
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"📭 {day_str}：今天还没有灵感记录。")
        return 0

    # Build LLM input
    lines = [f"以下是 {day_str} 这一天 Jerry 在 Discord #灵感 频道里记录的 {len(rows)} 条灵感：\n"]
    for rid, content, summary, tags_json, file_path, status in rows:
        tags = json.loads(tags_json or "[]")
        tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
        body = (summary or content).strip()
        lines.append(f"- 灵感#{rid} {tag_str}\n  {body}")
    block = "\n".join(lines)

    sys_prompt = (
        "你是 Jerry 的私人灵感整理助手。下面是 Jerry 一天里记录的所有灵感。\n"
        "请用中文写一份精简的日报，结构：\n"
        "1) 开头一句总结今天的灵感主题分布（按主题分组）。\n"
        "2) 按主题列出每条灵感（保留 #灵感ID，简短一句话即可）。\n"
        "3) 结尾给一两条可执行的下一步建议（如有合适的话）。\n"
        "整体不超过 600 字，markdown 格式，不要写多余解释。"
    )

    try:
        digest = _llm_chat([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": block[:30000]},
        ])
    except Exception as e:
        # Fall back to raw listing on LLM failure — still useful
        digest = f"# {day_str} 灵感日报\n\n_(LLM 总结失败: {e})_\n\n{block}"

    # Strip code fences if any
    digest = re.sub(r"^```(?:markdown)?\s*", "", digest)
    digest = re.sub(r"\s*```$", "", digest)

    # Write digest to repo
    full_path = REPO_PATH / "digests" / f"{day_str}.md"
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(f"# {day_str} 灵感日报\n\n{digest}\n", encoding="utf-8")

    try:
        _git("pull", "--rebase", "origin", "main")
        _git("add", str(full_path.relative_to(REPO_PATH)))
        commit = _git("commit", "-m", f"digest: {day_str} ({len(rows)} ideas)")
        if commit.returncode == 0 or "nothing to commit" in commit.stdout.lower():
            _git("push", "origin", "main")
    except Exception as e:
        print(f"⚠️ git error: {e}", file=sys.stderr)

    final = f"📓 **{day_str} 灵感日报** ({len(rows)} 条)\n\n{digest}"
    # Post directly to Discord (bypasses cron-agent re-processing)
    _load_env()
    _post_to_discord(DIGEST_CHANNEL_ID, final)
    # Also echo to stdout so cron-job logs/agent see what happened.
    # Use a wakeAgent=false marker so the cron scheduler does NOT spin up
    # an agent run to re-process this output.
    print('{"wakeAgent": false}')
    print(f"(digest written: {full_path.relative_to(REPO_PATH)}; posted to channel {DIGEST_CHANNEL_ID})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
