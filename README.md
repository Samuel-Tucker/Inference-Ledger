# Inference Ledger

An append-only log of every local-model speed and quality experiment you run, plus a small CLI
that turns it into "what to try next" and "what not to bother with on this machine".
Your coding agents check it **before** tuning and log to it **after** every measured attempt,
so nobody repeats a failure and the levers that work on your hardware rise to the top.

From https://sams.help/local-ai-ledger, where my own ledger is published.
Source and issues: https://github.com/Samuel-Tucker/Inference-Ledger

## Install (macOS or Linux, Python 3.9+, no dependencies)

```sh
unzip inference-ledger.zip -d ~                  # gives you ~/inference-ledger
chmod +x ~/inference-ledger/bin/ledger
ln -s ~/inference-ledger/bin/ledger ~/.local/bin/ledger   # any folder on your PATH

# Give the instructions to your agent:
mkdir -p ~/.claude/skills
ln -s ~/inference-ledger/skill ~/.claude/skills/inference-ledger   # Claude Code
# Codex: same again into ~/.codex/skills/inference-ledger
# Anything else: paste skill/SKILL.md into its instructions file (AGENTS.md etc.)
```

If you keep the ledger somewhere else, either edit the paths at the top of `skill/SKILL.md`
or set `LEDGER_FILE=/path/to/entries.jsonl`.

## Commands

```sh
ledger suggest --host workstation --runtime llama.cpp --model-family qwen   # what to try / avoid here
ledger query --lever spec-decode                                           # history of one lever
ledger add ...                                                             # log a result (see skill/SKILL.md)
ledger stats                                                               # counts by host, runtime, lever
ledger health                                                              # how stale the log is
```

- Data: `ledger/entries.jsonl`, one JSON object per line, created on the first `ledger add`.
- `suggest` ranks each lever by a Beta(1,1) win rate, weighting past results more when they were
  on the same host, runtime and model family. A lever that only ever lost or broke on a host
  goes in "avoid on this host".
- `health` and `watcher-done` support an optional weekly review pass (an agent reading your
  project notes for experiments nobody logged). Ignore the "watcher" lines if you don't run one.
  To have `health` flag projects whose notes changed since their last entry, list them in
  `ledger/watched_projects.json` as `{"project-name": "~/path/to/project"}`.

MIT licence (see `LICENSE`).
