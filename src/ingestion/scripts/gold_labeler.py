"""Gold-standard labeler for benchmark messages.

Applies a multi-strategy approach:
  1. Rule-based regex/keyword matching (highest priority)
  2. Consensus vote (4+ or 3 out of 5 models agree)
  3. Weighted majority vote (fallback)

Reads local benchmark_results.json when present, or uses two SYNTHETIC EXAMPLE results.
Outputs gold_labels.json.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_PATH = SCRIPT_DIR / "benchmark_results.json"
OUTPUT_PATH = SCRIPT_DIR / "gold_labels.json"

VALID_LABELS = frozenset(
    {"assignment", "announcement", "event", "reminder", "material", "raw_message"}
)

# Model weights based on JSON reliability from benchmark
MODEL_WEIGHTS: dict[str, float] = {
    "Gemini 2.0 Flash Lite": 1.0,   # 100%
    "Command R7B": 1.0,              # 100%
    "GPT-4o-mini": 1.0,              # 100%
    "Mistral Nemo 12B": 0.99,        # 99.9%
    "Phi-4 14B": 0.97,               # 99.7%
}

NUM_MODELS = len(MODEL_WEIGHTS)

# SYNTHETIC EXAMPLE: replace with privately reviewed labels for local runs.
MANUAL_OVERRIDES: dict[str, str] = {
    "tg_0_1": "assignment",
}

# ---------------------------------------------------------------------------
# Rule-based patterns (compiled once)
# ---------------------------------------------------------------------------

_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"homework|lab\s*report|submit\s+your|hand\s+in|upload\s+your|"
    r"problem\s+set|exercise\s+sheet|"
    r"due\s+date\b.*\b(?:task|submit|assignment|report|essay|project)|"
    r"(?:task|submit|assignment|report|essay|project)\b.*\bdue\s+date"
    r")\b"
)

_ANNOUNCEMENT_RE = re.compile(
    r"(?i)\b("
    r"class\s+will\s+be|lecture\s+will|no\s+class|cancell?ed|"
    r"room\s+change|schedule\s+change|office\s+hours\s+changed|"
    r"grades\s+are\s+posted|exam\s+results"
    r")\b"
)

_EVENT_RE = re.compile(
    r"(?i)\b("
    r"office\s+hours|exam\s+on|workshop\s+on|meeting\s+at|session\s+on"
    r")\b"
)

_EVENT_TIME_RE = re.compile(
    r"(?i)("
    r"\d{1,2}[:.]\d{2}\s*(?:am|pm)?|"
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\b|"
    r"\d{4}-\d{2}-\d{2}"
    r")"
)

_REMINDER_RE = re.compile(
    r"(?i)\b("
    r"don'?t\s+forget|deadline|reminder|due\s+tomorrow|due\s+today|"
    r"last\s+chance|submit\s+by"
    r")\b"
)

_MATERIAL_RE = re.compile(
    r"(?i)\b("
    r"slides|lecture\s+notes|reading|shared\s+a\s+file|study\s+material|"
    r"uploaded|check\s+moodle|"
    r"link\s+to\b.*\b(?:resource|material|document|pdf|video|chapter|textbook)|"
    r"(?:resource|material|document|pdf|video|chapter|textbook)\b.*\blink\s+to"
    r")\b"
)

_CASUAL_RE = re.compile(
    r"(?i)^("
    r"(?:hey|hi|hello|yo|sup|lol|haha|thanks|thank you|ok|okay|sure|"
    r"yeah|yep|nope|no problem|good luck|see you|bye|"
    r"anyone|does anyone|has anyone|can someone|who|what|how|"
    r"is there|are there|do we|did we|"
    r"nice|great|awesome|cool|congrats)\b"
    r")"
)

# Question patterns — if a message is a question about a topic, it's raw_message,
# not the topic itself (e.g. "what homework?" is not an assignment)
_QUESTION_RE = re.compile(
    r"(?i)("
    r"^\s*(?:what|where|how|when|which|who|why|is there|are there|do we|did we|can i|can we|does|am i|"
    r"i think|i mean|i cannot|i checked|i am getting|guys how|anyone)\b|"
    r"\?\s*$|"                          # ends with ?
    r"^\s*(?:yeah|we have|so)\b.*\?|"   # "We have homework?!"
    r"^\s*(?:i uploaded|i would like|for homework|hi which)\b"  # student statements about homework
    r")"
)

# ---------------------------------------------------------------------------
# Rule-based classifier
# ---------------------------------------------------------------------------


def _apply_rules(text: str) -> str | None:
    """Return a label if a rule matches, else None.

    Only match rule-based patterns on authoritative messages (not questions/casual).
    Questions containing keywords like 'homework' should fall through to consensus.
    """
    if not text or not text.strip():
        return "raw_message"

    # Casual / question / social -> raw_message (check FIRST, before keyword rules)
    if _CASUAL_RE.match(text) and len(text) < 200:
        return "raw_message"

    # If the message is a question, skip keyword rules — let consensus decide
    is_question = bool(_QUESTION_RE.search(text))
    if is_question:
        return None

    # Order matters: more specific first
    if _ASSIGNMENT_RE.search(text):
        return "assignment"

    if _REMINDER_RE.search(text):
        return "reminder"

    if _ANNOUNCEMENT_RE.search(text):
        return "announcement"

    # Event needs both keyword AND a time/date indicator
    if _EVENT_RE.search(text) and _EVENT_TIME_RE.search(text):
        return "event"

    if _MATERIAL_RE.search(text):
        return "material"

    return None


# ---------------------------------------------------------------------------
# Consensus / weighted vote
# ---------------------------------------------------------------------------


def _consensus_vote(
    votes: dict[str, str],
) -> tuple[str, str, float, int]:
    """Try consensus (4+ then 3), else weighted vote.

    Returns (label, method, confidence, agreement_count).
    """
    counts = Counter(votes.values())
    total = len(votes)

    # Consensus 4+
    for label, count in counts.most_common():
        if label not in VALID_LABELS:
            continue
        if count >= 4:
            return label, "consensus_4+", count / total, count

    # Consensus 3
    for label, count in counts.most_common():
        if label not in VALID_LABELS:
            continue
        if count >= 3:
            return label, "consensus_3", count / total, count

    # Weighted vote
    weighted: dict[str, float] = {}
    for model, label in votes.items():
        if label in VALID_LABELS:
            weighted[label] = weighted.get(label, 0.0) + MODEL_WEIGHTS.get(model, 0.5)

    if not weighted:
        return "raw_message", "weighted_vote", 0.0, 0

    best_label = max(weighted, key=lambda k: weighted[k])
    total_weight = sum(MODEL_WEIGHTS.get(m, 0.5) for m in votes)
    confidence = weighted[best_label] / total_weight if total_weight else 0.0
    agreement = sum(1 for v in votes.values() if v == best_label)
    return best_label, "weighted_vote", round(confidence, 4), agreement


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_data(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_model_votes(
    detailed_results: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Map external_id -> {model_name: content_type} for every message."""
    votes_by_id: dict[str, dict[str, str]] = {}
    for entry in detailed_results:
        model_name = entry["model"]
        for result in entry["results"]:
            eid = result["external_id"]
            ct = result.get("content_type", "raw_message")
            if ct not in VALID_LABELS:
                ct = "raw_message"  # treat parse_error / unknown as raw
            votes_by_id.setdefault(eid, {})[model_name] = ct
    return votes_by_id


