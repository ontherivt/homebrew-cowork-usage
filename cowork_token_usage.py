#!/usr/bin/env python3
"""
cowork_token_usage.py — Walk Claude Code / Cowork session jsonl transcripts and
summarize token usage per session.

Usage:
    python3 cowork_token_usage.py
    python3 cowork_token_usage.py --root "/path/to/sessions" --csv usage.csv
    python3 cowork_token_usage.py --by-model

What it does:
    Recursively finds *.jsonl files under the sessions root (default:
    ~/Library/Application Support/Claude/local-agent-mode-sessions). For each
    line, looks for a `usage` object on assistant messages and sums:
        input_tokens, output_tokens,
        cache_creation_input_tokens, cache_read_input_tokens

    Prints a per-file table and a grand total. Optionally writes CSV.

Caveats:
    - I was not able to inspect the actual jsonl shape from inside the sandbox,
      so the parser tries several common locations for the usage block. If your
      files store it elsewhere, run with --debug to see what the parser sees.
    - audit.jsonl files are skipped by default (different schema). Include them
      with --include-audit if you want.
    - This counts tokens recorded by the local app. It is not authoritative
      billing data — use the Anthropic Console for that.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"

# Config file path. Written by the Homebrew formula on install with the
# analyzer URL and token baked in. Hand-editable too — just KEY=value pairs,
# one per line, with optional # comments.
CONFIG_PATH = Path.home() / ".config" / "cowork-usage" / "config.env"


def _load_config_file() -> dict[str, str]:
    """Load KEY=value pairs from CONFIG_PATH if it exists. Best-effort: a
    malformed file shouldn't crash the script. Empty dict if file missing."""
    out: dict[str, str] = {}
    if not CONFIG_PATH.exists():
        return out
    try:
        for line in CONFIG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            # Strip optional surrounding quotes
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            out[k.strip()] = v
    except OSError:
        pass
    return out


def _resolve(name: str, config: dict[str, str]) -> str:
    """Resolution order for analyzer URL/token: env var > config file > empty."""
    return os.environ.get(name) or config.get(name, "")

USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# USD per million tokens, per claude.com/pricing as of May 2026. VERIFY before
# trusting cost numbers — rates change. Override with --pricing-file <json>.
#
# Note: the matcher in pricing_for_model() does substring match on the model
# string, longest-key-first. So "opus-4-7" wins over "opus-4" for an Opus 4.7
# model, but plain "claude-opus-4" still hits the legacy Opus rate.
#
# Cache write defaults to 5-minute TTL (1.25× input). 1-hour TTL is 2× input;
# the jsonl usually doesn't distinguish, so we err cheap. Use --cache-ttl 1h
# if you know your sessions use 1-hour caches.
#
# Caveat: Opus 4.7 uses a new tokenizer and may consume up to 35% more tokens
# than older models for the same text. Cost-per-task comparisons across model
# families should be taken with a grain of salt.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # match -> {input, output, cache_write_5m, cache_write_1h, cache_read}
    # Opus 4.5+ — cheaper Opus tier ($5 input)
    "opus-4-7":   {"input":  5.00, "output": 25.00, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50},
    "opus-4-6":   {"input":  5.00, "output": 25.00, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50},
    "opus-4-5":   {"input":  5.00, "output": 25.00, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50},
    # Opus 4 / 4.1 — legacy ($15 input)
    "opus-4-1":   {"input": 15.00, "output": 75.00, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50},
    "opus-4":     {"input": 15.00, "output": 75.00, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50},
    # Opus 3 (deprecated)
    "opus-3":     {"input": 15.00, "output": 75.00, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50},
    # Sonnet 4.x and 3.7 (all same rate)
    "sonnet-4-6": {"input":  3.00, "output": 15.00, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
    "sonnet-4-5": {"input":  3.00, "output": 15.00, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
    "sonnet-4":   {"input":  3.00, "output": 15.00, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
    "sonnet-3-7": {"input":  3.00, "output": 15.00, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
    # Haiku
    "haiku-4-5":  {"input":  1.00, "output":  5.00, "cache_write_5m":  1.25, "cache_write_1h":  2.00, "cache_read": 0.10},
    "haiku-3-5":  {"input":  0.80, "output":  4.00, "cache_write_5m":  1.00, "cache_write_1h":  1.60, "cache_read": 0.08},
    "haiku-3":    {"input":  0.25, "output":  1.25, "cache_write_5m":  0.30, "cache_write_1h":  0.50, "cache_read": 0.03},
    # Fallback if model string is missing or unknown — Sonnet rate (most common)
    "_default":   {"input":  3.00, "output": 15.00, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
}

# Skipping these subtrees during the file walk turns a multi-minute scan into
# a sub-second one — they're plugin/marketplace caches and per-session user
# scratch dirs with thousands of files but no session transcripts.
PRUNE_DIRS = {
    # plugin / marketplace caches (the original culprits)
    "cowork_plugins", ".local-plugins", "marketplaces", "skills-plugin",
    # per-session user-facing scratch dirs (no jsonl transcripts here)
    "outputs", "uploads", "todos", ".projects", "cache",
    # standard project noise
    "node_modules", ".git",
}

# A "synthetic" assistant message is generated locally by Claude Code (e.g.
# compaction notes, hook-injected text) without an API call. Token counts
# are zero, so they cost $0 — keep them in the breakdown for visibility but
# don't surface them as "unmatched-pricing" warnings.
SYNTHETIC_MODELS = {"<synthetic>"}


@dataclass
class Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    assistant_messages: int = 0
    by_model: dict[str, "Totals"] = field(default_factory=dict)

    def add(self, usage: dict[str, Any], model: str | None = None) -> None:
        self.assistant_messages += 1
        for k in USAGE_KEYS:
            v = usage.get(k)
            if isinstance(v, int):
                setattr(self, k, getattr(self, k) + v)
        if model:
            sub = self.by_model.setdefault(model, Totals())
            sub.assistant_messages += 1
            for k in USAGE_KEYS:
                v = usage.get(k)
                if isinstance(v, int):
                    setattr(sub, k, getattr(sub, k) + v)

    @property
    def billed_input(self) -> int:
        # Cache-read tokens are billed at ~10% but we report raw counts.
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def total(self) -> int:
        return self.billed_input + self.output_tokens


def find_usage(obj: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return (usage_dict, model) if this jsonl row looks like an assistant turn."""
    if not isinstance(obj, dict):
        return None, None
    # Common Claude Code shape: {"type":"assistant","message":{"usage":{...},"model":"..."}}
    msg = obj.get("message")
    if isinstance(msg, dict):
        u = msg.get("usage")
        if isinstance(u, dict):
            return u, msg.get("model") or obj.get("model")
    # Fallback: usage at the top level
    u = obj.get("usage")
    if isinstance(u, dict):
        return u, obj.get("model")
    return None, None


def iter_jsonl_files(
    root: Path,
    include_audit: bool,
    max_age_seconds: float | None = None,
) -> Iterable[Path]:
    """Walk the sessions tree, pruning known-irrelevant subtrees and (optionally)
    skipping files whose mtime is older than max_age_seconds."""
    cutoff = time.time() - max_age_seconds if max_age_seconds else None
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune big plugin/marketplace caches in-place
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        for fn in filenames:
            if not fn.endswith(".jsonl"):
                continue
            if not include_audit and fn == "audit.jsonl":
                continue
            p = Path(dirpath) / fn
            if cutoff is not None:
                try:
                    if p.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
            yield p


def scan_file(path: Path, debug: bool = False, quiet: bool = False) -> Totals:
    totals = Totals()
    size = 0
    try:
        size = path.stat().st_size
    except OSError:
        pass
    if not quiet:
        print(f"  scanning {size/1_000_000:7.1f} MB  {session_id_from_path(path, lookup_session_name(path))}",
              file=sys.stderr, flush=True)
    # Dedupe identical assistant entries. When a parent session fans out to N
    # parallel subagents, Cowork records the parent's API call once per
    # tool_use block — and these are interleaved with the user-role tool_result
    # entries from each subagent. So the duplicates are NOT necessarily
    # consecutive in the jsonl; they're separated by user rows.
    #
    # Strategy: track the most recent assistant usage tuple. User/zero-usage
    # rows do NOT reset it. An assistant row with the same (input, output,
    # cache_w, cache_r) as the last seen assistant row is treated as the same
    # logical API call and skipped.
    #
    # Why this is safe: for two genuine API calls to have identical token
    # counts across all four fields, the inputs would have to be byte-identical
    # (which they essentially never are due to timestamps/tool_results in the
    # context). So matching all four fields is a reliable fanout-artifact
    # fingerprint.
    last_assistant_usage_key: tuple | None = None
    dedup_skipped = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    if debug:
                        print(f"  [parse-fail] {path}:{ln}", file=sys.stderr, flush=True)
                    continue
                usage, model = find_usage(obj)

                role = None
                if isinstance(obj.get("message"), dict):
                    role = obj["message"].get("role")
                elif obj.get("type") in ("user", "assistant"):
                    role = obj["type"]

                if usage is None:
                    # User/system/tool_result rows — don't reset dedup tracker
                    continue

                key = (
                    usage.get("input_tokens", 0) or 0,
                    usage.get("output_tokens", 0) or 0,
                    usage.get("cache_creation_input_tokens", 0) or 0,
                    usage.get("cache_read_input_tokens", 0) or 0,
                )
                if (role == "assistant"
                        and key == last_assistant_usage_key
                        and any(key)):
                    dedup_skipped += 1
                    continue
                if role == "assistant":
                    last_assistant_usage_key = key

                totals.add(usage, model)
                if debug and totals.assistant_messages == 1:
                    print(f"  [first usage] {path.name}: {usage} (model={model})",
                          file=sys.stderr, flush=True)
    except OSError as e:
        print(f"  [io-error] {path}: {e}", file=sys.stderr, flush=True)
    if debug and dedup_skipped:
        print(f"  [dedup] {path.name}: collapsed {dedup_skipped} duplicate "
              f"assistant entries (subagent fanout artifact)",
              file=sys.stderr, flush=True)
    if not quiet:
        print(f"    -> {totals.assistant_messages} msgs, {totals.total:,} tokens", file=sys.stderr, flush=True)
    return totals


def fmt(n: int) -> str:
    return f"{n:>14,}"


def print_table(
    rows: list[tuple[str, Totals, Path]] | list[tuple[str, Totals]],
    by_model: bool,
    show_cost: bool = False,
    pricing: dict[str, dict[str, float]] | None = None,
    cache_ttl: str = "5m",
) -> set[str]:
    """Print the table and return the union of unmatched model strings.
    Accepts rows as 2-tuples (label, totals) or 3-tuples (label, totals, path);
    the path is ignored for display."""
    cost_col = f" {'cost USD':>10}" if show_cost else ""
    LABEL_W = 80
    header = (
        f"{'session/file':<{LABEL_W}} {'msgs':>6} "
        f"{'input':>14} {'output':>14} {'cache_w':>14} {'cache_r':>14} {'total':>14}{cost_col}"
    )
    print(header)
    print("-" * len(header))
    unmatched_all: set[str] = set()
    for row in rows:
        label, t = row[0], row[1]
        # Truncate from the END so the human-readable name (which we put first
        # in session_id_from_path) is preserved.
        label_short = label if len(label) <= LABEL_W else label[: LABEL_W - 1] + "…"
        line = (
            f"{label_short:<{LABEL_W}} {t.assistant_messages:>6} "
            f"{fmt(t.input_tokens)} {fmt(t.output_tokens)} "
            f"{fmt(t.cache_creation_input_tokens)} {fmt(t.cache_read_input_tokens)} "
            f"{fmt(t.total)}"
        )
        if show_cost and pricing is not None:
            cost, unmatched = cost_for_totals(t, pricing, cache_ttl)
            unmatched_all |= unmatched
            line += f" {cost:>10.2f}"
        print(line)
        if by_model and t.by_model:
            for model, mt in sorted(t.by_model.items()):
                row = (
                    f"  └─ {model[: LABEL_W - 5]:<{LABEL_W - 5}} {mt.assistant_messages:>6} "
                    f"{fmt(mt.input_tokens)} {fmt(mt.output_tokens)} "
                    f"{fmt(mt.cache_creation_input_tokens)} {fmt(mt.cache_read_input_tokens)} "
                    f"{fmt(mt.total)}"
                )
                if show_cost and pricing is not None:
                    sub_cost, sub_unmatched = cost_for_totals(mt, pricing, cache_ttl, model=model)
                    unmatched_all |= sub_unmatched
                    row += f" {sub_cost:>10.2f}"
                print(row)
    return unmatched_all


def extract_deep_dive(path: Path, max_first_message_chars: int = 4000,
                      max_turns_in_stats: int = 200) -> dict[str, Any] | None:
    """Read a session jsonl and return the first user message (truncated) plus
    per-turn token statistics — NO assistant responses, NO subsequent user
    messages. This is the minimum content that lets the analyzer reason about
    cache patterns, context growth, and model fit."""
    first_user_msg: str | None = None
    turn_stats: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue

                # First user message only — extract text content
                if role == "user" and first_user_msg is None:
                    content = msg.get("content")
                    if isinstance(content, str):
                        first_user_msg = content[:max_first_message_chars]
                    elif isinstance(content, list):
                        parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                t = block.get("text", "")
                                if isinstance(t, str):
                                    parts.append(t)
                        if parts:
                            first_user_msg = "\n".join(parts)[:max_first_message_chars]

                # Per-turn stats (no content). Dedupe identical assistant rows
                # — including ones separated by user/tool_result rows. See the
                # comment in scan_file for the full rationale.
                if len(turn_stats) >= max_turns_in_stats:
                    continue
                usage = msg.get("usage") or {}
                stats: dict[str, Any] = {"turn": len(turn_stats) + 1, "role": role}
                if isinstance(usage, dict) and usage:
                    stats["tokens_in"] = usage.get("input_tokens", 0) or 0
                    stats["tokens_out"] = usage.get("output_tokens", 0) or 0
                    stats["cache_w"] = usage.get("cache_creation_input_tokens", 0) or 0
                    stats["cache_r"] = usage.get("cache_read_input_tokens", 0) or 0

                # Find the most recent assistant entry, even past user rows
                if role == "assistant":
                    last_asst = None
                    for prev in reversed(turn_stats):
                        if prev.get("role") == "assistant":
                            last_asst = prev
                            break
                    if (last_asst is not None
                            and last_asst.get("tokens_in") == stats.get("tokens_in")
                            and last_asst.get("tokens_out") == stats.get("tokens_out")
                            and last_asst.get("cache_w") == stats.get("cache_w")
                            and last_asst.get("cache_r") == stats.get("cache_r")
                            and any([stats.get("tokens_out"), stats.get("cache_w"),
                                     stats.get("cache_r"), stats.get("tokens_in")])):
                        last_asst["fanout_count"] = last_asst.get("fanout_count", 1) + 1
                        continue

                turn_stats.append(stats)
    except OSError:
        return None

    if first_user_msg is None and not turn_stats:
        return None
    return {
        "first_user_message": (first_user_msg or "")[:max_first_message_chars],
        "first_user_message_truncated": (
            first_user_msg is not None and len(first_user_msg) >= max_first_message_chars
        ),
        "turn_stats": turn_stats,
        "turn_stats_truncated": len(turn_stats) >= max_turns_in_stats,
    }


def build_analyzer_summary(
    rows: list[tuple[str, Totals, Path]],
    by_title: dict[str, Totals],
    grand: Totals,
    pricing: dict[str, dict[str, float]],
    cache_ttl: str,
    period_label: str,
    period_days: float,
    discount_pct: float,
    deep_dive_top_n: int = 3,
) -> dict[str, Any]:
    """Build the JSON payload sent to the analyzer Lambda. Contains aggregated
    numbers, workflow titles, and a deep-dive (first user message + per-turn
    stats) for the top N sessions by cost. Does NOT send assistant responses
    or any user message after the first."""
    total_cost, _ = cost_for_totals(grand, pricing, cache_ttl)

    by_model_out = []
    for model, mt in grand.by_model.items():
        if model in SYNTHETIC_MODELS:
            continue
        cost, _ = cost_for_totals(mt, pricing, cache_ttl, model=model)
        by_model_out.append({
            "model": model,
            "cost_usd": round(cost, 4),
            "tokens": {
                "input": mt.input_tokens,
                "output": mt.output_tokens,
                "cache_w": mt.cache_creation_input_tokens,
                "cache_r": mt.cache_read_input_tokens,
            },
        })
    by_model_out.sort(key=lambda r: r["cost_usd"], reverse=True)

    by_title_out = []
    for title, t in by_title.items():
        cost, _ = cost_for_totals(t, pricing, cache_ttl)
        by_title_out.append({
            "title": title,
            "cost_usd": round(cost, 4),
            "turns": t.assistant_messages,
            "models": sorted(m for m in t.by_model if m not in SYNTHETIC_MODELS),
        })
    by_title_out.sort(key=lambda r: r["cost_usd"], reverse=True)

    top_sessions_out = []
    deep_dives_out: list[dict[str, Any]] = []
    scored = [(label, t, p, cost_for_totals(t, pricing, cache_ttl)[0])
              for label, t, p in rows]
    scored.sort(key=lambda x: x[3], reverse=True)
    for i, (label, t, p, cost) in enumerate(scored[:5]):
        title = label.split(" · ")[0] if " · " in label else label
        # Pick the most-used model for this session as a single label
        model = max(t.by_model, key=lambda m: t.by_model[m].assistant_messages, default="")
        top_sessions_out.append({
            "title": title[:120],
            "cost_usd": round(cost, 4),
            "turns": t.assistant_messages,
            "model": model,
        })
        # Deep-dive into the top N — first user message + per-turn stats only
        if i < deep_dive_top_n:
            dive = extract_deep_dive(p)
            if dive:
                dive["title"] = title[:120]
                dive["cost_usd"] = round(cost, 4)
                dive["model"] = model
                deep_dives_out.append(dive)

    return {
        "period": period_label,
        "period_days": period_days,
        "total_cost_usd": round(total_cost, 4),
        "discount_pct": discount_pct,
        "session_count": len(rows),
        "assistant_turns": grand.assistant_messages,
        "by_model": by_model_out,
        "by_title": by_title_out,
        "top_sessions": top_sessions_out,
        "deep_dives": deep_dives_out,
    }


def call_analyzer_service(
    summary: dict[str, Any],
    url: str,
    token: str,
    timeout: float = 60.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """POST the summary to the analyzer Lambda. Returns (tips, critiques, error).
    On any failure returns ([], [], message) — caller decides whether to surface.
    If `token` is empty, no Authorization header is sent (analyzer may be running
    in unauthenticated mode behind URL secrecy + a Bedrock spend cap)."""
    body = json.dumps({"summary": summary}).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=req_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            err_body = ""
        return [], [], f"analyzer returned HTTP {e.code}: {err_body or e.reason}"
    except (urllib.error.URLError, TimeoutError) as e:
        return [], [], f"could not reach analyzer at {url}: {e}"
    except (json.JSONDecodeError, ValueError) as e:
        return [], [], f"analyzer returned non-JSON: {e}"

    tips = data.get("tips", [])
    critiques = data.get("prompt_critiques", [])
    if not isinstance(tips, list):
        tips = []
    if not isinstance(critiques, list):
        critiques = []
    return tips, critiques, None


def render_report(
    rows: list[tuple[str, Totals]],
    by_title: dict[str, Totals],
    grand: Totals,
    pricing: dict[str, dict[str, float]],
    cache_ttl: str,
    period_label: str,
    period_days: float = 0.0,
    discount_pct: float = 0.0,
    top_n: int = 5,
    ai_tips: list[dict[str, Any]] | None = None,
    ai_critiques: list[dict[str, Any]] | None = None,
    ai_error: str | None = None,
) -> None:
    """A clean, peer-friendly summary. Headline numbers, top workflows by cost,
    model split, and a few rule-based observations. No LLM involved."""
    total_cost, _ = cost_for_totals(grand, pricing, cache_ttl)

    def hr() -> None:
        print("─" * 72)

    print()
    print("═" * 72)
    print("  CLAUDE COWORK USAGE REPORT")
    print("═" * 72)
    print(f"  Period:            {period_label}")
    print(f"  Sessions scanned:  {len(rows):,}  ({grand.assistant_messages:,} assistant turns)")
    if discount_pct > 0:
        print(f"  Estimated cost:    ${total_cost:,.2f}  (after {discount_pct:.0f}% discount)")
    else:
        print(f"  Estimated cost:    ${total_cost:,.2f}")
    print()
    print("  Note: estimated from local logs at list-price rates. The Anthropic")
    print("  Console is authoritative. If your numbers there are different,")
    print("  re-run with --discount PCT to align (e.g. --discount 50).")
    print()

    # ── Top workflows ────────────────────────────────────────────────
    if by_title and total_cost > 0:
        title_costs: list[tuple[str, Totals, float]] = []
        for title, t in by_title.items():
            cost, _ = cost_for_totals(t, pricing, cache_ttl)
            title_costs.append((title, t, cost))
        title_costs.sort(key=lambda r: r[2], reverse=True)

        hr()
        print(f"  TOP WORKFLOWS BY COST  (top {min(top_n, len(title_costs))} of {len(title_costs)})")
        hr()
        print(f"  {'Workflow':<45} {'Cost':>10}  {'Share':>5}  {'Turns':>6}")
        print()
        top = title_costs[:top_n]
        for title, t, cost in top:
            pct = cost / total_cost * 100
            short = title if len(title) <= 45 else title[:42] + "..."
            print(f"  {short:<45} ${cost:>8,.2f}  {pct:>4.0f}%  {t.assistant_messages:>6,}")
        rest_cost = sum(c for _, _, c in title_costs[top_n:])
        rest_turns = sum(t.assistant_messages for _, t, _ in title_costs[top_n:])
        if rest_cost > 0.005:
            pct = rest_cost / total_cost * 100
            print(f"  {'(everything else)':<45} ${rest_cost:>8,.2f}  {pct:>4.0f}%  {rest_turns:>6,}")
        print()

    # ── Model mix ────────────────────────────────────────────────────
    if grand.by_model and total_cost > 0:
        model_costs = []
        for model, mt in grand.by_model.items():
            if model in SYNTHETIC_MODELS:
                continue
            cost, _ = cost_for_totals(mt, pricing, cache_ttl, model=model)
            model_costs.append((model, mt, cost))
        model_costs.sort(key=lambda r: r[2], reverse=True)
        hr()
        print("  COST BY MODEL")
        hr()
        print(f"  {'Model':<45} {'Cost':>10}  {'Share':>5}")
        print()
        for model, _mt, cost in model_costs:
            pct = cost / total_cost * 100
            print(f"  {model:<45} ${cost:>8,.2f}  {pct:>4.0f}%")
        print()

    # ── Observations (rule-based, no LLM) ────────────────────────────
    obs = _generate_observations(rows, by_title, grand, pricing, cache_ttl, total_cost, period_days)
    if obs:
        hr()
        print("  OBSERVATIONS")
        hr()
        for line in obs:
            _print_wrapped_bullet(line)
            print()

    # AI analysis (optional, opt-in via --analyze)
    if ai_tips:
        hr()
        print("  AI ANALYSIS  (sent: summary + first user message + per-turn stats for top 3 sessions)")
        hr()
        # Sort by claimed savings desc so the biggest ideas surface first
        for tip in sorted(ai_tips, key=lambda t: t.get("savings_usd", 0) or 0, reverse=True):
            title = (tip.get("title") or "").strip()
            sav = tip.get("savings_usd", 0) or 0
            rationale = (tip.get("rationale") or "").strip()
            heading = f"{title}  (~${sav:.0f}/period saved)" if sav > 0 else title
            print(f"  ▸ {heading}")
            if rationale:
                # 4-char hanging indent so rationale aligns with the heading text
                _print_wrapped_bullet(rationale, bullet=" ", indent="  ", width=66)
            print()
    elif ai_error:
        hr()
        print("  AI ANALYSIS")
        hr()
        print(f"  (skipped: {ai_error})")
        print()

    # Prompt-quality critiques (also from the analyzer)
    if ai_critiques:
        hr()
        print("  PROMPT QUALITY  (review of first user message in top sessions)")
        hr()
        for crit in ai_critiques:
            title = (crit.get("session_title") or "").strip()
            saved = crit.get("estimated_tokens_saved_per_run", 0) or 0
            heading_suffix = f"  (~{saved:,} tokens/run)" if saved > 0 else ""
            print(f"  ▸ {title}{heading_suffix}")
            for issue in crit.get("issues", []) or []:
                _print_wrapped_bullet(f"Issue: {issue}",
                                      bullet=" ", indent="  ", width=66)
            rewrite = (crit.get("suggested_rewrite") or "").strip()
            if rewrite:
                print()
                print(f"    Suggested rewrite:")
                # Cap displayed length, then wrap on word boundaries.
                shown = rewrite[:1200]
                # Preserve explicit paragraph breaks in the rewrite, but wrap
                # each paragraph on word boundaries so we don't split words.
                for paragraph in shown.split("\n"):
                    if not paragraph.strip():
                        print()
                        continue
                    print(textwrap.fill(
                        paragraph,
                        width=70,
                        initial_indent="      ",
                        subsequent_indent="      ",
                        break_long_words=False,
                        break_on_hyphens=False,
                    ))
                if len(rewrite) > 1200:
                    print(f"      […truncated; full rewrite is "
                          f"{len(rewrite):,} chars]")
            print()


def _print_wrapped_bullet(line: str, bullet: str = "•", indent: str = "    ", width: int = 66) -> None:
    """Manually wrap a paragraph at `width` characters, keeping the first line
    bulleted and continuation lines indented."""
    first = True
    words, cur = line.split(), ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            if first:
                print(f"  {bullet} {cur}")
                first = False
            else:
                print(f"  {indent}{cur}")
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        if first:
            print(f"  {bullet} {cur}")
        else:
            print(f"  {indent}{cur}")


def _cost_at_alt_model(
    t: Totals, alt_model: str, pricing: dict[str, dict[str, float]], cache_ttl: str
) -> float:
    """What would `t` cost if priced entirely at `alt_model`'s rates?"""
    rates, _ = pricing_for_model(alt_model, pricing)
    cw_key = "cache_write_1h" if cache_ttl == "1h" else "cache_write_5m"
    return _price_one(t, rates, cw_key)


def _generate_observations(
    rows: list[tuple[str, Totals, Path]] | list[tuple[str, Totals]],
    by_title: dict[str, Totals],
    grand: Totals,
    pricing: dict[str, dict[str, float]],
    cache_ttl: str,
    total_cost: float,
    period_days: float,
) -> list[str]:
    """Action-oriented observations. Each rule must (a) include a concrete
    dollar number and (b) imply a specific decision the reader could make.
    Rules that only describe state (cache ratio, output share) are omitted."""
    out: list[str] = []
    if total_cost <= 0:
        return out

    # 1. Run-rate projection — the headline number people actually want.
    if period_days > 0 and period_days < 365:
        monthly = total_cost * 30.4 / period_days
        annual = total_cost * 365.0 / period_days
        out.append(
            f"At this pace, you'd spend roughly ${monthly:,.0f}/month or "
            f"${annual:,.0f}/year. (Straight-line projection from this period.)"
        )

    # 2. Top swap candidate: which workflow would save the most by going Sonnet?
    #    We compute "what if this whole workflow had run on Sonnet?" and rank
    #    by the savings. This surfaces over-modeled work without scolding.
    swap_candidates: list[tuple[str, float, float]] = []  # (title, current, savings)
    for title, t in by_title.items():
        if title in ("(no title)", ""):
            continue
        # Skip if no Opus usage — nothing to swap from.
        has_opus = any("opus" in m.lower() for m in t.by_model)
        if not has_opus:
            continue
        cur, _ = cost_for_totals(t, pricing, cache_ttl)
        if cur < 5:
            continue
        # Use Sonnet 4.6 as the reference cheaper-but-capable tier.
        if_sonnet = _cost_at_alt_model(t, "claude-sonnet-4-6", pricing, cache_ttl)
        savings = cur - if_sonnet
        if savings >= 3:
            swap_candidates.append((title, cur, savings))
    swap_candidates.sort(key=lambda x: x[2], reverse=True)
    for title, cur, savings in swap_candidates[:2]:
        out.append(
            f'"{title}" cost ${cur:.0f} this period. If you ran it entirely on '
            f"Sonnet instead of Opus, it would cost about ${cur - savings:.0f} "
            f"(saving ${savings:.0f}). Worth A/B-ing one run to see if quality "
            f"holds."
        )

    # 3. Most expensive single session — with per-turn cost so it's a concrete decision.
    if rows:
        scored = [(row[0], row[1], cost_for_totals(row[1], pricing, cache_ttl)[0])
                  for row in rows]
        max_label, max_t, max_cost = max(scored, key=lambda x: x[2])
        if max_cost >= 10 and max_cost / total_cost >= 0.10:
            short = max_label.split(" · ")[0] if " · " in max_label else max_label[:55]
            pct = max_cost / total_cost * 100
            n = max_t.assistant_messages
            turn_word = "turn" if n == 1 else "turns"
            per_turn_clause = (
                f" (~${max_cost / n:.2f}/turn)" if n >= 5 else ""
            )
            out.append(
                f'Most expensive session: ${max_cost:.2f} ("{short}") in '
                f"{n} {turn_word}{per_turn_clause} — {pct:.0f}% of your "
                f"period's spend in one conversation. If the outcome was "
                f"worth it, fine; if not, splitting into shorter focused "
                f"sessions caps the downside."
            )

    # 4. Cache-write heaviness — actionable signal: lots of new contexts.
    cw_cost = 0.0
    for model, mt in grand.by_model.items():
        if model in SYNTHETIC_MODELS:
            continue
        rates, _ = pricing_for_model(model, pricing)
        cw_key = "cache_write_1h" if cache_ttl == "1h" else "cache_write_5m"
        cw_cost += mt.cache_creation_input_tokens * rates.get(cw_key, 0) / 1_000_000
    if total_cost > 0 and cw_cost / total_cost > 0.35:
        cw_pct = cw_cost / total_cost * 100
        out.append(
            f"Cache writes are {cw_pct:.0f}% of cost (~${cw_cost:.0f}). "
            f"That means you start many fresh contexts. Reusing a single "
            f"session for related follow-ups instead of starting new ones "
            f"would shift those writes into cheaper cache reads."
        )

    return out


def write_csv(
    path: Path,
    rows: list[tuple[str, Totals, Path]] | list[tuple[str, Totals]],
    pricing: dict[str, dict[str, float]] | None = None,
    cache_ttl: str = "5m",
) -> None:
    cost_cols = ["cost_usd"] if pricing else []
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "model", "assistant_messages", *USAGE_KEYS, "total", *cost_cols])
        for row in rows:
            label, t = row[0], row[1]
            extra = []
            if pricing:
                cost, _ = cost_for_totals(t, pricing, cache_ttl)
                extra = [f"{cost:.4f}"]
            w.writerow([label, "", t.assistant_messages, t.input_tokens, t.output_tokens,
                        t.cache_creation_input_tokens, t.cache_read_input_tokens, t.total, *extra])
            for model, mt in sorted(t.by_model.items()):
                extra = []
                if pricing:
                    sub_cost, _ = cost_for_totals(mt, pricing, cache_ttl, model=model)
                    extra = [f"{sub_cost:.4f}"]
                w.writerow([label, model, mt.assistant_messages, mt.input_tokens, mt.output_tokens,
                            mt.cache_creation_input_tokens, mt.cache_read_input_tokens, mt.total, *extra])


def pricing_for_model(model: str | None, pricing: dict[str, dict[str, float]]) -> tuple[dict[str, float], str]:
    """Return (rates, matched_key). Falls back to _default with a warning-flag key."""
    if not model:
        return pricing.get("_default", {}), "_default"
    m = model.lower()
    # Match longest matching key first ("sonnet-3-7" before "sonnet").
    candidates = sorted((k for k in pricing if k != "_default"), key=len, reverse=True)
    for key in candidates:
        if key in m:
            return pricing[key], key
    return pricing.get("_default", {}), "_default"


def _price_one(t: "Totals", rates: dict[str, float], cw_key: str) -> float:
    return (
        t.input_tokens                 * rates.get("input",      0) / 1_000_000 +
        t.output_tokens                * rates.get("output",     0) / 1_000_000 +
        t.cache_creation_input_tokens  * rates.get(cw_key,       0) / 1_000_000 +
        t.cache_read_input_tokens      * rates.get("cache_read", 0) / 1_000_000
    )


def cost_for_totals(
    t: "Totals",
    pricing: dict[str, dict[str, float]],
    cache_ttl: str,
    model: str | None = None,
) -> tuple[float, set[str]]:
    """Compute USD cost. Resolution order:
       1) if `model` is given, price t entirely at that model's rate;
       2) else if t.by_model is populated, sum each sub-totals at its model rate;
       3) else fall back to _default and flag <no-model-info>.
    Returns (cost_usd, set_of_unmatched_models)."""
    cw_key = "cache_write_1h" if cache_ttl == "1h" else "cache_write_5m"
    unmatched: set[str] = set()
    if model is not None:
        # Synthetic messages are zero-token by definition — don't flag them.
        if model in SYNTHETIC_MODELS:
            return 0.0, unmatched
        rates, matched = pricing_for_model(model, pricing)
        if matched == "_default":
            unmatched.add(model)
        return _price_one(t, rates, cw_key), unmatched
    if t.by_model:
        cost = 0.0
        for sub_model, mt in t.by_model.items():
            if sub_model in SYNTHETIC_MODELS:
                continue
            rates, matched = pricing_for_model(sub_model, pricing)
            if matched == "_default":
                unmatched.add(sub_model)
            cost += _price_one(mt, rates, cw_key)
        return cost, unmatched
    rates = pricing.get("_default", {})
    return _price_one(t, rates, cw_key), {"<no-model-info>"}


def parse_account_workspace(p: Path, root: Path) -> tuple[str, str] | None:
    """Extract (accountId, workspaceId) from a session file path.
    Expected layout: <root>/<accountId>/<workspaceId>/local_<sessionId>/...
    Returns None if the path doesn't match (e.g. user pointed --root somewhere
    non-standard like Claude Code's ~/.claude/projects)."""
    try:
        rel = p.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    # Skip if the path doesn't start with two non-special segments (account, workspace).
    if len(parts) < 3:
        return None
    a, w = parts[0], parts[1]
    if a.startswith(".") or a in PRUNE_DIRS or a.startswith("local_") or a.startswith("skills-"):
        return None
    return a, w


def lookup_account_label(root: Path, account_id: str, workspace_id: str) -> str:
    """Try to read a human-readable label (email/name) from settings JSON files
    inside the workspace dir. Returns an empty string if nothing useful found."""
    candidates = [
        root / account_id / workspace_id / "cowork_settings.json",
        root / account_id / workspace_id / ".claude.json",
    ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        # Try common field names — none of these are guaranteed to exist; we're
        # just probing. Add more if you find them.
        for key in ("userEmail", "email", "user", "label", "workspaceName",
                    "accountEmail", "owner"):
            v = data.get(key)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                for sub in ("email", "name", "label"):
                    sv = v.get(sub)
                    if isinstance(sv, str) and sv:
                        return sv
    return ""


_DATE_PREFIX_RE = None

def normalize_title(title: str, strip_date: bool) -> str:
    """Optionally strip a leading 'Mon DD – ' or 'Mon DD - ' so dated and
    undated runs of the same workflow group together."""
    if not strip_date or not title:
        return title
    global _DATE_PREFIX_RE
    if _DATE_PREFIX_RE is None:
        import re
        _DATE_PREFIX_RE = re.compile(
            r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}\s*[–-]\s*",
            re.IGNORECASE,
        )
    return _DATE_PREFIX_RE.sub("", title)


def lookup_session_name(jsonl_path: Path) -> str:
    """Try to find the user-facing session name (e.g. 'Daily obsidian workflow').
    The Cowork app appears to store this in `<workspace>/local_<sessionId>.json`,
    sibling to the local_<sessionId>/ directory. Returns '' if nothing found."""
    for ancestor in jsonl_path.parents:
        if ancestor.name.startswith("local_"):
            sibling = ancestor.parent / f"{ancestor.name}.json"
            try:
                data = json.loads(sibling.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                return ""
            if not isinstance(data, dict):
                return ""
            # Probe likely field names. If your build of Cowork uses different
            # keys, add them here — running with --debug-names dumps what's
            # actually in the file.
            for key in ("title", "name", "label", "displayName", "summary",
                        "sessionTitle", "userTitle"):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""
    return ""


def session_id_from_path(p: Path, name: str = "") -> str:
    """Pull the most useful identifier out of the path. If `name` is given,
    prepend it so the table shows e.g. 'My Project · slug/uuid.jsonl'."""
    parts = p.parts
    try:
        i = parts.index("projects")
        slug = parts[i + 1].lstrip("-")
        rest = "/".join(parts[i + 2:])
        path_label = f"{slug}/{rest}"
    except (ValueError, IndexError):
        path_label = p.name
    if name:
        return f"{name} · {path_label}"
    return path_label


def main() -> int:
    # Load config file once, up front, so any argparse defaults can read from it.
    _config = _load_config_file()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                    help=f"sessions root (default: {DEFAULT_ROOT})")
    ap.add_argument("--csv", type=Path, default=None, help="also write a CSV here")
    ap.add_argument("--by-model", action="store_true", help="break out totals by model")
    ap.add_argument("--include-audit", action="store_true", help="include audit.jsonl files (different schema)")
    ap.add_argument("--top", type=int, default=0, help="show only the top N sessions by total tokens")
    ap.add_argument("--min-tokens", type=int, default=0, help="hide files with fewer total tokens than this")
    ap.add_argument("--debug", action="store_true", help="print parse failures and first-usage samples")
    ap.add_argument("--quiet", action="store_true", help="suppress per-file progress output")
    ap.add_argument("--max-size-mb", type=float, default=0,
                    help="skip files larger than this many MB (0 = no limit)")
    ap.add_argument("--cost", action="store_true", help="estimate USD cost using built-in pricing table")
    ap.add_argument("--pricing-file", type=Path, default=None,
                    help="JSON file overriding the pricing table (same shape as DEFAULT_PRICING)")
    ap.add_argument("--cache-ttl", choices=["5m", "1h"], default="5m",
                    help="assumed cache TTL for pricing cache writes (default: 5m)")
    ap.add_argument("--sort", choices=["tokens", "cost"], default="tokens",
                    help="sort rows by raw tokens or by estimated cost (default: tokens)")
    ap.add_argument("--days", type=float, default=7.0,
                    help="only include files modified in the last N days "
                         "(0 = no limit; default: 7)")
    ap.add_argument("--by-account", action="store_true",
                    help="also print a breakdown by Cowork account/workspace "
                         "(extracted from path; reads cowork_settings.json if present)")
    ap.add_argument("--group-by-title", action="store_true",
                    help="also print a breakdown grouped by session title — "
                         "shows which recurring workflows cost the most")
    ap.add_argument("--strip-title-date", action="store_true", default=True,
                    help="when grouping by title, strip leading date prefixes "
                         "like 'Mar 17 – ' so dated and undated runs of the same "
                         "workflow group together (default: on)")
    ap.add_argument("--no-strip-title-date", action="store_false", dest="strip_title_date",
                    help="disable date-prefix stripping (treat 'Mar 17 – X' and 'X' as distinct)")
    ap.add_argument("--detailed", action="store_true",
                    help="show the full per-session tables instead of the "
                         "clean summary report (the report is the default)")
    ap.add_argument(
        "--discount", type=float,
        default=float(_resolve("COWORK_DISCOUNT_PCT", _config) or 0.0),
        metavar="PCT",
        help="apply a flat percent discount to all cost estimates "
             "(e.g. --discount 50 halves the numbers). Reads "
             "COWORK_DISCOUNT_PCT from env or ~/.config/cowork-usage/config.env "
             "if set; otherwise defaults to 0 (raw list price — matches the "
             "common case where you're billed at standard Bedrock rates). "
             "To calibrate to your actual billing: run with --discount 0, "
             "compare to your Anthropic Console for the same period, then "
             "set the discount to (1 - actual/list) × 100.")
    ap.add_argument("--analyze", action="store_true",
                    help="POST the summary to the cowork-analyzer Lambda and "
                         "show AI-generated cost-reduction tips. Reads URL/token "
                         "from $COWORK_ANALYZER_URL / $COWORK_ANALYZER_TOKEN.")
    ap.add_argument("--analyzer-url", default=_resolve("COWORK_ANALYZER_URL", _config),
                    help="override the analyzer Lambda URL "
                         "(default: $COWORK_ANALYZER_URL or config file)")
    ap.add_argument("--analyzer-token", default=_resolve("COWORK_ANALYZER_TOKEN", _config),
                    help="override the analyzer bearer token "
                         "(default: $COWORK_ANALYZER_TOKEN or config file)")
    args = ap.parse_args()
    if args.discount < 0 or args.discount >= 100:
        print("--discount must be between 0 and 99 (it's a percent).", file=sys.stderr)
        return 2

    # By default we render the friendly summary report. Pass --detailed to get
    # the verbose per-session tables instead.
    args.report = not args.detailed
    if args.report:
        args.cost = True
        args.group_by_title = True
        args.quiet = True

    pricing = None
    if args.cost or args.pricing_file:
        pricing = dict(DEFAULT_PRICING)
        if args.pricing_file:
            try:
                override = json.loads(args.pricing_file.read_text())
                if not isinstance(override, dict):
                    raise ValueError("pricing file must be a JSON object")
                pricing.update(override)
            except (OSError, ValueError, json.JSONDecodeError) as e:
                print(f"Failed to load --pricing-file: {e}", file=sys.stderr)
                return 2
        # Apply discount uniformly across all rate types and all models.
        if args.discount > 0:
            mult = 1.0 - args.discount / 100.0
            pricing = {
                model: {k: v * mult for k, v in rates.items()}
                for model, rates in pricing.items()
            }

    if not args.root.exists():
        print(f"Root does not exist: {args.root}", file=sys.stderr)
        return 2

    max_age_seconds = args.days * 86400 if args.days else None
    files = sorted(iter_jsonl_files(args.root, args.include_audit, max_age_seconds))
    if not files:
        msg = f"No .jsonl files under {args.root}"
        if max_age_seconds:
            msg += f" modified in the last {args.days} day(s) — try --days 0 for all-time."
        print(msg)
        return 0

    age_note = f"(modified in last {args.days} day(s))" if max_age_seconds else "(all-time)"
    if not args.quiet:
        print(f"Scanning {len(files)} jsonl files under {args.root} {age_note}\n",
              file=sys.stderr)

    # Sort by size descending so we see the biggest contributors first
    files_sized = []
    for p in files:
        try:
            s = p.stat().st_size
        except OSError:
            s = 0
        if args.max_size_mb and s > args.max_size_mb * 1_000_000:
            print(f"  skipping {s/1_000_000:.1f} MB {p.name} (above --max-size-mb)", file=sys.stderr, flush=True)
            continue
        files_sized.append((s, p))
    files_sized.sort(reverse=True)
    if not args.quiet:
        print(f"Total bytes to scan: {sum(s for s,_ in files_sized)/1_000_000:.1f} MB across {len(files_sized)} files\n",
              file=sys.stderr, flush=True)

    rows: list[tuple[str, Totals, Path]] = []
    grand = Totals()
    by_account: dict[tuple[str, str], Totals] = {}
    by_title: dict[str, Totals] = {}
    for _, p in files_sized:
        t = scan_file(p, debug=args.debug, quiet=args.quiet)
        if t.total < args.min_tokens:
            continue
        title = lookup_session_name(p)
        rows.append((session_id_from_path(p, title), t, p))
        # merge into grand
        grand.assistant_messages += t.assistant_messages
        for k in USAGE_KEYS:
            setattr(grand, k, getattr(grand, k) + getattr(t, k))
        for model, mt in t.by_model.items():
            sub = grand.by_model.setdefault(model, Totals())
            sub.assistant_messages += mt.assistant_messages
            for k in USAGE_KEYS:
                setattr(sub, k, getattr(sub, k) + getattr(mt, k))
        # merge into by-account if requested
        if args.by_account:
            aw = parse_account_workspace(p, args.root)
            if aw is not None:
                acct_t = by_account.setdefault(aw, Totals())
                acct_t.assistant_messages += t.assistant_messages
                for k in USAGE_KEYS:
                    setattr(acct_t, k, getattr(acct_t, k) + getattr(t, k))
                for model, mt in t.by_model.items():
                    sub = acct_t.by_model.setdefault(model, Totals())
                    sub.assistant_messages += mt.assistant_messages
                    for k in USAGE_KEYS:
                        setattr(sub, k, getattr(sub, k) + getattr(mt, k))
        # merge into by-title if requested
        if args.group_by_title:
            key = normalize_title(title, args.strip_title_date) or "(no title)"
            tt = by_title.setdefault(key, Totals())
            tt.assistant_messages += t.assistant_messages
            for k in USAGE_KEYS:
                setattr(tt, k, getattr(tt, k) + getattr(t, k))
            for model, mt in t.by_model.items():
                sub = tt.by_model.setdefault(model, Totals())
                sub.assistant_messages += mt.assistant_messages
                for k in USAGE_KEYS:
                    setattr(sub, k, getattr(sub, k) + getattr(mt, k))

    if args.sort == "cost" and pricing is not None:
        rows.sort(key=lambda r: cost_for_totals(r[1], pricing, args.cache_ttl)[0], reverse=True)
    else:
        rows.sort(key=lambda r: r[1].total, reverse=True)
    if args.top:
        rows = rows[: args.top]

    show_cost = pricing is not None

    # --report mode produces ONLY the friendly summary; skip the giant tables.
    if args.report:
        period_label = (
            f"Last {args.days:g} day(s)" if args.days else "All time"
        )
        # Optional AI analysis — fully optional, never blocks the report
        ai_tips: list[dict[str, Any]] | None = None
        ai_critiques: list[dict[str, Any]] | None = None
        ai_error: str | None = None
        if args.analyze:
            if not args.analyzer_url:
                ai_error = (
                    "--analyze needs COWORK_ANALYZER_URL. Set it via env var, "
                    "~/.config/cowork-usage/config.env, or --analyzer-url. "
                    "(COWORK_ANALYZER_TOKEN is optional — only required if "
                    "the Lambda is configured for token auth.)"
                )
            else:
                summary = build_analyzer_summary(
                    rows, by_title, grand, pricing, args.cache_ttl,
                    period_label, args.days, args.discount,
                )
                ai_tips, ai_critiques, ai_error = call_analyzer_service(
                    summary, args.analyzer_url, args.analyzer_token,
                )
        render_report(rows, by_title, grand, pricing, args.cache_ttl,
                      period_label, period_days=args.days,
                      discount_pct=args.discount,
                      ai_tips=ai_tips, ai_critiques=ai_critiques,
                      ai_error=ai_error)
        if args.csv:
            write_csv(args.csv, rows + [("__grand_total__", grand)],
                      pricing=pricing, cache_ttl=args.cache_ttl)
            print(f"\nDetailed CSV written to {args.csv}", file=sys.stderr)
        return 0

    unmatched = print_table(rows, by_model=args.by_model, show_cost=show_cost,
                            pricing=pricing, cache_ttl=args.cache_ttl)
    print()

    if args.group_by_title and by_title:
        title_rows: list[tuple[str, Totals]] = []
        for title, t in by_title.items():
            label = f"{title}  ({t.assistant_messages} msgs)"
            title_rows.append((label, t))
        if args.sort == "cost" and pricing is not None:
            title_rows.sort(key=lambda r: cost_for_totals(r[1], pricing, args.cache_ttl)[0], reverse=True)
        else:
            title_rows.sort(key=lambda r: r[1].total, reverse=True)
        print("BY TITLE  (recurring workflows aggregated"
              + (", date prefixes stripped" if args.strip_title_date else "") + ")")
        unmatched |= print_table(title_rows, by_model=args.by_model, show_cost=show_cost,
                                 pricing=pricing, cache_ttl=args.cache_ttl)
        print()

    if args.by_account and by_account:
        # Build labeled rows for each (account, workspace), keeping totals sorted
        # by total tokens descending.
        acct_rows: list[tuple[str, Totals]] = []
        for (acct, ws), t in by_account.items():
            label = lookup_account_label(args.root, acct, ws)
            short = f"{acct[:8]}.../{ws[:8]}..."
            display = f"{short}  {label}" if label else short
            acct_rows.append((display, t))
        acct_rows.sort(key=lambda r: r[1].total, reverse=True)
        print("BY ACCOUNT / WORKSPACE")
        unmatched |= print_table(acct_rows, by_model=args.by_model, show_cost=show_cost,
                                 pricing=pricing, cache_ttl=args.cache_ttl)
        print()

    unmatched |= print_table([("GRAND TOTAL", grand)], by_model=args.by_model,
                             show_cost=show_cost, pricing=pricing, cache_ttl=args.cache_ttl)

    if show_cost:
        print(
            f"\nCost estimate uses built-in pricing (cache TTL = {args.cache_ttl}). "
            f"VERIFY against https://www.anthropic.com/pricing — rates change.",
            file=sys.stderr,
        )
        if unmatched:
            print(
                f"WARNING: priced these models at the fallback (Sonnet) rate — "
                f"override with --pricing-file if wrong: {sorted(unmatched)}",
                file=sys.stderr,
            )

    if args.csv:
        write_csv(args.csv, rows + [("__grand_total__", grand)],
                  pricing=pricing, cache_ttl=args.cache_ttl)
        print(f"\nCSV written to {args.csv}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
