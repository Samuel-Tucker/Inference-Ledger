#!/usr/bin/env python3
"""Inference Ledger: append-only log of local-model tuning experiments and what they measured.

Stdlib only, so any agent on any host can run it. Data lives in ledger/entries.jsonl.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LEDGER_FILE", ROOT / "ledger" / "entries.jsonl"))
WATCH = ROOT / "ledger" / "watched_projects.json"
WATCHER_LOG = ROOT / "ledger" / "watcher_runs.jsonl"
WATCHER_EVERY_DAYS = 7  # WATCHER.md cadence; scripts/watcher_auto.py runs a pass past this

OUTCOMES = {"win", "loss", "neutral", "broken", "inconclusive"}
STATUSES = {"measured", "candidate"}
CATEGORIES = {
    "decode-speed", "prefill", "memory", "quality", "sampling", "kernel",
    "parallelism", "driver", "quantisation", "serving", "other",
}
REQUIRED_MEASURED = ["project", "host", "model", "runtime", "lever", "change", "outcome", "source"]
REQUIRED_CANDIDATE = ["lever", "change", "source"]
DIRECTION = {"higher": 1, "lower": -1}


class LedgerError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise(e: dict) -> dict:
    """Validate and fill derived fields. Raises LedgerError on bad input."""
    e = {k: v for k, v in e.items() if v is not None and v != ""}
    e.setdefault("status", "measured")
    if e["status"] not in STATUSES:
        raise LedgerError(f"status must be one of {sorted(STATUSES)}")
    required = REQUIRED_MEASURED if e["status"] == "measured" else REQUIRED_CANDIDATE
    missing = [k for k in required if k not in e]
    if missing:
        raise LedgerError(f"missing required fields: {', '.join(missing)}")
    if "outcome" in e and e["outcome"] not in OUTCOMES:
        raise LedgerError(f"outcome must be one of {sorted(OUTCOMES)}")
    if "lever_category" in e and e["lever_category"] not in CATEGORIES:
        raise LedgerError(f"lever_category must be one of {sorted(CATEGORIES)}")
    for k in ("before", "after"):
        if k in e:
            try:
                e[k] = float(e[k])
            except (TypeError, ValueError):
                raise LedgerError(f"{k} must be a number") from None
    direction = e.get("better", "higher")
    if direction not in DIRECTION:
        raise LedgerError("better must be 'higher' or 'lower'")
    if "before" in e and "after" in e and e["before"]:
        e["delta_pct"] = round((e["after"] - e["before"]) / abs(e["before"]) * 100, 2)
    for k in ("lever", "host", "runtime", "model_family"):
        if k in e:
            e[k] = str(e[k]).strip().lower()
    e.setdefault("id", uuid.uuid4().hex[:12])
    e.setdefault("logged_at", _now())
    e.setdefault("date", date.today().isoformat())
    e.setdefault("agent", os.environ.get("LEDGER_AGENT", "unknown"))
    return e


def load(path: Path = DATA) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path}:{n}: bad JSON ({exc})") from None
    return out


def append(entry: dict, path: Path = DATA) -> dict:
    entry = normalise(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)
    return entry


def _match(e: dict, filters: dict) -> bool:
    for k, v in filters.items():
        if v and v.lower() not in str(e.get(k, "")).lower():
            return False
    return True


def improvement_pct(e: dict):
    """delta_pct signed so positive always means better (latency down = positive)."""
    d = e.get("delta_pct")
    if d is None:
        return None
    if e.get("better") in DIRECTION:
        return d * DIRECTION[e["better"]]
    # Unset direction: a logged win is an improvement by definition.
    return abs(d) if e.get("outcome") == "win" else d


def suggest(entries: list[dict], host=None, runtime=None, model_family=None, model=None, top=10) -> dict:
    """Rank levers by past results, weighting entries by how closely they match the context."""
    stats = defaultdict(lambda: {"w": 0.0, "l": 0.0, "n": 0, "best_delta": None, "examples": []})
    tried_here = defaultdict(list)
    for e in entries:
        if e.get("status") != "measured" or e.get("outcome") == "inconclusive":
            continue
        weight = 1.0
        same_host = bool(host) and e.get("host") == host.lower()
        weight += 2.0 * same_host
        weight += 1.5 * (bool(runtime) and e.get("runtime") == runtime.lower())
        weight += 1.0 * (bool(model_family) and e.get("model_family") == model_family.lower())
        weight += 1.0 * (bool(model) and model.lower() in str(e.get("model", "")).lower())
        s = stats[e["lever"]]
        s["n"] += 1
        if e["outcome"] == "win":
            s["w"] += weight
        elif e["outcome"] in ("loss", "broken"):
            s["l"] += weight
        else:
            s["w"] += weight * 0.3
            s["l"] += weight * 0.7
        d = improvement_pct(e)
        if d is not None and e["outcome"] == "win":
            s["best_delta"] = d if s["best_delta"] is None else max(s["best_delta"], d)
        if len(s["examples"]) < 3:
            s["examples"].append(f"{e.get('host')}/{e.get('runtime')}/{e.get('model')}: {e['outcome']}"
                                 + (f" ({d:+.1f}%)" if d is not None else ""))
        if same_host:
            tried_here[e["lever"]].append(e["outcome"])
    ranked = []
    for lever, s in stats.items():
        score = (s["w"] + 1) / (s["w"] + s["l"] + 2)  # Beta(1,1) posterior mean
        ranked.append({"lever": lever, "score": round(score, 3), "n": s["n"],
                       "best_gain_pct": s["best_delta"], "examples": s["examples"],
                       "tried_on_host": tried_here.get(lever, [])})
    ranked.sort(key=lambda r: (-r["score"], -r["n"]))
    avoid = [r for r in ranked if r["tried_on_host"] and all(o in ("loss", "broken") for o in r["tried_on_host"])]
    measured_levers = {e["lever"] for e in entries if e.get("status") == "measured"}
    candidates = [e for e in entries if e.get("status") == "candidate"
                  and e["lever"] not in measured_levers
                  and (not runtime or not e.get("runtime") or e.get("runtime") == runtime.lower())]
    return {"try": [r for r in ranked if r not in avoid][:top], "avoid_on_this_host": avoid,
            "untested_candidates": candidates[:top]}


def _age_days(ts: str, now: float) -> float:
    return (now - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) / 86400


def record_watcher_run(notes=None, entries: int = 0, path: Path = WATCHER_LOG) -> dict:
    """Stamp a finished WATCHER.md pass (append-only), so the reminder knows when the next is due."""
    run = {"ran_at": _now(), "agent": os.environ.get("LEDGER_AGENT", "unknown"), "entries": entries}
    if notes:
        run["notes"] = notes
    with open(path, "a") as fh:
        fh.write(json.dumps(run, sort_keys=True) + "\n")
    return run


def health(entries: list[dict], watch_path: Path = WATCH, stale_days: int = 14,
           watcher_log: Path = WATCHER_LOG) -> dict:
    """Staleness report: last entry age, projects with activity newer than their last ledger entry."""
    now = time.time()
    measured = [e for e in entries if e.get("status") == "measured"]
    last_logged = max((e.get("logged_at", "") for e in entries), default=None)
    by_project: dict[str, str] = {}
    for e in measured:
        p = e.get("project", "")
        by_project[p] = max(by_project.get(p, ""), e.get("logged_at", ""))
    unlogged = []
    watched = json.loads(watch_path.read_text()) if watch_path.exists() else {}
    for project, folder in watched.items():
        folder_p = Path(os.path.expanduser(folder))
        progress = next((folder_p / n for n in ("PROGRESS.md", "progress.md", "README.md") if (folder_p / n).exists()), None)
        if not progress:
            continue
        mtime = datetime.fromtimestamp(progress.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if mtime > by_project.get(project, ""):
            unlogged.append({"project": project, "progress_modified": mtime,
                             "last_ledger_entry": by_project.get(project)})
    old_candidates = []
    for e in entries:
        if e.get("status") != "candidate":
            continue
        try:
            age = (now - datetime.strptime(e["logged_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) / 86400
        except (KeyError, ValueError):
            continue
        if age > 30:
            old_candidates.append({"lever": e["lever"], "age_days": int(age)})
    days_since = None
    if last_logged:
        days_since = round((now - datetime.strptime(last_logged, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) / 86400, 1)
    last_run = max((r.get("ran_at", "") for r in load(watcher_log)), default=None)
    watcher_days = round(_age_days(last_run, now), 1) if last_run else None
    return {"entries": len(entries), "measured": len(measured),
            "candidates": len(entries) - len(measured), "last_logged_at": last_logged,
            "days_since_last_entry": days_since, "stale": days_since is None or days_since > stale_days,
            "projects_with_unlogged_activity": unlogged, "candidates_untested_30d": old_candidates,
            "watcher_last_run": last_run, "days_since_watcher_run": watcher_days,
            "watcher_overdue": watcher_days is None or watcher_days >= WATCHER_EVERY_DAYS}


def _print_table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: min(40, max(len(c), *(len(str(r.get(c, ""))) for r in rows))) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, ""))[:40].ljust(widths[c]) for c in cols))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ledger", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="log a measured experiment or a candidate idea")
    a.add_argument("--json", help="full entry as JSON (or '-' for stdin); flags override")
    for f in ("project", "host", "hardware", "model", "model_family", "quant", "runtime", "lever",
              "lever_category", "change", "metric", "unit", "better", "quality_check", "outcome",
              "status", "source", "notes", "agent", "date"):
        a.add_argument(f"--{f.replace('_', '-')}", dest=f)
    a.add_argument("--before", type=float)
    a.add_argument("--after", type=float)
    a.add_argument("--quality-ok", dest="quality_ok", choices=["true", "false"])
    a.add_argument("--kept", choices=["true", "false"])

    q = sub.add_parser("query", help="filter entries")
    for f in ("project", "host", "runtime", "model", "model_family", "lever", "outcome", "status"):
        q.add_argument(f"--{f.replace('_', '-')}", dest=f)
    q.add_argument("--json", action="store_true")

    s = sub.add_parser("suggest", help="rank levers to try for a context")
    s.add_argument("--host")
    s.add_argument("--runtime")
    s.add_argument("--model-family", dest="model_family")
    s.add_argument("--model")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--json", action="store_true")

    h = sub.add_parser("health", help="staleness report")
    h.add_argument("--json", action="store_true")
    h.add_argument("--stale-days", type=int, default=14)

    w = sub.add_parser("watcher-done", help="record that a WATCHER.md pass finished (resets the weekly reminder)")
    w.add_argument("--notes")

    sub.add_parser("stats", help="counts by host, runtime, lever")
    args = ap.parse_args(argv)

    try:
        entries = load()
        if args.cmd == "add":
            base = {}
            if args.json:
                base = json.loads(sys.stdin.read() if args.json == "-" else args.json)
            flags = {k: v for k, v in vars(args).items() if k not in ("cmd", "json") and v is not None}
            for k in ("quality_ok", "kept"):
                if k in flags:
                    flags[k] = flags[k] == "true"
            e = append({**base, **flags})
            print(f"logged {e['id']}: {e['lever']} on {e.get('host', '-')} -> {e.get('outcome', e['status'])}"
                  + (f" ({e['delta_pct']:+.1f}%)" if "delta_pct" in e else ""))
        elif args.cmd == "query":
            filt = {k: v for k, v in vars(args).items() if k not in ("cmd", "json")}
            rows = [e for e in entries if _match(e, filt)]
            if args.json:
                print(json.dumps(rows, indent=1))
            else:
                _print_table(rows, ["date", "host", "runtime", "model", "lever", "outcome", "delta_pct", "project"])
        elif args.cmd == "suggest":
            r = suggest(entries, args.host, args.runtime, args.model_family, args.model, args.top)
            if args.json:
                print(json.dumps(r, indent=1))
            else:
                print("== Try (ranked by past results, weighted to this context)")
                _print_table(r["try"], ["lever", "score", "n", "best_gain_pct", "tried_on_host"])
                print("\n== Avoid on this host (only ever lost/broke here)")
                _print_table(r["avoid_on_this_host"], ["lever", "n", "examples"])
                print("\n== Untested candidates (ideas from experts/X/Reddit, never measured)")
                _print_table(r["untested_candidates"], ["lever", "runtime", "change", "source"])
        elif args.cmd == "health":
            r = health(entries, stale_days=args.stale_days)
            if args.json:
                print(json.dumps(r, indent=1))
            else:
                print(f"entries={r['entries']} measured={r['measured']} candidates={r['candidates']}")
                print(f"last entry: {r['last_logged_at']} ({r['days_since_last_entry']} days ago)"
                      + ("  ** STALE **" if r["stale"] else ""))
                print("\nProjects with activity newer than their last ledger entry:")
                _print_table(r["projects_with_unlogged_activity"], ["project", "progress_modified", "last_ledger_entry"])
                print("\nCandidates untested for 30+ days:")
                _print_table(r["candidates_untested_30d"], ["lever", "age_days"])
                print(f"\nwatcher last run: {r['watcher_last_run'] or 'never'}"
                      + (f" ({r['days_since_watcher_run']} days ago)" if r["watcher_last_run"] else "")
                      + ("  ** OVERDUE: follow WATCHER.md **" if r["watcher_overdue"] else ""))
        elif args.cmd == "watcher-done":
            run = record_watcher_run(args.notes, len(entries))
            print(f"watcher run recorded at {run['ran_at']}; next due in {WATCHER_EVERY_DAYS} days")
        elif args.cmd == "stats":
            for key in ("host", "runtime", "lever", "outcome", "agent"):
                counts: dict[str, int] = defaultdict(int)
                for e in entries:
                    counts[str(e.get(key, "-"))] += 1
                print(f"{key}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    except (LedgerError, json.JSONDecodeError) as exc:
        print(f"ledger: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