def label_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    detailed = data["detailed_results"]
    votes_by_id = build_model_votes(detailed)

    # Use first model's results as the canonical message list
    base_results = detailed[0]["results"]

    labels: list[dict[str, Any]] = []

    for msg in base_results:
        eid = msg["external_id"]
        body = msg.get("input_body", "") or ""
        platform = msg.get("platform", "")
        course = msg.get("course_name", "")
        model_votes = votes_by_id.get(eid, {})

        # Strategy 0: Manual override (human-reviewed)
        if eid in MANUAL_OVERRIDES:
            override_label = MANUAL_OVERRIDES[eid]
            agreement = sum(1 for v in model_votes.values() if v == override_label)
            labels.append({
                "external_id": eid,
                "input_body": body,
                "platform": platform,
                "course_name": course,
                "gold_label": override_label,
                "label_method": "manual_review",
                "confidence": 1.0,
                "model_votes": dict(model_votes),
                "agreement_count": agreement,
            })
            continue

        # Strategy 1: rule-based
        rule_label = _apply_rules(body)
        if rule_label is not None:
            agreement = sum(1 for v in model_votes.values() if v == rule_label)
            labels.append({
                "external_id": eid,
                "input_body": body,
                "platform": platform,
                "course_name": course,
                "gold_label": rule_label,
                "label_method": "rule_based",
                "confidence": 0.95,
                "model_votes": dict(model_votes),
                "agreement_count": agreement,
            })
            continue

        # Strategy 2 & 3: consensus / weighted
        gold, method, confidence, agreement = _consensus_vote(model_votes)
        labels.append({
            "external_id": eid,
            "input_body": body,
            "platform": platform,
            "course_name": course,
            "gold_label": gold,
            "label_method": method,
            "confidence": round(confidence, 4),
            "model_votes": dict(model_votes),
            "agreement_count": agreement,
        })

    return labels


