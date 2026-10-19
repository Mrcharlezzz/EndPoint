"""
Generate a context-aware review of all 1000 gold-labeled messages.

For each labeled message, finds the 15 preceding messages in the same
channel/course to provide conversational context for human review.

Outputs:
  - context_review.json  (structured, machine-readable)
  - context_review.txt   (human-readable, grouped by gold_label)
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
TEAMS_DUMP = Path.home() / ".config/ingest/output/2026-03-13T16-35.json"
TELEGRAM_DUMP = Path.home() / ".config/ingest/output/telegram-personal-2026-03-16T03-13.json"
GOLD_LABELS = Path(__file__).resolve().parent / "gold_labels.json"
OUT_JSON = Path(__file__).resolve().parent / "context_review.json"
OUT_TXT = Path(__file__).resolve().parent / "context_review.txt"

CONTEXT_WINDOW = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO-8601-ish timestamp, returning None on failure."""
    if ts is None:
        return None
    try:
        # Strip trailing Z and any timezone suffix for naive comparison
        cleaned = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def sort_key(msg: dict[str, Any]) -> tuple[int, datetime]:
    """Sort key that puts messages with no timestamp at the end."""
    ts = parse_timestamp(msg.get("published_at"))
    if ts is None:
        return (1, datetime.min)
    return (0, ts)


def truncate(text: str | None, length: int = 200) -> str:
    if text is None:
        return ""
    if len(text) <= length:
        return text
    return text[:length] + "..."


