"""Evaluate course resolution accuracy across Levenshtein thresholds.

Reads labeled pairs from data/course-resolution-pairs.csv at the repo root.
Uses the production CourseResolver directly — if the resolver logic changes,
this eval reflects it automatically.

Each pair has a raw course name (as it arrives from ingestion) and a canonical
course name (as it would be stored in the DB from a prior ingestion). The script
builds a one-course fake course list per pair and calls the resolver's internal
matching methods, then sweeps thresholds to compute precision, recall, and F1.

Usage:
    cd src/processor
    uv run python scripts/eval_course_resolution.py
    uv run python scripts/eval_course_resolution.py --threshold 0.70
    uv run python scripts/eval_course_resolution.py --detail
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from uuid import uuid4

from processing.application.normalization.course_resolution import CourseResolver
from processing.config import ProcessingConfig
from processing.models.course import Course

# ── Paths ──────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
PAIRS_PATH = REPO_ROOT / "data" / "course-resolution-pairs.csv"

# ── Resolver setup ─────────────────────────────────────────────────────────────
# CourseResolver needs a repository and config at construction, but the methods
# we call (_find_exact_code_match, _find_title_match, extract_course_code,
# _normalize_course_name) are pure — no DB access. We pass None as the
# repository; it is never used in this eval path.


def _make_resolver(threshold: float) -> CourseResolver:
    config = ProcessingConfig(course_title_match_threshold=threshold)
    return CourseResolver(repository=None, config=config)  # type: ignore[arg-type]


def _course_from_row(row: dict) -> Course:
    """Build a Course domain object from a CSV pair row (the canonical side)."""
    return Course(
        id=uuid4(),
        name=row["canonical"],
        code=row["canonical_code"] or None,
    )


# ── Per-pair resolution ────────────────────────────────────────────────────────


def resolve_pair(
    resolver: CourseResolver,
    raw: str,
    canonical_course: Course,
) -> tuple[bool, str, float]:
    """Return (predicted_match, method, score).

    Mirrors the resolver's decision path using its own methods:

    1. Raw has a code AND canonical has a code:
       only an exact code match counts — no title fallback between coded courses.

    2. Raw has a code AND canonical has no code:
       title fallback against code-less courses (include_coded=False).
       Since canonical has no code it is included in the search.

    3. Raw has no code:
       title match against ALL courses (include_coded=True).
    """
    import Levenshtein as _lev

    raw_code = resolver.extract_course_code(raw)
    canonical_code = canonical_course.code

    if raw_code and canonical_code:
        match = resolver._find_exact_code_match([canonical_course], raw_code)
        matched = match is not None
        return matched, "code_exact", 1.0 if matched else 0.0

    # Title path — include_coded follows the real resolver:
    # raw has code → only code-less candidates; raw has no code → all candidates.
    include_coded = raw_code is None
    normalized_raw = resolver._normalize_course_name(raw, raw_code)
    match = resolver._find_title_match(
        [canonical_course],
        normalized_raw,
        include_coded=include_coded,
        min_score=resolver._strict_title_match_threshold(),
    )

    normalized_canonical = resolver._normalize_course_name(
        canonical_course.name, canonical_code
    )
    score = _lev.ratio(normalized_raw.casefold(), normalized_canonical.casefold())

    return match is not None, "title_levenshtein", score


# ── Dataset ────────────────────────────────────────────────────────────────────


def load_pairs(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Metrics ────────────────────────────────────────────────────────────────────


def compute_metrics(pairs: list[dict], threshold: float) -> dict:
    resolver = _make_resolver(threshold)
    tp = fp = fn = tn = 0
    results = []

    for row in pairs:
        expected = row["should_match"].strip().lower() == "yes"
        canonical_course = _course_from_row(row)
        predicted, method, score = resolve_pair(resolver, row["raw"], canonical_course)
        correct = predicted == expected

        if predicted and expected:
            tp += 1
            outcome = "TP"
        elif predicted and not expected:
            fp += 1
            outcome = "FP"
        elif not predicted and expected:
            fn += 1
            outcome = "FN"
        else:
            tn += 1
            outcome = "TN"

        results.append(
            {
                "id": row["id"],
                "raw": row["raw"],
                "canonical": row["canonical"],
                "expected": expected,
                "predicted": predicted,
                "method": method,
                "score": score,
                "outcome": outcome,
                "correct": correct,
            }
        )

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "threshold": threshold,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "results": results,
    }


# ── Output ─────────────────────────────────────────────────────────────────────

THRESHOLDS = [0.70, 0.75, 0.80, 0.85, 0.90, 0.95]


def print_threshold_table(all_metrics: list[dict]) -> None:
    print(
        f"\n{'Threshold':>10}  {'Precision':>10}  {'Recall':>8}  {'F1':>8}  {'TP':>4}  {'FP':>4}  {'FN':>4}  {'TN':>4}"
    )
    print("-" * 68)
    for m in all_metrics:
        print(
            f"{m['threshold']:>10.2f}  "
            f"{m['precision']:>10.3f}  "
            f"{m['recall']:>8.3f}  "
            f"{m['f1']:>8.3f}  "
            f"{m['tp']:>4}  {m['fp']:>4}  {m['fn']:>4}  {m['tn']:>4}"
        )
    print()


def print_detail(metrics: dict) -> None:
    threshold = metrics["threshold"]
    print(f"\nPer-pair detail at threshold {threshold:.2f}\n")
    print(
        f"{'ID':<10}  {'Out':>4}  {'Score':>6}  {'Method':<20}  {'Raw':<55}  Canonical"
    )
    print("-" * 140)
    for r in metrics["results"]:
        marker = "" if r["correct"] else " <--"
        score_str = (
            f"{r['score']:.3f}" if r["method"] == "title_levenshtein" else "exact"
        )
        raw_short = r["raw"][:53] + ".." if len(r["raw"]) > 55 else r["raw"]
        can_short = (
            r["canonical"][:53] + ".." if len(r["canonical"]) > 55 else r["canonical"]
        )
        print(
            f"{r['id']:<10}  {r['outcome']:>4}  {score_str:>6}  {r['method']:<20}  "
            f"{raw_short:<55}  {can_short}{marker}"
        )
    print()
    errors = [r for r in metrics["results"] if not r["correct"]]
    if errors:
        print(f"{len(errors)} error(s) at threshold {threshold:.2f}:")
        for r in errors:
            label = "false positive" if r["outcome"] == "FP" else "false negative"
            print(f"  [{r['id']}] {label} (score={r['score']:.3f})")
            print(f"    raw:       {r['raw']}")
            print(f"    canonical: {r['canonical']}")
    else:
        print(f"No errors at threshold {threshold:.2f}.")
    print()


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate course resolution pairs.")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Show per-pair detail at this threshold. Omit for full sweep.",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="Print per-pair detail for every threshold in the sweep.",
    )
    args = parser.parse_args()

    if not PAIRS_PATH.exists():
        print(f"Pairs file not found: {PAIRS_PATH}", file=sys.stderr)
        sys.exit(1)

    pairs = load_pairs(PAIRS_PATH)
    print(f"Loaded {len(pairs)} pairs from {PAIRS_PATH.relative_to(REPO_ROOT)}")

    if args.threshold is not None:
        metrics = compute_metrics(pairs, args.threshold)
        print_threshold_table([metrics])
        print_detail(metrics)
    else:
        all_metrics = [compute_metrics(pairs, t) for t in THRESHOLDS]
        print_threshold_table(all_metrics)
        if args.detail:
            for m in all_metrics:
                print_detail(m)


if __name__ == "__main__":
    main()
