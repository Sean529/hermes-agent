"""
Background processor for jerry-memo ideas.

Picks pending rows, fetches any URLs, copies attached media into the
repo, runs the LLM (with vision) for a Chinese summary + tags, writes
a markdown file under ``~/code/jerry-memo``, then commits + pushes.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB_PATH = HERMES_HOME / "jerry_memo.db"
LOG_PATH = HERMES_HOME / "logs" / "jerry_memo.log"
REPO_PATH = Path(os.environ.get("JERRY_MEMO_REPO", os.path.expanduser("~/code/jerry-memo")))

_GIT_LOCK = threading.Lock()

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}\n")


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


def _slugify(text: str, max_len: int = 36) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[\s\W_]+", "-", text, flags=re.UNICODE).strip("-")
    return text[:max_len] or "idea"


def _fetch_url(url: str) -> str:
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 jerry-memo/1.0"},
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            text = r.text
            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:8000]
    except Exception as e:
        return f"[fetch error: {e}]"


def _llm_chat(messages: list[dict], timeout: float = 180.0) -> str:
    """OpenAI-compatible chat completion via httpx — bypasses the openai
    SDK because jccode.cc rejects its default headers."""
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


def _describe_image(image_path: Path) -> str:
    """Ask the LLM to briefly describe one image.  Returns a short
    Chinese description or an empty string on failure."""
    try:
        mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
        with image_path.open("rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        out = _llm_chat(
            [
                {
                    "role": "system",
                    "content": "你是一个简洁的图像描述助手。用 1-3 句中文描述图片里的关键信息（界面元素、文字、数据、人物动作等）。只输出描述，不要前缀和编号。",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "描述这张图："},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            timeout=120,
        )
        return out.strip()
    except Exception as e:
        _log(f"vision failed for {image_path}: {e}")
        return ""


def _copy_media(idea_id: int, ym: str, media_entries: list) -> tuple[list[dict], list[str]]:
    """Copy media files into the repo.  Returns ([{rel, src, type, ext}, ...], errors)."""
    errors: list[str] = []
    out: list[dict] = []
    if not media_entries:
        return out, errors
    dst_dir = REPO_PATH / "attachments" / ym / f"{idea_id:04d}"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for entry in media_entries:
        # tolerate either ["path", "type"] tuples or bare path strings
        if isinstance(entry, (list, tuple)) and entry:
            src_str = entry[0]
            mime = entry[1] if len(entry) > 1 else ""
        else:
            src_str, mime = str(entry), ""
        src = Path(src_str).expanduser()
        if not src.exists():
            errors.append(f"missing: {src}")
            continue
        dst = dst_dir / src.name
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            errors.append(f"copy {src} → {dst}: {e}")
            continue
        out.append({
            "rel": str(dst.relative_to(REPO_PATH)),
            "abs": str(dst),
            "type": mime,
            "ext": src.suffix.lower(),
        })
    return out, errors


def _llm_summarize(content: str, fetched: dict[str, str], image_notes: list[str]) -> dict:
    parts = []
    if content:
        parts.append(f"用户消息：\n{content}")
    for url, body in fetched.items():
        parts.append(f"\n链接 {url} 抓取内容（前 8k 字符）：\n{body[:8000]}")
    if image_notes:
        joined = "\n".join(f"- 图{i+1}：{n}" for i, n in enumerate(image_notes) if n)
        if joined:
            parts.append(f"\n附件图像的视觉描述：\n{joined}")
    if not parts:
        return {"summary": "", "tags": []}
    user_msg = "\n".join(parts)[:20000]

    sys_prompt = (
        "你是 Jerry 的个人灵感整理助手。用户在 Discord #灵感 频道发了一条想法（可能含链接和/或图片）。\n"
        "任务：\n"
        "1) 用 2-5 句中文总结这条灵感的核心要点；融合链接内容与图像描述。\n"
        "2) 给 1-4 个简短中文标签（如 #产品 #trading #学习 #购物 #想法 #工具 #生活 #UI 等）。\n"
        "\n严格只输出 JSON，无其他文字：\n"
        '{"summary": "中文总结...", "tags": ["产品", "学习"]}'
    )

    raw = _llm_chat([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ])
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {"summary": raw[:400], "tags": []}


def _git(*args: str, cwd: Path = REPO_PATH) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def process_idea(idea_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        # Allow reprocessing of done rows too — useful when fixing a
        # bug and regenerating.  Pending/failed are the normal path.
        cur.execute(
            "UPDATE ideas SET status='processing' WHERE id=?",
            (idea_id,),
        )
        if cur.rowcount == 0:
            return
        conn.commit()

        cur.execute(
            "SELECT content, urls, user_name, created_at, media_paths FROM ideas WHERE id=?",
            (idea_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        content, urls_json, user_name, created_at, media_json = row
        urls = json.loads(urls_json or "[]")
        media_entries = json.loads(media_json or "[]")

        try:
            now = datetime.now(timezone.utc).astimezone()
            ym = now.strftime("%Y-%m")

            # 1) Copy media files into the repo first so we have stable
            # paths to reference in the markdown body.
            copied_media, media_errors = _copy_media(idea_id, ym, media_entries)
            for err in media_errors:
                _log(f"idea#{idea_id} media warn: {err}")

            # 2) Vision-describe each image (best-effort)
            image_notes: list[str] = []
            for m in copied_media:
                if m["ext"] in _IMAGE_EXTS:
                    image_notes.append(_describe_image(Path(m["abs"])))
                else:
                    image_notes.append("")

            # 3) Fetch URLs
            fetched = {u: _fetch_url(u) for u in urls}

            # 4) Summarize
            result = _llm_summarize(content, fetched, image_notes)
            summary = (result.get("summary") or "").strip()
            tags = result.get("tags") or []
            if not isinstance(tags, list):
                tags = [str(tags)]
            tags = [str(t).lstrip("#").strip() for t in tags if t]

            slug = _slugify(summary or content or "image-only")
            rel_path = f"{ym}/{idea_id:04d}-{slug}.md"
            full_path = REPO_PATH / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)

            tag_line = " ".join(f"#{t}" for t in tags) if tags else "(none)"
            md_lines = [
                f"# 灵感#{idea_id}",
                "",
                f"- created: {created_at}",
                f"- user: {user_name}",
                f"- tags: {tag_line}",
                "",
            ]
            if content:
                md_lines += ["## 原文", "", content, ""]
            if summary:
                md_lines += ["## 总结", "", summary, ""]
            if copied_media:
                md_lines += ["## 附件", ""]
                for m, note in zip(copied_media, image_notes):
                    if m["ext"] in _IMAGE_EXTS:
                        md_lines += [f"![{Path(m['rel']).name}]({m['rel']})"]
                    else:
                        md_lines += [f"- [{Path(m['rel']).name}]({m['rel']})"]
                    if note:
                        md_lines += [f"  - 描述：{note}"]
                    md_lines += [""]
            if urls:
                md_lines += ["## 链接", ""]
                for u in urls:
                    body = fetched.get(u, "")
                    snippet = body[:2000].strip() + ("..." if len(body) > 2000 else "")
                    md_lines += [f"### {u}", "", snippet, ""]
            full_path.write_text("\n".join(md_lines), encoding="utf-8")

            # Clean up any stale prior file for this idea (slug may have changed)
            for old in (REPO_PATH / ym).glob(f"{idea_id:04d}-*.md"):
                if old != full_path:
                    try:
                        old.unlink()
                    except Exception:
                        pass

            with _GIT_LOCK:
                _git("pull", "--rebase", "origin", "main")
                _git("add", "-A", ym)
                if copied_media:
                    _git("add", "-A", "attachments")
                first_line = (summary or content or "image-only")[:60].replace("\n", " ")
                commit = _git("commit", "-m", f"idea#{idea_id}: {first_line}")
                if commit.returncode != 0 and "nothing to commit" not in commit.stdout.lower():
                    raise RuntimeError(f"git commit failed: {commit.stderr}")
                push = _git("push", "origin", "main")
                if push.returncode != 0:
                    raise RuntimeError(f"git push failed: {push.stderr}")
                commit_sha = _git("rev-parse", "HEAD").stdout.strip()

            cur.execute(
                """
                UPDATE ideas SET status='done', processed_at=?, summary=?,
                                  tags=?, file_path=?, commit_sha=?, error=NULL
                WHERE id=?
                """,
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    summary,
                    json.dumps(tags, ensure_ascii=False),
                    rel_path,
                    commit_sha,
                    idea_id,
                ),
            )
            conn.commit()
            _log(f"idea#{idea_id} done -> {rel_path} (media={len(copied_media)})")
        except Exception as e:
            cur.execute(
                "UPDATE ideas SET status='failed', error=? WHERE id=?",
                (f"{e}\n{traceback.format_exc()}"[:2000], idea_id),
            )
            conn.commit()
            _log(f"idea#{idea_id} FAILED: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "drain":
        c = sqlite3.connect(DB_PATH)
        ids = [r[0] for r in c.execute(
            "SELECT id FROM ideas WHERE status IN ('pending','failed') ORDER BY id"
        )]
        c.close()
        for i in ids:
            print(f"processing idea#{i}")
            process_idea(i)
    elif len(sys.argv) > 1:
        process_idea(int(sys.argv[1]))
    else:
        print("usage: worker.py <idea_id> | drain")