def short_time(ts: str | None) -> str:
    """Return a compact time string like '14:32' or 'no-time'."""
    dt = parse_timestamp(ts)
    if dt is None:
        return "no-time"
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # 1. Load full corpus ---------------------------------------------------
    print("Loading Teams dump...")
    with open(TEAMS_DUMP, "r", encoding="utf-8") as f:
        teams_data = json.load(f)
    teams_items: list[dict] = teams_data["items"]
    print(f"  -> {len(teams_items)} items")

    print("Loading Telegram dump...")
    with open(TELEGRAM_DUMP, "r", encoding="utf-8") as f:
        tg_data = json.load(f)
    tg_items: list[dict] = tg_data["items"]
    print(f"  -> {len(tg_items)} items")

    all_items = teams_items + tg_items
    print(f"Total corpus: {len(all_items)} messages")

    # 2. Build lookup by external_id ----------------------------------------
    by_id: dict[str, dict] = {}
    for item in all_items:
        eid = item.get("external_id")
        if eid:
            by_id[eid] = item

    # 3. Build per-channel sorted lists -------------------------------------
    print("Building per-channel indexes...")
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for item in all_items:
        channel = item.get("course_name") or "unknown"
        by_channel[channel].append(item)

    # Sort each channel chronologically (None timestamps go last)
    for channel, msgs in by_channel.items():
        msgs.sort(key=sort_key)

    # Build position indexes: external_id -> index in sorted channel list
    channel_pos: dict[str, int] = {}
    for channel, msgs in by_channel.items():
        for idx, msg in enumerate(msgs):
            eid = msg.get("external_id")
            if eid:
                channel_pos[eid] = idx

    # 4. Load gold labels ---------------------------------------------------
    print("Loading gold labels...")
    with open(GOLD_LABELS, "r", encoding="utf-8") as f:
        gold_data = json.load(f)
    labels: list[dict] = gold_data["labels"]
    print(f"  -> {len(labels)} labeled messages")

    # 5. Build enriched records ---------------------------------------------
    print("Extracting context for each labeled message...")
    records: list[dict] = []
    context_counts: list[int] = []
    channels_seen: dict[str, int] = defaultdict(int)

    for i, label in enumerate(labels):
        eid = label["external_id"]
        course = label.get("course_name", "unknown")
        channels_seen[course] += 1

        # Find the full message in the corpus
        full_msg = by_id.get(eid)
        published_at = label.get("published_at") or (full_msg.get("published_at") if full_msg else None)

        # Find context from channel
        context_before: list[dict] = []
        if eid in channel_pos:
            channel_msgs = by_channel[course]
            pos = channel_pos[eid]
            start = max(0, pos - CONTEXT_WINDOW)
            for ctx_msg in channel_msgs[start:pos]:
                context_before.append({
                    "author": ctx_msg.get("author", "unknown"),
                    "body": truncate(ctx_msg.get("body"), 200),
                    "published_at": ctx_msg.get("published_at"),
                })

        context_counts.append(len(context_before))

        record = {
            "index": i,
            "external_id": eid,
            "input_body": label.get("input_body", ""),
            "course_name": course,
            "platform": label.get("platform", "unknown"),
            "published_at": published_at,
            "gold_label": label["gold_label"],
            "label_method": label.get("label_method", ""),
            "model_votes": label.get("model_votes", {}),
            "agreement_count": label.get("agreement_count", 0),
            "context_before": context_before,
        }
        records.append(record)

    # 6. Write JSON output --------------------------------------------------
    print(f"Writing {OUT_JSON}...")
    output = {"messages": records}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # 7. Write human-readable TXT (grouped by gold_label) -------------------
    print(f"Writing {OUT_TXT}...")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        grouped[rec["gold_label"]].append(rec)

    # Sort groups by size descending
    sorted_groups = sorted(grouped.items(), key=lambda kv: -len(kv[1]))

    lines: list[str] = []
    lines.append("CONTEXT-AWARE REVIEW OF 1000 GOLD-LABELED MESSAGES")
    lines.append("=" * 60)
    lines.append("")

    global_idx = 0
    for label_name, group in sorted_groups:
        lines.append("")
        lines.append(f"{'#' * 60}")
        lines.append(f"# CATEGORY: {label_name.upper()} ({len(group)} messages)")
        lines.append(f"{'#' * 60}")
        lines.append("")

        for rec in group:
            global_idx += 1
            votes_str = ", ".join(
                f"{model}={vote}"
                for model, vote in rec["model_votes"].items()
            )
            agree = rec["agreement_count"]
            total_models = len(rec["model_votes"])

            lines.append(f"========== MSG {global_idx}/1000 ==========")
            lines.append(f"Channel: {rec['course_name']}")
            lines.append(f"Platform: {rec['platform']}")
            lines.append(f"Time: {rec['published_at'] or 'N/A'}")
            lines.append(
                f"Gold: {rec['gold_label']} "
                f"(method={rec['label_method']}, agree={agree}/{total_models})"
            )
            lines.append(f"Models: {votes_str}")
            lines.append("")

            ctx = rec["context_before"]
            if ctx:
                lines.append(f"--- Context ({len(ctx)} preceding messages) ---")
                for c in ctx:
                    t = short_time(c["published_at"])
                    author = c["author"] or "unknown"
                    body = c["body"].replace("\n", " ").strip()
                    lines.append(f'  [{t}] {author}: "{body}"')
                lines.append("")
            else:
                lines.append("--- No preceding context available ---")
                lines.append("")

            body_preview = (rec["input_body"] or "").replace("\n", " ").strip()
            lines.append(f'>>> TARGET: "{body_preview}"')
            lines.append("=" * 40)
            lines.append("")

    # 8. Append summary stats -----------------------------------------------
    has_context = sum(1 for c in context_counts if c > 0)
    no_context = sum(1 for c in context_counts if c == 0)
    avg_depth = sum(context_counts) / len(context_counts) if context_counts else 0

    lines.append("")
    lines.append("=" * 60)
    lines.append("SUMMARY STATISTICS")
    lines.append("=" * 60)
    lines.append(f"Total messages reviewed: {len(records)}")
    lines.append(f"Messages with context:   {has_context}")
    lines.append(f"Messages without context: {no_context}")
    lines.append(f"Average context depth:   {avg_depth:.1f} messages (max {CONTEXT_WINDOW})")
    lines.append("")
    lines.append("Label distribution:")
    for label_name, group in sorted_groups:
        lines.append(f"  {label_name:<20s} {len(group):>4d}")
    lines.append("")
    lines.append(f"Channels represented: {len(channels_seen)}")
    lines.append("Channel distribution:")
    for ch, count in sorted(channels_seen.items(), key=lambda kv: -kv[1]):
        lines.append(f"  [{count:>3d}] {ch}")
    lines.append("")

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # 9. Print summary to stdout --------------------------------------------
    print()
    print("=" * 50)
    print("DONE")
    print("=" * 50)
    print(f"Messages with context:   {has_context}")
    print(f"Messages without context: {no_context}")
    print(f"Average context depth:   {avg_depth:.1f} / {CONTEXT_WINDOW}")
    print(f"Channels represented:    {len(channels_seen)}")
    print(f"Output JSON: {OUT_JSON}")
    print(f"Output TXT:  {OUT_TXT}")


if __name__ == "__main__":
    main()
