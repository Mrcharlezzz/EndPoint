from __future__ import annotations

import argparse
import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from evaluation.eval.call_debug_logger import CallDebugLogger
from evaluation.eval.loader import DATASET_CHOICES, apply_row_selection, load_eval_rows
from evaluation.eval.logger import write_artifacts
from evaluation.eval.runner import EvalRunner
from evaluation.eval.scorer import TokenCostConfig, score_results
from processing.adapters.llm.classifier import LLMClassifier
from processing.config import ProcessingConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate ClassificationOrchestrator on local gold CSV datasets."
    )
    parser.add_argument(
        "--dataset",
        choices=DATASET_CHOICES,
        default="all",
        help="Dataset to run.",
    )
    parser.add_argument("--limit", type=int, help="Evaluate only the first N rows.")
    parser.add_argument(
        "--offset", type=int, default=0, help="Start from row offset N."
    )
    parser.add_argument("--ids", nargs="*", help="Evaluate only specific row IDs.")
    parser.add_argument(
        "--shuffle", action="store_true", help="Shuffle rows before limiting."
    )
    parser.add_argument("--seed", type=int, default=0, help="Stable shuffle seed.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Maximum in-flight async orchestrator calls.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/eval_runs"),
        help="Base artifact output directory.",
    )
    parser.add_argument("--model", help="Optional model override.")
    parser.add_argument("--prompt-version", help="Optional prompt version override.")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=90.0,
        help="Per-row timeout in seconds.",
    )
    parser.add_argument(
        "--retry-count",
        type=int,
        default=0,
        help="Full-row retry count for system/runtime failures.",
    )
    parser.add_argument(
        "--max-errors",
        type=int,
        help="Set an unhealthy-run error limit; remaining rows may be skipped.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Return success exit code even when row-level system errors occur.",
    )
    parser.add_argument(
        "--fail-below-macro-f1",
        type=float,
        help="Return nonzero if classification macro F1 is below this threshold.",
    )
    parser.add_argument(
        "--fail-below-completeness",
        type=float,
        help="Return nonzero if mean extraction completeness is below this threshold.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).parent / "telegram_llm",
        help="Directory containing evaluation CSVs.",
    )
    parser.add_argument(
        "--input-price-usd-per-1m-tokens",
        type=float,
        help="Optional estimated USD price for 1M prompt tokens.",
    )
    parser.add_argument(
        "--output-price-usd-per-1m-tokens",
        type=float,
        help="Optional estimated USD price for 1M completion tokens.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    rows = load_eval_rows(args.data_dir, dataset=args.dataset)
    selected_rows = apply_row_selection(
        rows,
        ids=args.ids,
        offset=args.offset,
        limit=args.limit,
        shuffle=args.shuffle,
        seed=args.seed,
    )
    if not selected_rows:
        raise SystemExit("No evaluation rows selected")

    config = ProcessingConfig()
    update: dict[str, object] = {
        "llm_timeout_seconds": args.timeout_seconds,
    }
    if args.model:
        update["llm_model"] = args.model
    if args.prompt_version:
        update["llm_prompt_version"] = args.prompt_version
    config = config.model_copy(update=update)

    eval_run_id = _eval_run_id()
    artifact_dir = args.output_dir / eval_run_id
    call_debug_log_path = artifact_dir / "call_debug.jsonl"
    call_debug_logger = CallDebugLogger(call_debug_log_path)

    classifier = LLMClassifier(config)
    runner = EvalRunner(
        classifier,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout_seconds,
        retry_count=args.retry_count,
        max_errors=args.max_errors,
        continue_on_error=args.continue_on_error,
        eval_run_id=eval_run_id,
        call_debug_logger=call_debug_logger,
    )
    started_at = perf_counter()
    pipeline_results = await runner.run_rows(selected_rows)
    wall_clock_seconds = perf_counter() - started_at
    scored_rows, metrics = score_results(
        pipeline_results, cost_config=_token_cost_config(args)
    )
    metrics["run"].update(
        {
            "eval_run_id": eval_run_id,
            "eval_timestamp": datetime.now(timezone.utc).isoformat(),
            "dataset": args.dataset,
            "input_data_dir": str(args.data_dir),
            "concurrency": args.concurrency,
            "timeout_seconds": args.timeout_seconds,
            "retry_count": args.retry_count,
            "max_errors": args.max_errors,
            "model_name": config.llm_model,
            **_prompt_metadata(classifier),
            "code_version": _git_sha(),
            "limit": args.limit,
            "offset": args.offset,
            "shuffle": args.shuffle,
            "seed": args.seed,
            "call_debug_log_path": str(call_debug_log_path),
        }
    )
    _update_run_timing_metrics(metrics, wall_clock_seconds=wall_clock_seconds)
    artifact_dir = write_artifacts(
        scored_rows,
        metrics,
        output_dir=args.output_dir,
        eval_run_id=eval_run_id,
    )
    print(f"Evaluation artifacts written to {artifact_dir}")

    return _exit_code(metrics, args)


def _token_cost_config(args: argparse.Namespace) -> TokenCostConfig | None:
    if (
        args.input_price_usd_per_1m_tokens is None
        or args.output_price_usd_per_1m_tokens is None
    ):
        return None
    return TokenCostConfig(
        input_price_usd_per_1m_tokens=args.input_price_usd_per_1m_tokens,
        output_price_usd_per_1m_tokens=args.output_price_usd_per_1m_tokens,
    )


def _update_run_timing_metrics(metrics: dict, *, wall_clock_seconds: float) -> None:
    total_rows = metrics.get("run", {}).get("total_rows", 0)
    metrics.setdefault("run", {})
    metrics["run"]["wall_clock_seconds"] = round(wall_clock_seconds, 6)
    metrics["run"]["throughput_rows_per_second"] = (
        total_rows / wall_clock_seconds if wall_clock_seconds > 0 else 0.0
    )


def _prompt_metadata(classifier: object) -> dict[str, str | None]:
    return {
        "prompt_version": getattr(classifier, "prompt_version", None),
        "prompt_fingerprint": getattr(classifier, "prompt_fingerprint", None),
    }


def _exit_code(metrics: dict, args: argparse.Namespace) -> int:
    if metrics["run"].get("system_error_rows", 0) and not args.continue_on_error:
        return 1
    if args.fail_below_macro_f1 is not None:
        if metrics["classification"].get("macro_f1", 0.0) < args.fail_below_macro_f1:
            return 2
    if args.fail_below_completeness is not None:
        means = [
            section["end_to_end"].get("item_completeness_mean", 0.0)
            for section in metrics.get("extraction", {}).values()
        ]
        overall = sum(means) / len(means) if means else 0.0
        if overall < args.fail_below_completeness:
            return 2
    return 0


def _eval_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sha = _git_sha(short=True) or "unknown"
    return f"{timestamp}-{sha}"


def _git_sha(*, short: bool = False) -> str | None:
    args = ["git", "rev-parse"]
    args.extend(["--short", "HEAD"] if short else ["HEAD"])
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
