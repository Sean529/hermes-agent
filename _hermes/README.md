# _hermes/ — Jerry-personal hermes-agent extensions

User-managed extensions that live in `~/.hermes/` at runtime. Files here
mirror the on-disk layout — install by symlinking, so edits in this repo
take effect immediately.

## jerry-memo hook

Captures every message in Discord `#灵感` (channel `1503323760034582538`),
inserts it into a SQLite queue, runs a background worker to fetch any
URLs, asks the LLM for a Chinese summary + tags, writes a markdown file
into the [`Sean529/jerry-memo`](https://github.com/Sean529/jerry-memo)
repo, and `git push`-es. A daily cron at 22:00 builds an aggregated
digest and posts it back to `#灵感`.

Requires the `agent:start` short-circuit support in `gateway/run.py`
(merged on `main`).

### Install

```bash
# from this repo root:
ln -s "$(pwd)/_hermes/hooks/jerry-memo"          ~/.hermes/hooks/jerry-memo
ln -s "$(pwd)/_hermes/scripts/jerry_memo_digest.py" ~/.hermes/scripts/jerry_memo_digest.py

# data repo (used by the worker)
git clone git@github.com:Sean529/jerry-memo.git ~/code/jerry-memo

# config: add the channel to free_response_channels so the bot wakes
# without an @-mention, and create the daily-digest cron
yq -i .discord.free_response_channels=\"1503323760034582538\" ~/.hermes/config.yaml
hermes cron create '0 22 * * *' 'noop (script handles delivery)' \
  --name 'jerry-memo daily digest' \
  --script jerry_memo_digest.py --deliver local
hermes gateway restart
```

### Layout

```
_hermes/
├── hooks/jerry-memo/
│   ├── HOOK.yaml      # registers for agent:start
│   ├── handler.py     # filter + enqueue + short-circuit ack
│   └── worker.py      # background fetch → summarize → md → git push
└── scripts/
    └── jerry_memo_digest.py   # cron-driven daily digest builder
```

### State (not tracked here)

- `~/.hermes/jerry_memo.db`    — SQLite queue
- `~/.hermes/logs/jerry_memo.log` — worker diagnostics
- `~/code/jerry-memo/`        — checked-out data repo

### Env knobs

- `JERRY_MEMO_MODEL`  — override the LLM model (default `gpt-5.4`)
- `JERRY_MEMO_REPO`   — override the data repo path (default `~/code/jerry-memo`)
- `JERRY_MEMO_CHANNEL` — override the Discord channel for digest delivery