def build_output(labels: list[dict[str, Any]]) -> dict[str, Any]:
    method_counts = Counter(l["label_method"] for l in labels)
    label_counts = Counter(l["gold_label"] for l in labels)
    agreement_counts = Counter(l["agreement_count"] for l in labels)

    return {
        "metadata": {
            "total": len(labels),
            "method_breakdown": dict(method_counts),
            "label_distribution": dict(label_counts),
            "agreement_distribution": {
                str(k): v for k, v in sorted(agreement_counts.items())
            },
        },
        "labels": labels,
    }


def print_summary(output: dict[str, Any]) -> None:
    meta = output["metadata"]
    print("=" * 60)
    print(f"  Gold-Standard Labeling Summary  ({meta['total']} messages)")
    print("=" * 60)

    print("\n-- Method Breakdown --")
    for method, count in sorted(meta["method_breakdown"].items(), key=lambda x: -x[1]):
        pct = count / meta["total"] * 100
        print(f"  {method:<20s}  {count:>5d}  ({pct:5.1f}%)")

    print("\n-- Label Distribution --")
    for label, count in sorted(meta["label_distribution"].items(), key=lambda x: -x[1]):
        pct = count / meta["total"] * 100
        print(f"  {label:<20s}  {count:>5d}  ({pct:5.1f}%)")

    print("\n-- Agreement Statistics --")
    for agree, count in sorted(meta["agreement_distribution"].items(), key=lambda x: int(x[0])):
        pct = int(count) / meta["total"] * 100
        print(f"  {agree} models agree:    {count:>5}  ({pct:5.1f}%)")

    print("=" * 60)


def _synthetic_example_data() -> dict[str, Any]:
    """Provide safe sample results when no local benchmark file is available."""
    return {
        "detailed_results": [
            {
                "model": "SYNTHETIC EXAMPLE MODEL",
                "results": [
                    {
                        "external_id": "tg_0_1",
                        "input_body": "Submit the example worksheet on Friday.",
                        "platform": "telegram",
                        "course_name": "SYNTHETIC EXAMPLE COURSE",
                        "content_type": "assignment",
                    },
                    {
                        "external_id": "teams_csa_synthetic_2",
                        "input_body": "The example study session starts tomorrow.",
                        "platform": "teams",
                        "course_name": "SYNTHETIC EXAMPLE COURSE",
                        "content_type": "event",
                    },
                ],
            }
        ]
    }


def main() -> None:
    if INPUT_PATH.exists():
        data = load_data(INPUT_PATH)
    else:
        print("No local benchmark input found; using SYNTHETIC EXAMPLE results.")
        data = _synthetic_example_data()
    labels = label_messages(data)
    output = build_output(labels)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(labels)} gold labels to {OUTPUT_PATH}")
    print_summary(output)


if __name__ == "__main__":
    main()
