"""Build a 1,000-message stratified evaluation dataset from Teams + Telegram dumps.

Usage:
    uv run python build_eval_dataset.py

Outputs (in the same directory as this script):
    eval_dataset_unlabeled.json   — 1,000 sampled items for LLM eval
    data_analysis_report.json     — full data-analysis stats
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TEAMS_PATH = Path(
    os.path.expanduser("~/.config/ingest/output/2026-03-13T16-35.json")
)
TELEGRAM_PATH = Path(
    os.path.expanduser(
        "~/.config/ingest/output/telegram-personal-2026-03-16T03-13.json"
    )
)
SCRIPT_DIR = Path(__file__).resolve().parent
SAMPLE_SIZE = 1_000
MIN_PER_CHANNEL = 5
SEED = 42

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map
    "\U0001f1e0-\U0001f1ff"  # flags
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "\U0001f900-\U0001f9ff"  # supplemental
    "\U0001fa00-\U0001fa6f"  # chess
    "\U0001fa70-\U0001faff"  # symbols ext-A
    "\U00002600-\U000026ff"  # misc symbols
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0000200d"  # ZWJ
    "\U0000200b-\U0000200f"  # zero-width chars
    "\U0000205f"
    "\U00003000"
    "\U0000feff"
    "]+",
    flags=re.UNICODE,
)

DATE_PATTERNS = re.compile(
    r"""
      \d{1,2}/\d{1,2}/\d{2,4}            # DD/MM/YYYY or M/D/YY
    | \d{1,2}\.\d{1,2}\.\d{2,4}          # DD.MM.YYYY
    | \d{4}-\d{2}-\d{2}                   # YYYY-MM-DD
    | (?:January|February|March|April|May|June|July|August|
        September|October|November|December)\s+\d{1,2}
    | \d{1,2}\s+(?:January|February|March|April|May|June|July|
        August|September|October|November|December)
    | (?:due|deadline|tomorrow|next\s+week|next\s+monday|
        next\s+tuesday|next\s+wednesday|next\s+thursday|
        next\s+friday|next\s+saturday|next\s+sunday|
        this\s+friday|this\s+monday|by\s+friday|by\s+monday)
    """,
    re.IGNORECASE | re.VERBOSE,
)

SYSTEM_CARD_RE = re.compile(r"Card\s*[-–—]\s*access\s+it\s+on", re.IGNORECASE)
SCHEDULED_RE = re.compile(
    r"^[\w.+-]+@[\w.-]+\s+(?:Scheduled|Recurring)", re.MULTILINE
)


def estimate_tokens(text: str) -> int:
    """Whitespace-split word count * 1.3, rounded up."""
    if not text:
        return 0
    return math.ceil(len(text.split()) * 1.3)


def is_emoji_only(body: str) -> bool:
    """Return True when the body contains nothing besides emoji / whitespace."""
    stripped = EMOJI_RE.sub("", body).strip()
    return len(stripped) == 0


def classify_noise(item: dict[str, Any]) -> bool:
    """Return True when the message is likely noise."""
    body = (item.get("body") or "").strip()

    # Very short
    if len(body) < 5:
        return True

    # Emoji-only
    if is_emoji_only(body):
        return True

    # System-generated card
    if SYSTEM_CARD_RE.search(body):
        return True

    # Scheduled / recurring pattern
    if SCHEDULED_RE.search(body):
        return True

    # Author == "Assignments" (Teams auto-cards)
    if (item.get("author") or "").strip() == "Assignments":
        return True

    return False


def has_date_reference(body: str) -> bool:
    return bool(DATE_PATTERNS.search(body or ""))


# ---------------------------------------------------------------------------
# Load & filter
# ---------------------------------------------------------------------------
def load_messages(path: Path) -> list[dict[str, Any]]:
    print(f"Loading {path.name} …")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    items = data.get("items", [])
    raw = [i for i in items if i.get("content_type") == "raw_message"]
    print(f"  {len(items)} total items → {len(raw)} raw_messages")
    return raw


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyse(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Run all analyses, return report dict."""
    platform_counts: Counter[str] = Counter()
    channel_counts: Counter[str] = Counter()
    token_estimates: list[int] = []
    noise_count = 0
    date_ref_count = 0

    for msg in messages:
        platform = msg.get("platform", "unknown")
        channel = msg.get("course_name") or "(no channel)"
        body = msg.get("body") or ""

        platform_counts[platform] += 1
        channel_counts[channel] += 1

        tokens = estimate_tokens(body)
        token_estimates.append(tokens)

        if classify_noise(msg):
            noise_count += 1
        if has_date_reference(body):
            date_ref_count += 1

    sorted_tokens = sorted(token_estimates)
    total = len(messages)

    def percentile(data: list[int], pct: float) -> int:
        idx = int(math.ceil(pct / 100 * len(data))) - 1
        return data[max(0, idx)]

    report: dict[str, Any] = {
        "total_messages": total,
        "platform_counts": dict(platform_counts.most_common()),
        "token_distribution": {
            "min": sorted_tokens[0] if sorted_tokens else 0,
            "max": sorted_tokens[-1] if sorted_tokens else 0,
            "mean": round(statistics.mean(token_estimates), 1) if token_estimates else 0,
            "median": round(statistics.median(token_estimates), 1) if token_estimates else 0,
            "p95": percentile(sorted_tokens, 95) if sorted_tokens else 0,
        },
        "noise": {
            "count": noise_count,
            "ratio_pct": round(100 * noise_count / total, 2) if total else 0,
        },
        "date_references": {
            "count": date_ref_count,
            "ratio_pct": round(100 * date_ref_count / total, 2) if total else 0,
        },
        "channel_distribution": dict(channel_counts.most_common()),
    }
    return report


