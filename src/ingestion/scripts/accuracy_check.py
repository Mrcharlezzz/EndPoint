"""Compute accuracy of benchmark models against gold labels.

Loads benchmark_results.json and gold_labels.json, matches by external_id,
and reports per-model accuracy, per-category accuracy, and confusion stats.

Usage:
    uv run python accuracy_check.py
    uv run python accuracy_check.py --benchmark outputs/benchmark_round2_15models_300msg.json
    uv run python accuracy_check.py --min-json 90   # only models with >=90% JSON valid
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GOLD_PATH = SCRIPT_DIR / "gold_labels.json"

CATEGORY_ORDER = ["assignment", "announcement", "event", "reminder", "material", "raw_message"]


def load_gold_labels(path: Path) -> dict[str, str]:
    """Return {external_id: gold_label} mapping."""
    with open(path) as f:
        data = json.load(f)
    return {entry["external_id"]: entry["gold_label"] for entry in data["labels"]}


def load_benchmark(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def compute_accuracy(
    benchmark: dict,
    gold: dict[str, str],
    min_json_rate: float = 0.0,
) -> list[dict]:
    """Compute accuracy for each model in the benchmark."""
    summaries_by_name = {s["model"]: s for s in benchmark["model_summaries"]}
    results = []

    for entry in benchmark["detailed_results"]:
        model_name = entry["model"]
        summary = summaries_by_name.get(model_name, {})
        json_rate = summary.get("json_valid_rate", 0)

        if json_rate < min_json_rate / 100:
            continue

        correct = 0
        total = 0
        per_category_correct = Counter()
        per_category_total = Counter()
        confusion = defaultdict(Counter)  # confusion[gold][predicted] = count
        mismatches = []

        for r in entry["results"]:
            ext_id = r.get("external_id", "")
            if ext_id not in gold:
                continue
            if not r.get("json_valid", False):
                continue

            gold_label = gold[ext_id]
            predicted = r.get("content_type", "unknown")

            # Normalize
            if predicted not in set(CATEGORY_ORDER):
                predicted = "unknown"

            total += 1
            per_category_total[gold_label] += 1
            confusion[gold_label][predicted] += 1

            if predicted == gold_label:
                correct += 1
                per_category_correct[gold_label] += 1
            else:
                mismatches.append({
                    "external_id": ext_id,
                    "gold": gold_label,
                    "predicted": predicted,
                    "text": r.get("input_body", "")[:100],
                })

        accuracy = correct / total if total else 0

        per_cat = {}
        for cat in CATEGORY_ORDER:
            t = per_category_total[cat]
            c = per_category_correct[cat]
            per_cat[cat] = {"correct": c, "total": t, "accuracy": round(c / t, 4) if t else 0}

        results.append({
            "model": model_name,
            "json_valid_rate": json_rate,
            "accuracy": round(accuracy, 4),
            "correct": correct,
            "total": total,
            "per_category": per_cat,
            "confusion": {g: dict(preds) for g, preds in confusion.items()},
            "top_mismatches": sorted(mismatches, key=lambda x: x["gold"])[:20],
            "latency_avg": summary.get("latency_avg", 0),
            "price_in": summary.get("price_in_per_m", 0),
            "price_out": summary.get("price_out_per_m", 0),
        })

    results.sort(key=lambda x: -x["accuracy"])
    return results


def print_report(results: list[dict]) -> None:
    """Print a human-readable accuracy report."""
    print(f"\n{'=' * 90}")
    print(f"{'Model':<25} {'Acc':>6} {'Correct':>8} {'Total':>6} {'JSON%':>6} {'Lat':>6} {'$/M in':>7} {'$/M out':>8}")
    print(f"{'=' * 90}")

    for r in results:
        print(
            f"{r['model']:<25} "
            f"{r['accuracy']:>5.1%} "
            f"{r['correct']:>7}/{r['total']:<5} "
            f"{r['json_valid_rate']:>5.1%} "
            f"{r['latency_avg']:>5.1f}s "
            f"{r['price_in']:>6.3f} "
            f"{r['price_out']:>7.3f}"
        )

    # Per-category breakdown for each model
    print(f"\n{'─' * 90}")
    print("Per-Category Accuracy:")
    print(f"{'─' * 90}")

    header = f"{'Model':<25}"
    for cat in CATEGORY_ORDER:
        short = cat[:6]
        header += f" {short:>10}"
    print(header)
    print("-" * (25 + 11 * len(CATEGORY_ORDER)))

    for r in results:
        line = f"{r['model']:<25}"
        for cat in CATEGORY_ORDER:
            pc = r["per_category"][cat]
            if pc["total"] == 0:
                line += f" {'n/a':>10}"
            else:
                line += f" {pc['accuracy']:>5.1%}({pc['total']:>2})"
        print(line)

    # Top confusion pairs across all models
    print(f"\n{'─' * 90}")
    print("Most Common Misclassifications (top 15):")
    print(f"{'─' * 90}")

    all_confusions = Counter()
    for r in results:
        for gold_cat, preds in r["confusion"].items():
            for pred_cat, count in preds.items():
                if gold_cat != pred_cat:
                    all_confusions[(r["model"], gold_cat, pred_cat)] += count

    print(f"{'Model':<25} {'Gold':<15} {'Predicted':<15} {'Count':>6}")
    for (model, gold_cat, pred_cat), count in all_confusions.most_common(15):
        print(f"{model:<25} {gold_cat:<15} {pred_cat:<15} {count:>6}")


def save_results(results: list[dict], out_path: Path) -> None:
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nSaved accuracy results to: {out_path}")
    print(f"File size: {out_path.stat().st_size / 1024:.1f} KB")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Accuracy check against gold labels")
    parser.add_argument(
        "--benchmark", type=str, default="benchmark_results.json",
        help="Path to benchmark_results.json",
    )
    parser.add_argument(
        "--gold", type=str, default=str(GOLD_PATH),
        help="Path to gold_labels.json",
    )
    parser.add_argument(
        "--min-json", type=float, default=0.0,
        help="Minimum JSON valid rate (%%) to include model (e.g. 90)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output JSON path (default: outputs/accuracy_<benchmark_stem>.json)",
    )
    args = parser.parse_args()

    benchmark_path = SCRIPT_DIR / args.benchmark
    gold_path = Path(args.gold)

    print(f"Benchmark: {benchmark_path.name}")
    print(f"Gold labels: {gold_path.name}")
    print(f"Min JSON rate: {args.min_json}%")

    gold = load_gold_labels(gold_path)
    print(f"Gold labels loaded: {len(gold)} messages")

    benchmark = load_benchmark(benchmark_path)
    print(f"Models in benchmark: {len(benchmark['model_summaries'])}")

    results = compute_accuracy(benchmark, gold, min_json_rate=args.min_json)

    if not results:
        print("No models met the minimum JSON rate threshold.")
        sys.exit(1)

    print_report(results)

    # Save
    out_dir = SCRIPT_DIR / "outputs"
    out_dir.mkdir(exist_ok=True)
    if args.output:
        out_path = Path(args.output)
    else:
        stem = benchmark_path.stem
        out_path = out_dir / f"accuracy_{stem}.json"

    save_results(results, out_path)


if __name__ == "__main__":
    main()