# ---------------------------------------------------------------------------
# Stratified sampling
# ---------------------------------------------------------------------------
def stratified_sample(
    messages: list[dict[str, Any]],
    n: int,
    min_per_channel: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Sample *n* messages proportional to channel volume."""
    rng = random.Random(seed)

    # Group by channel
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for msg in messages:
        ch = msg.get("course_name") or "(no channel)"
        by_channel.setdefault(ch, []).append(msg)

    total = len(messages)
    allocations: dict[str, int] = {}

    # First pass: proportional allocation with minimum guarantee
    for ch, ch_msgs in by_channel.items():
        proportional = max(1, round(n * len(ch_msgs) / total))
        if len(ch_msgs) >= min_per_channel:
            proportional = max(proportional, min_per_channel)
        # Can't sample more than available
        allocations[ch] = min(proportional, len(ch_msgs))

    # Adjust total to hit target n
    allocated = sum(allocations.values())
    if allocated > n:
        # Trim from largest channels
        channels_by_size = sorted(
            allocations, key=lambda c: allocations[c], reverse=True
        )
        for ch in channels_by_size:
            if allocated <= n:
                break
            reduction = min(
                allocated - n,
                allocations[ch] - (min_per_channel if len(by_channel[ch]) >= min_per_channel else 1),
            )
            if reduction > 0:
                allocations[ch] -= reduction
                allocated -= reduction
    elif allocated < n:
        # Add to largest channels that still have headroom
        channels_by_size = sorted(
            allocations, key=lambda c: len(by_channel[c]), reverse=True
        )
        for ch in channels_by_size:
            if allocated >= n:
                break
            headroom = len(by_channel[ch]) - allocations[ch]
            addition = min(n - allocated, headroom)
            if addition > 0:
                allocations[ch] += addition
                allocated += addition

    # Sample from each channel
    sampled: list[dict[str, Any]] = []
    for ch, count in allocations.items():
        pool = by_channel[ch]
        chosen = rng.sample(pool, min(count, len(pool)))
        sampled.extend(chosen)

    # Shuffle final order
    rng.shuffle(sampled)
    return sampled


# ---------------------------------------------------------------------------
# Build output item
# ---------------------------------------------------------------------------
def to_eval_item(msg: dict[str, Any]) -> dict[str, Any]:
    body = msg.get("body") or ""
    attachments = msg.get("attachments") or []
    return {
        "external_id": msg.get("external_id"),
        "platform": msg.get("platform"),
        "body": body,
        "body_html": msg.get("body_html"),
        "course_name": msg.get("course_name"),
        "author": msg.get("author"),
        "published_at": msg.get("published_at"),
        "attachments_count": len(attachments),
        "_token_estimate": estimate_tokens(body),
        "_is_noise": classify_noise(msg),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    teams = load_messages(TEAMS_PATH)
    telegram = load_messages(TELEGRAM_PATH)
    all_messages = teams + telegram

    print(f"\nTotal raw_messages loaded: {len(all_messages)}")
    print("=" * 60)

    # --- Analysis ---
    report = analyse(all_messages)

    print("\n--- Platform counts ---")
    for plat, cnt in report["platform_counts"].items():
        print(f"  {plat}: {cnt}")

    td = report["token_distribution"]
    print(f"\n--- Token distribution ---")
    print(f"  min={td['min']}  max={td['max']}  mean={td['mean']}  median={td['median']}  p95={td['p95']}")

    ns = report["noise"]
    print(f"\n--- Noise ---")
    print(f"  {ns['count']} messages are noise  ({ns['ratio_pct']}%)")

    dr = report["date_references"]
    print(f"\n--- Date references ---")
    print(f"  {dr['count']} messages contain date references  ({dr['ratio_pct']}%)")

    print(f"\n--- Per-channel message counts (top 20) ---")
    for i, (ch, cnt) in enumerate(report["channel_distribution"].items()):
        if i >= 20:
            print(f"  … and {len(report['channel_distribution']) - 20} more channels")
            break
        print(f"  {cnt:>5}  {ch}")

    # --- Stratified sample ---
    print(f"\n{'=' * 60}")
    print(f"Sampling {SAMPLE_SIZE} messages (seed={SEED}) …")
    sampled = stratified_sample(all_messages, SAMPLE_SIZE, MIN_PER_CHANNEL, SEED)
    print(f"  Sampled: {len(sampled)} messages")

    # Quick sample stats
    sample_platforms = Counter(m.get("platform") for m in sampled)
    sample_noise = sum(1 for m in sampled if classify_noise(m))
    print(f"  Per platform: {dict(sample_platforms)}")
    print(f"  Noise in sample: {sample_noise} ({round(100 * sample_noise / len(sampled), 1)}%)")

    eval_items = [to_eval_item(m) for m in sampled]

    # --- Write outputs ---
    eval_path = SCRIPT_DIR / "eval_dataset_unlabeled.json"
    report_path = SCRIPT_DIR / "data_analysis_report.json"

    with open(eval_path, "w", encoding="utf-8") as fh:
        json.dump(eval_items, fh, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(eval_items)} items → {eval_path}")

    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(f"Wrote analysis report  → {report_path}")

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"  Total messages analysed: {report['total_messages']}")
    print(f"  Noise ratio:            {ns['ratio_pct']}%")
    print(f"  Date-ref ratio:         {dr['ratio_pct']}%")
    print(f"  Median tokens:          {td['median']}")
    print(f"  P95 tokens:             {td['p95']}")
    print(f"  Channels:               {len(report['channel_distribution'])}")
    print(f"  Eval dataset size:      {len(eval_items)}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
