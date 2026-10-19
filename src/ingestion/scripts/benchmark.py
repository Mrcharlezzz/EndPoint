"""LLM Cost Research Benchmark — Message Classification Pipeline.

Runs 50 sample messages through 40 models across 2 providers IN PARALLEL,
measures accuracy, JSON reliability, latency, and cost.
Dumps all results to benchmark_results.json.

Usage:
    cd ingestion/scripts
    uv run python benchmark.py
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

# ─── Task 1/20: Load env and initialize API clients ─────────────────────

def load_env() -> None:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def init_async_clients() -> dict:
    from openai import AsyncOpenAI

    clients = {}
    or_key = os.environ.get("OPENROUTER_API_KEY", "")

    if or_key:
        clients["openrouter"] = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=or_key)
        print("  [ok] OpenRouter async client initialized")
    else:
        print("  [!!] OPENROUTER_API_KEY missing")

    return clients


# ─── Task 2/20: Load data dumps ─────────────────────────────────────────

OUTPUT_DIR = Path.home() / ".config" / "ingest" / "output"
TEAMS_FILE = OUTPUT_DIR / "2026-03-13T16-35.json"
TELEGRAM_FILE = OUTPUT_DIR / "telegram-personal-2026-03-16T03-13.json"


def load_dumps() -> tuple[list[dict], list[dict]]:
    print(f"  Loading {TEAMS_FILE.name}...")
    with open(TEAMS_FILE) as f:
        teams = json.load(f)
    print(f"    {len(teams['items'])} items")

    print(f"  Loading {TELEGRAM_FILE.name}...")
    with open(TELEGRAM_FILE) as f:
        tg = json.load(f)
    print(f"    {len(tg['items'])} items")

    return teams["items"], tg["items"]


# ─── Task 3/20: Merge and filter to raw_message ─────────────────────────

def merge_raw_messages(teams_items: list[dict], tg_items: list[dict]) -> list[dict]:
    raw = [i for i in teams_items if i["content_type"] == "raw_message"] + tg_items
    teams_count = sum(1 for i in raw if i["platform"] == "teams")
    tg_count = sum(1 for i in raw if i["platform"] == "telegram")
    print(f"  Total raw_messages: {len(raw)} (teams={teams_count}, telegram={tg_count})")
    return raw


# ─── Task 4/20: Junk detection heuristics ───────────────────────────────

JUNK_PATTERNS = [
    "Card - access it on",
    "Scheduled",
    "Recurring",
    "@thread.tacv2",
    "orgid:",
    "AAMkA",
    "scopeId",
    "storageId",
]

EMOJI_RE = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    r"\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001FA00-\U0001FA6F"
    r"\U0001FA70-\U0001FAFF\U00002600-\U000026FF\U0000FE00-\U0000FE0F"
    r"\U0000200D\U00002B50\U00002B55\U000023E9-\U000023F3\U0000231A-\U0000231B]+"
)


def classify_junk(msg: dict) -> str | None:
    """Returns junk reason or None if real message."""
    body = msg.get("body", "").strip()
    if not body:
        return "empty"
    if len(body) <= 5:
        return "tiny"
    if not EMOJI_RE.sub("", body).strip():
        return "emoji_only"
    for pattern in JUNK_PATTERNS:
        if pattern in body:
            return "system_junk"
    if msg.get("author") == "Assignments":
        return "assignments_bot"
    if body.endswith("Play") and len(body.split()) <= 3:
        return "recording_placeholder"
    return None


# ─── Task 5/20: Run pre-filter and compute noise ratio ──────────────────

def run_prefilter(raw: list[dict]) -> tuple[list[dict], list[dict], dict]:
    junk_reasons = Counter()
    real, junk = [], []

    for msg in raw:
        reason = classify_junk(msg)
        if reason:
            junk_reasons[reason] += 1
            junk.append(msg)
        else:
            real.append(msg)

    total = len(raw)
    stats = {
        "total": total,
        "real": len(real),
        "junk": len(junk),
        "real_pct": round(100 * len(real) / total, 1),
        "junk_pct": round(100 * len(junk) / total, 1),
        "junk_breakdown": dict(junk_reasons.most_common()),
    }

    print(f"  Real messages:  {len(real)} ({stats['real_pct']}%)")
    print(f"  Junk filtered:  {len(junk)} ({stats['junk_pct']}%)")
    for reason, count in junk_reasons.most_common():
        print(f"    {reason:<30} {count:>5}")

    return real, junk, stats


# ─── Task 6/20: Token distribution ──────────────────────────────────────

def estimate_tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.3))


def token_distribution(messages: list[dict]) -> dict:
    tokens = sorted(estimate_tokens(m.get("body", "")) for m in messages)
    n = len(tokens)
    if n == 0:
        print("  No messages to analyze.")
        return {"count": 0}
    stats = {
        "count": n,
        "min": tokens[0],
        "p25": tokens[n // 4],
        "median": tokens[n // 2],
        "mean": round(sum(tokens) / n, 1),
        "p75": tokens[3 * n // 4],
        "p95": tokens[int(n * 0.95)],
        "max": tokens[-1],
        "total": sum(tokens),
    }
    print(f"  Count:  {n}")
    print(f"  Min={stats['min']}  p25={stats['p25']}  median={stats['median']}  mean={stats['mean']}  p75={stats['p75']}  p95={stats['p95']}  max={stats['max']}")
    print(f"  Total tokens: {stats['total']:,}")
    return stats


# ─── Task 7/20: Daily volume statistics ─────────────────────────────────

def daily_volume_stats(messages: list[dict]) -> dict:
    daily = Counter()
    for m in messages:
        dt = m.get("published_at")
        if dt:
            daily[dt[:10]] += 1

    vals = sorted(daily.values())
    n = len(vals)
    if n == 0:
        return {"days": 0}

    stats = {
        "days_with_messages": n,
        "date_range": [min(daily.keys()), max(daily.keys())],
        "per_day_min": vals[0],
        "per_day_median": vals[n // 2],
        "per_day_mean": round(sum(vals) / n, 1),
        "per_day_max": vals[-1],
    }
    print(f"  Date range: {stats['date_range'][0]} to {stats['date_range'][1]}")
    print(f"  Days with messages: {n}")
    print(f"  Per day: min={stats['per_day_min']}  median={stats['per_day_median']}  mean={stats['per_day_mean']}  max={stats['per_day_max']}")
    return stats


# ─── Task 8/20: System prompt ────────────────────────────────────────────

SYSTEM_PROMPT = """You classify university chat messages into structured types and extract key fields.

For each message, return a JSON object with:
- content_type: one of "assignment", "announcement", "event", "reminder", "material", "raw_message", "unknown"
- title: short extracted title or null
- due_at: ISO 8601 datetime if a deadline is mentioned, or null
- confidence: 0.0 to 1.0
- action_items: list of extracted action items, or empty list

Rules:
- "assignment": homework, lab, quiz, project with a task to complete
- "announcement": professor/admin info broadcast (schedule change, room change, policy)
- "event": meeting, office hours, exam, workshop with a specific time
- "reminder": deadline reminder, follow-up on existing task
- "material": shared resource, lecture slides, link to study material
- "raw_message": casual chat, question, social, or anything that doesn't fit above
- If unsure, use "raw_message" with lower confidence

Examples:

Message: "Homework 3 deadline extended to Friday 11:59pm"
{"content_type": "reminder", "title": "Homework 3 Deadline Extension", "due_at": null, "confidence": 0.92, "action_items": ["Submit Homework 3 by Friday 11:59pm"]}

Message: "Don't forget that in the beginning of the seminar you'll have the quiz."
{"content_type": "reminder", "title": "Seminar Quiz Reminder", "due_at": null, "confidence": 0.88, "action_items": ["Prepare for quiz at start of seminar"]}

Message: "Next week lectures will be completely online via Teams meeting"
{"content_type": "announcement", "title": "Online Lectures Next Week", "due_at": null, "confidence": 0.95, "action_items": ["Join Teams meeting for lectures next week"]}

Message: "Where do we submit the homework?"
{"content_type": "raw_message", "title": null, "due_at": null, "confidence": 0.90, "action_items": []}

Message: "lol"
{"content_type": "raw_message", "title": null, "due_at": null, "confidence": 0.99, "action_items": []}

Return ONLY the JSON object, no markdown, no explanation."""


# ─── Task 9/20: Model configurations ────────────────────────────────────

MODELS = [
    # ── OpenRouter FREE ─────────────────────────────────────────────────────
    # Google Gemma family
    {"name": "Gemma 3 4B",              "provider": "openrouter", "model_id": "google/gemma-3-4b-it:free",                              "price_in": 0.00, "price_out": 0.00},
    {"name": "Gemma 3 12B",             "provider": "openrouter", "model_id": "google/gemma-3-12b-it:free",                             "price_in": 0.00, "price_out": 0.00},
    {"name": "Gemma 3 27B",             "provider": "openrouter", "model_id": "google/gemma-3-27b-it:free",                             "price_in": 0.00, "price_out": 0.00},
    {"name": "Gemma 3n E2B",            "provider": "openrouter", "model_id": "google/gemma-3n-e2b-it:free",                            "price_in": 0.00, "price_out": 0.00},
    {"name": "Gemma 3n E4B",            "provider": "openrouter", "model_id": "google/gemma-3n-e4b-it:free",                            "price_in": 0.00, "price_out": 0.00},
    # Qwen family
    {"name": "Qwen3 4B",                "provider": "openrouter", "model_id": "qwen/qwen3-4b:free",                                     "price_in": 0.00, "price_out": 0.00},
    {"name": "Qwen3 Coder 480B",        "provider": "openrouter", "model_id": "qwen/qwen3-coder:free",                                  "price_in": 0.00, "price_out": 0.00},
    {"name": "Qwen3-Next 80B-A3B",      "provider": "openrouter", "model_id": "qwen/qwen3-next-80b-a3b-instruct:free",                  "price_in": 0.00, "price_out": 0.00},
    # Meta Llama family
    {"name": "Llama 3.2 3B",            "provider": "openrouter", "model_id": "meta-llama/llama-3.2-3b-instruct:free",                  "price_in": 0.00, "price_out": 0.00},
    {"name": "Llama 3.3 70B",           "provider": "openrouter", "model_id": "meta-llama/llama-3.3-70b-instruct:free",                 "price_in": 0.00, "price_out": 0.00},
    # NVIDIA Nemotron family
    {"name": "Nemotron 3 Nano 30B-A3B", "provider": "openrouter", "model_id": "nvidia/nemotron-3-nano-30b-a3b:free",                    "price_in": 0.00, "price_out": 0.00},
    {"name": "Nemotron 3 Super 120B",   "provider": "openrouter", "model_id": "nvidia/nemotron-3-super-120b-a12b:free",                 "price_in": 0.00, "price_out": 0.00},
    {"name": "Nemotron Nano 9B v2",     "provider": "openrouter", "model_id": "nvidia/nemotron-nano-9b-v2:free",                        "price_in": 0.00, "price_out": 0.00},
    {"name": "Nemotron Nano 12B VL",    "provider": "openrouter", "model_id": "nvidia/nemotron-nano-12b-v2-vl:free",                    "price_in": 0.00, "price_out": 0.00},
    # Mistral
    {"name": "Mistral Small 3.1 24B",   "provider": "openrouter", "model_id": "mistralai/mistral-small-3.1-24b-instruct:free",          "price_in": 0.00, "price_out": 0.00},
    # Other notable free models
    {"name": "Hermes 3 Llama 405B",     "provider": "openrouter", "model_id": "nousresearch/hermes-3-llama-3.1-405b:free",              "price_in": 0.00, "price_out": 0.00},
    {"name": "StepFun 3.5 Flash",       "provider": "openrouter", "model_id": "stepfun/step-3.5-flash:free",                            "price_in": 0.00, "price_out": 0.00},
    {"name": "GLM 4.5 Air",             "provider": "openrouter", "model_id": "z-ai/glm-4.5-air:free",                                  "price_in": 0.00, "price_out": 0.00},
    {"name": "Arcee Trinity Large",     "provider": "openrouter", "model_id": "arcee-ai/trinity-large-preview:free",                    "price_in": 0.00, "price_out": 0.00},
    {"name": "Arcee Trinity Mini",      "provider": "openrouter", "model_id": "arcee-ai/trinity-mini:free",                             "price_in": 0.00, "price_out": 0.00},
    {"name": "Liquid LFM 1.2B Instruct","provider": "openrouter", "model_id": "liquid/lfm-2.5-1.2b-instruct:free",                     "price_in": 0.00, "price_out": 0.00},
    {"name": "Liquid LFM 1.2B Think",   "provider": "openrouter", "model_id": "liquid/lfm-2.5-1.2b-thinking:free",                     "price_in": 0.00, "price_out": 0.00},
    {"name": "Dolphin Mistral 24B",     "provider": "openrouter", "model_id": "cognitivecomputations/dolphin-mistral-24b-venice-edition:free", "price_in": 0.00, "price_out": 0.00},

    # ── OpenRouter PAID — original top-5 benchmark models ──────────────────
    {"name": "Mistral Nemo 12B",        "provider": "openrouter", "model_id": "mistralai/mistral-nemo",                                "price_in": 0.02, "price_out": 0.04},
    {"name": "Command R7B",             "provider": "openrouter", "model_id": "cohere/command-r7b-12-2024",                             "price_in": 0.0375, "price_out": 0.15},
    {"name": "Phi-4 14B",               "provider": "openrouter", "model_id": "microsoft/phi-4",                                       "price_in": 0.06, "price_out": 0.14},
    {"name": "Gemini 2.0 Flash Lite",   "provider": "openrouter", "model_id": "google/gemini-2.0-flash-lite-001",                      "price_in": 0.075, "price_out": 0.30},
    {"name": "GPT-4o-mini",             "provider": "openrouter", "model_id": "openai/gpt-4o-mini",                                    "price_in": 0.15, "price_out": 0.60},

    # ── OpenRouter PAID — 15 new cheap models (round 2) ──────────────────
    {"name": "Llama 3.1 8B",            "provider": "openrouter", "model_id": "meta-llama/llama-3.1-8b-instruct",                      "price_in": 0.02, "price_out": 0.05},
    {"name": "Liquid LFM2 24B-A2B",     "provider": "openrouter", "model_id": "liquid/lfm-2-24b-a2b",                                  "price_in": 0.03, "price_out": 0.12},
    {"name": "Qwen 3.5 9B",             "provider": "openrouter", "model_id": "qwen/qwen3.5-9b",                                       "price_in": 0.05, "price_out": 0.15},
    {"name": "GPT-5 Nano",              "provider": "openrouter", "model_id": "openai/gpt-5-nano",                                     "price_in": 0.05, "price_out": 0.40},
    {"name": "GLM 4.7 Flash",           "provider": "openrouter", "model_id": "z-ai/glm-4.7-flash",                                    "price_in": 0.06, "price_out": 0.40},
    {"name": "Seed 1.6 Flash",          "provider": "openrouter", "model_id": "bytedance-seed/seed-1.6-flash",                         "price_in": 0.075, "price_out": 0.30},
    {"name": "XiaoMi MiMo V2 Flash",    "provider": "openrouter", "model_id": "xiaomi/mimo-v2-flash",                                   "price_in": 0.09, "price_out": 0.29},
    {"name": "Seed 2.0 Mini",           "provider": "openrouter", "model_id": "bytedance-seed/seed-2.0-mini",                          "price_in": 0.10, "price_out": 0.40},
    {"name": "Qwen 3.5 Flash",          "provider": "openrouter", "model_id": "qwen/qwen3.5-flash-02-23",                              "price_in": 0.10, "price_out": 0.40},
    {"name": "Ministral 3B",            "provider": "openrouter", "model_id": "mistralai/ministral-3b-2512",                           "price_in": 0.10, "price_out": 0.10},
    {"name": "Ministral 8B",            "provider": "openrouter", "model_id": "mistralai/ministral-8b-2512",                           "price_in": 0.15, "price_out": 0.15},
    {"name": "Upstage Solar Pro 3",     "provider": "openrouter", "model_id": "upstage/solar-pro-3",                                   "price_in": 0.15, "price_out": 0.60},
    {"name": "Qwen 3.5 35B-A3B",        "provider": "openrouter", "model_id": "qwen/qwen3.5-35b-a3b",                                  "price_in": 0.1625, "price_out": 1.30},
    {"name": "Qwen 3.5 27B",            "provider": "openrouter", "model_id": "qwen/qwen3.5-27b",                                      "price_in": 0.195, "price_out": 1.56},
    {"name": "Ministral 14B",           "provider": "openrouter", "model_id": "mistralai/ministral-14b-2512",                          "price_in": 0.20, "price_out": 0.20},
]

# ── Round 2 model IDs (for --round2 flag) ─────────────────────────────
ROUND2_MODEL_IDS = [
    "meta-llama/llama-3.1-8b-instruct",
    "liquid/lfm-2-24b-a2b",
    "qwen/qwen3.5-9b",
    "openai/gpt-5-nano",
    "z-ai/glm-4.7-flash",
    "bytedance-seed/seed-1.6-flash",
    "xiaomi/mimo-v2-flash",
    "bytedance-seed/seed-2.0-mini",
    "qwen/qwen3.5-flash-02-23",
    "mistralai/ministral-3b-2512",
    "mistralai/ministral-8b-2512",
    "upstage/solar-pro-3",
    "qwen/qwen3.5-35b-a3b",
    "qwen/qwen3.5-27b",
    "mistralai/ministral-14b-2512",
]


# ─── Concurrency limits per provider ────────────────────────────────────

PROVIDER_CONCURRENCY = {
    "openrouter": 10,  # Per-model concurrency (each model gets its own semaphore)
}

# Free models get tighter limits to avoid 429s
FREE_CONCURRENCY = 3

# ─── Partial results (resume support) ───────────────────────────────────

PARTIAL_PATH = Path(__file__).parent / "benchmark_partial.json"


def load_partial() -> dict:
    """Load previously completed model results, keyed by model_id."""
    if PARTIAL_PATH.exists():
        try:
            data = json.loads(PARTIAL_PATH.read_text())
            print(f"  Loaded partial results: {len(data)} models already done")
            return data
        except (json.JSONDecodeError, KeyError):
            print("  Partial file corrupt, starting fresh")
    return {}


def save_partial(partial: dict) -> None:
    """Atomically write partial results to disk."""
    tmp = PARTIAL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(partial, indent=2, default=str, ensure_ascii=False))
    tmp.replace(PARTIAL_PATH)


def clear_partial() -> None:
    """Remove partial file after successful full run."""
    if PARTIAL_PATH.exists():
        PARTIAL_PATH.unlink()
        print("  Cleared partial results file")


# ─── Task 10/20: Single message classification (async) ──────────────────

MAX_RETRIES = 3
RETRY_BACKOFF = [2, 5, 10]  # seconds


async def classify_single_async(client, model_id: str, body: str, semaphore: asyncio.Semaphore, progress=None) -> dict:
    """Call LLM with rate-limit semaphore, retry on 429, parse result."""
    combined_prompt = SYSTEM_PROMPT + f'\n\nMessage: "{body}"'

    for attempt in range(MAX_RETRIES):
        async with semaphore:
            start = time.time()
            try:
                r = await client.chat.completions.create(
                    model=model_id,
                    messages=[{"role": "user", "content": combined_prompt}],
                    max_tokens=200,
                    temperature=0.1,
                )
                latency = time.time() - start

                # Handle None content (some models return empty)
                raw_content = r.choices[0].message.content if r.choices and r.choices[0].message.content else ""
                raw_output = raw_content.strip()

                # Strip markdown fences
                cleaned = raw_output
                if cleaned.startswith("```"):
                    cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

                try:
                    parsed = json.loads(cleaned)
                    json_valid = True
                    content_type = parsed.get("content_type", "unknown")
                    has_required = all(k in parsed for k in ["content_type", "confidence"])
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                    json_valid = False
                    content_type = "parse_error"
                    has_required = False

                result = {
                    "success": True,
                    "latency": round(latency, 3),
                    "json_valid": json_valid,
                    "has_required_fields": has_required,
                    "content_type": content_type,
                    "parsed": parsed,
                    "raw_output": raw_output,
                    "input_tokens": getattr(r.usage, "prompt_tokens", 0) if r.usage else 0,
                    "output_tokens": getattr(r.usage, "completion_tokens", 0) if r.usage else 0,
                }
                if progress:
                    progress.advance(progress.task_id)
                return result

            except Exception as e:
                err_str = str(e)
                # Retry on 429 rate limit
                if "429" in err_str and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    continue

                result = {
                    "success": False,
                    "latency": round(time.time() - start, 3),
                    "error": err_str,
                    "json_valid": False,
                    "has_required_fields": False,
                    "content_type": "error",
                    "parsed": None,
                    "raw_output": "",
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
                if progress:
                    progress.advance(progress.task_id)
                return result

    # Should not reach here, but just in case
    if progress:
        progress.advance(progress.task_id)
    return {"success": False, "latency": 0, "error": "max retries", "json_valid": False,
            "has_required_fields": False, "content_type": "error", "parsed": None,
            "raw_output": "", "input_tokens": 0, "output_tokens": 0}


# ─── Task 11/20: Benchmark runner (async, parallel messages) ────────────

def _aggregate_results(name: str, model_id: str, model_config: dict, results: list[dict], n: int) -> dict:
    """Compute summary statistics from benchmark results."""
    successful = [r for r in results if r["success"]]
    json_valid_count = sum(1 for r in results if r["json_valid"])
    has_fields_count = sum(1 for r in results if r["has_required_fields"])
    error_count = sum(1 for r in results if not r["success"])

    latencies = sorted(r["latency"] for r in successful)
    n_lat = len(latencies)

    ct_dist = Counter(r["content_type"] for r in results)
    total_in = sum(r["input_tokens"] for r in results)
    total_out = sum(r["output_tokens"] for r in results)

    return {
        "model": name,
        "model_id": model_id,
        "provider": model_config["provider"],
        "price_in_per_m": model_config["price_in"],
        "price_out_per_m": model_config["price_out"],
        "total_messages": n,
        "success_count": len(successful),
        "error_count": error_count,
        "json_valid_count": json_valid_count,
        "json_valid_rate": round(json_valid_count / n, 4) if n else 0,
        "required_fields_count": has_fields_count,
        "required_fields_rate": round(has_fields_count / n, 4) if n else 0,
        "latency_avg": round(sum(latencies) / n_lat, 3) if n_lat else 0,
        "latency_p50": round(latencies[n_lat // 2], 3) if n_lat else 0,
        "latency_p95": round(latencies[int(n_lat * 0.95)], 3) if n_lat else 0,
        "latency_min": round(latencies[0], 3) if n_lat else 0,
        "latency_max": round(latencies[-1], 3) if n_lat else 0,
        "content_type_dist": dict(ct_dist.most_common()),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "avg_input_tokens": round(total_in / n, 1) if n else 0,
        "avg_output_tokens": round(total_out / n, 1) if n else 0,
    }


async def benchmark_model_async(
    client,
    model_config: dict,
    messages: list[dict],
    semaphore: asyncio.Semaphore,
    progress=None,
    partial: dict | None = None,
    partial_lock: asyncio.Lock | None = None,
) -> tuple[dict, list[dict]] | None:
    """Run all messages through a model concurrently, collect metrics."""
    model_id = model_config["model_id"]
    name = model_config["name"]
    n = len(messages)

    # Fire all messages concurrently (semaphore controls rate)
    tasks = []
    for msg in messages:
        body = msg.get("body", "")
        task = asyncio.create_task(classify_single_async(client, model_id, body, semaphore, progress=progress))
        tasks.append((msg, task))

    results = []
    for msg, task in tasks:
        r = await task
        body = msg.get("body", "")
        r["external_id"] = msg.get("external_id", "")
        r["input_body"] = body[:200]
        r["platform"] = msg.get("platform", "")
        r["course_name"] = msg.get("course_name", "")
        results.append(r)

    summary = _aggregate_results(name, model_id, model_config, results, n)

    # Save to partial results immediately
    if partial is not None and partial_lock is not None:
        async with partial_lock:
            partial[model_id] = {
                "summary": summary,
                "results": [{k: v for k, v in r.items() if k != "parsed"} for r in results],
            }
            save_partial(partial)

    return summary, results


# ─── Task 12/20: Sample benchmark messages ───────────────────────────────

def sample_messages(real: list[dict], n: int = 50, seed: int = 42) -> list[dict]:
    if not real:
        print("  No real messages to sample!")
        return []
    random.seed(seed)
    sample = random.sample(real, min(n, len(real)))
    print(f"  Sampled {len(sample)} messages (seed={seed})")
    platforms = Counter(m["platform"] for m in sample)
    print(f"  Platforms: {dict(platforms)}")
    lengths = sorted(estimate_tokens(m.get("body", "")) for m in sample)
    print(f"  Token range: {lengths[0]}-{lengths[-1]} (median={lengths[len(lengths)//2]})")
    return sample


# ─── Task 13-15/20: Run all benchmarks in parallel ──────────────────────

async def run_all_benchmarks_async(
    clients: dict,
    models: list[dict],
    messages: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Run ALL models concurrently with a rich progress bar. Resumes from partial results."""
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

    # Load partial results from previous interrupted runs
    partial = load_partial()
    partial_lock = asyncio.Lock()

    # Create per-provider semaphores
    semaphores = {
        provider: asyncio.Semaphore(limit)
        for provider, limit in PROVIDER_CONCURRENCY.items()
    }

    eligible = [m for m in models if m["provider"] in clients]

    # Split into already-done (from partial) and remaining
    already_done = [m for m in eligible if m["model_id"] in partial]
    remaining = [m for m in eligible if m["model_id"] not in partial]

    skipped = [m for m in models if m["provider"] not in clients]
    for m in skipped:
        print(f"  SKIP: {m['name']} — no {m['provider']} client")

    # Restore already-completed results
    summaries = []
    all_results = []
    for m in already_done:
        cached = partial[m["model_id"]]
        summaries.append(cached["summary"])
        all_results.append({"model": m["name"], "results": cached["results"]})
        print(f"  CACHED: {m['name']}")

    if not remaining:
        print("  All models already completed! Delete benchmark_partial.json to re-run.")
        return summaries, all_results

    total_calls = len(remaining) * len(messages)
    n_paid = sum(1 for m in remaining if m["price_in"] > 0)
    n_free = len(remaining) - n_paid
    print(f"  Remaining: {len(remaining)} models ({n_paid} paid, {n_free} free)  Calls: {total_calls}")
    print(f"  Already done: {len(already_done)} models (from partial)")
    print(f"  Concurrency: paid=10, free={FREE_CONCURRENCY}")

    # Each model gets its own semaphore — no model blocks another
    # The semaphore limits concurrent requests *within* a single model
    model_sems = {
        m["model_id"]: asyncio.Semaphore(10 if m["price_in"] > 0 or ":free" not in m["model_id"] else FREE_CONCURRENCY)
        for m in remaining
    }

    class ProgressHandle:
        def __init__(self, prog, tid):
            self.progress = prog
            self.task_id = tid
        def advance(self, tid):
            self.progress.advance(tid)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=50),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("{task.completed}/{task.total} calls"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
    ) as prog:
        tid = prog.add_task("Benchmarking", total=total_calls)
        handle = ProgressHandle(prog, tid)

        # Launch remaining models concurrently
        tasks = []
        for model_config in remaining:
            provider = model_config["provider"]
            sem = model_sems[model_config["model_id"]]
            task = asyncio.create_task(
                benchmark_model_async(
                    clients[provider], model_config, messages, sem,
                    progress=handle, partial=partial, partial_lock=partial_lock,
                )
            )
            tasks.append((model_config["name"], task))

        # Gather results
        for name, task in tasks:
            try:
                result = await task
                if result:
                    summary, results = result
                    summaries.append(summary)
                    all_results.append({"model": name, "results": results})
            except Exception as e:
                print(f"  FAILED: {name} — {e}")

    return summaries, all_results


# ─── Task 16/20: Results comparison table ────────────────────────────────

def print_comparison_table(summaries: list[dict]) -> None:
    header = f"{'Model':<30} {'Provider':<12} {'JSON %':>7} {'Fields %':>8} {'Lat avg':>8} {'Lat p95':>8} {'Err':>4} {'In tok':>7} {'Out tok':>8}"
    print(header)
    print("-" * len(header))

    for s in sorted(summaries, key=lambda x: -x["json_valid_rate"]):
        print(
            f"{s['model']:<30} "
            f"{s['provider']:<12} "
            f"{100*s['json_valid_rate']:>6.1f}% "
            f"{100*s['required_fields_rate']:>7.1f}% "
            f"{s['latency_avg']:>7.2f}s "
            f"{s['latency_p95']:>7.2f}s "
            f"{s['error_count']:>4} "
            f"{s['avg_input_tokens']:>6.0f} "
            f"{s['avg_output_tokens']:>7.0f}"
        )


# ─── Task 17/20: Content type distribution ──────────────────────────────

def print_content_type_dist(summaries: list[dict]) -> None:
    types = ["assignment", "announcement", "event", "reminder", "material", "raw_message"]
    header = f"{'Model':<30}" + "".join(f"{t:>12}" for t in types) + f"{'other':>8}"
    print(header)
    print("-" * len(header))

    for s in summaries:
        d = s["content_type_dist"]
        known = sum(d.get(t, 0) for t in types)
        other = s["total_messages"] - known
        row = f"{s['model']:<30}" + "".join(f"{d.get(t, 0):>12}" for t in types) + f"{other:>8}"
        print(row)


# ─── Task 18/20: Cost projections ────────────────────────────────────────

SCALE_TIERS = [
    {"users": 20,   "msgs_day": 30},
    {"users": 50,   "msgs_day": 50},
    {"users": 100,  "msgs_day": 80},
    {"users": 300,  "msgs_day": 120},
    {"users": 500,  "msgs_day": 150},
    {"users": 1000, "msgs_day": 200},
    {"users": 1500, "msgs_day": 250},
]


def compute_cost_projections(summaries: list[dict]) -> list[dict]:
    projections = []

    for s in summaries:
        row = {"model": s["model"], "provider": s["provider"], "tiers": {}}
        for tier in SCALE_TIERS:
            monthly_msgs = tier["msgs_day"] * 30
            cost_in = (s["avg_input_tokens"] * monthly_msgs / 1_000_000) * s["price_in_per_m"]
            cost_out = (s["avg_output_tokens"] * monthly_msgs / 1_000_000) * s["price_out_per_m"]
            total = round(cost_in + cost_out, 4)
            row["tiers"][str(tier["users"])] = total
        projections.append(row)

    # Print
    header = f"{'Model':<30}" + "".join(f"{t['users']:>8}u" for t in SCALE_TIERS)
    print(header)
    print("-" * len(header))
    for row in projections:
        line = f"{row['model']:<30}"
        for tier in SCALE_TIERS:
            cost = row["tiers"][str(tier["users"])]
            if cost < 0.01:
                line += f"  {'<$0.01':>6}"
            else:
                line += f"  ${cost:>5.2f}"
        print(line)

    return projections


# ─── Task 19/20: Weighted scoring matrix ─────────────────────────────────

WEIGHTS = {"accuracy": 0.40, "json_reliability": 0.30, "speed": 0.20, "cost": 0.10}


def compute_scores(summaries: list[dict]) -> list[dict]:
    max_lat = max((s["latency_avg"] for s in summaries), default=1)

    scored = []
    for s in summaries:
        accuracy_score = s["required_fields_rate"]
        json_score = s["json_valid_rate"]
        speed_score = 1 - (s["latency_avg"] / max_lat) if max_lat > 0 else 1

        monthly_cost_1500 = (
            (s["avg_input_tokens"] * 7500 / 1e6) * s["price_in_per_m"]
            + (s["avg_output_tokens"] * 7500 / 1e6) * s["price_out_per_m"]
        )
        cost_score = 1.0 if monthly_cost_1500 < 0.01 else max(0, 1 - monthly_cost_1500 / 5)

        weighted = (
            accuracy_score * WEIGHTS["accuracy"]
            + json_score * WEIGHTS["json_reliability"]
            + speed_score * WEIGHTS["speed"]
            + cost_score * WEIGHTS["cost"]
        )

        scored.append({
            "model": s["model"],
            "provider": s["provider"],
            "accuracy": round(accuracy_score, 4),
            "json_reliability": round(json_score, 4),
            "speed": round(speed_score, 4),
            "cost": round(cost_score, 4),
            "weighted_total": round(weighted, 4),
        })

    scored.sort(key=lambda x: -x["weighted_total"])

    # Print
    print(f"{'Rank':<5} {'Model':<30} {'Accuracy':>9} {'JSON':>9} {'Speed':>9} {'Cost':>9} {'TOTAL':>9}")
    print("-" * 85)
    for i, s in enumerate(scored, 1):
        marker = " <-- WINNER" if i == 1 else ""
        print(
            f"{i:<5} {s['model']:<30} "
            f"{s['accuracy']:>8.1%} "
            f"{s['json_reliability']:>8.1%} "
            f"{s['speed']:>8.1%} "
            f"{s['cost']:>8.1%} "
            f"{s['weighted_total']:>8.1%}{marker}"
        )

    return scored


# ─── Task 20/20: Dump all results to JSON ────────────────────────────────

def save_results(
    prefilter_stats: dict,
    token_stats: dict,
    volume_stats: dict,
    summaries: list[dict],
    all_results: list[dict],
    cost_projections: list[dict],
    scores: list[dict],
    sample_size: int,
    total_corpus: int,
    real_count: int,
) -> Path:
    output = {
        "timestamp": datetime.now(UTC).isoformat(),
        "config": {
            "sample_size": sample_size,
            "total_corpus": total_corpus,
            "real_messages": real_count,
            "system_prompt_tokens": estimate_tokens(SYSTEM_PROMPT),
            "models_tested": len(summaries),
            "weights": WEIGHTS,
            "scale_tiers": SCALE_TIERS,
        },
        "data_analysis": {
            "prefilter": prefilter_stats,
            "token_distribution": token_stats,
            "daily_volume": volume_stats,
        },
        "model_summaries": summaries,
        "cost_projections": cost_projections,
        "scoring": scores,
        "detailed_results": [
            {
                "model": entry["model"],
                "results": [
                    {k: v for k, v in r.items() if k != "parsed"}
                    for r in entry["results"]
                ],
            }
            for entry in all_results
        ],
    }

    out_path = Path(__file__).parent / "benchmark_results.json"
    out_path.write_text(json.dumps(output, indent=2, default=str, ensure_ascii=False))
    print(f"  Saved to: {out_path}")
    print(f"  File size: {out_path.stat().st_size / 1024:.1f} KB")
    return out_path


# ─── Main ────────────────────────────────────────────────────────────────

TOP6_MODEL_IDS = [
    "google/gemini-2.0-flash-lite-001",
    "mistralai/mistral-nemo",
    "cohere/command-r7b-12-2024",
    "microsoft/phi-4",
    "openai/gpt-4o-mini",
]


async def main_async(resume: bool = False, paid_only: bool = False, free_only: bool = False, top6: bool = False, round2: bool = False, from_gold: bool = False, sample_size: int = 50) -> None:
    start_time = time.time()

    print("=" * 60)
    print("LLM Cost Research Benchmark (PARALLEL)")
    print("=" * 60)

    # Task 1: Load env and init clients
    print("\n[1/20] Loading environment and API clients...")
    load_env()
    clients = init_async_clients()
    if not clients:
        print("No API clients available. Add keys to .env")
        sys.exit(1)

    # Task 2: Load data dumps
    print("\n[2/20] Loading data dumps...")
    teams_items, tg_items = load_dumps()

    # Task 3: Merge raw_messages
    print("\n[3/20] Merging raw_messages...")
    raw = merge_raw_messages(teams_items, tg_items)

    # Task 4-5: Pre-filter
    print("\n[4-5/20] Running pre-filter (junk detection)...")
    real, junk, prefilter_stats = run_prefilter(raw)

    # Task 6: Token distribution
    print("\n[6/20] Computing token distribution (real messages)...")
    token_stats = token_distribution(real)

    # Task 7: Daily volume
    print("\n[7/20] Computing daily volume stats (real messages)...")
    volume_stats = daily_volume_stats(real)

    # Task 8: System prompt
    print("\n[8/20] System prompt ready.")
    print(f"  Words: {len(SYSTEM_PROMPT.split())}  Est. tokens: {estimate_tokens(SYSTEM_PROMPT)}")

    # Task 9: Filter and show model configs
    models_to_run = MODELS
    if round2:
        models_to_run = [m for m in MODELS if m["model_id"] in ROUND2_MODEL_IDS]
        print(f"\n[9/20] --round2: {len(models_to_run)} models selected")
    elif top6:
        models_to_run = [m for m in MODELS if m["model_id"] in TOP6_MODEL_IDS]
        print(f"\n[9/20] --top6: {len(models_to_run)} models selected")
    elif paid_only:
        models_to_run = [m for m in MODELS if m["price_in"] > 0]
        print(f"\n[9/20] --paid-only: {len(models_to_run)} models selected")
    elif free_only:
        models_to_run = [m for m in MODELS if m["price_in"] == 0]
        print(f"\n[9/20] --free-only: {len(models_to_run)} models selected")
    else:
        print(f"\n[9/20] All {len(models_to_run)} models selected")
    for m in models_to_run:
        print(f"  [{m['provider']:<12}] {m['name']:<30} ${m['price_in']}/{m['price_out']} per M tok")

    # Task 10-12: Sample messages
    if from_gold:
        gold_path = Path(__file__).parent / "gold_labels.json"
        print(f"\n[10-12/20] Loading messages from gold labels ({gold_path.name})...")
        with open(gold_path) as f:
            gold_data = json.load(f)
        gold_labels_list = gold_data["labels"]
        # Convert gold label entries to benchmark message format
        bench_messages = [
            {
                "external_id": g["external_id"],
                "body": g["input_body"],
                "platform": g["platform"],
                "course_name": g.get("course_name", ""),
            }
            for g in gold_labels_list
        ]
        if sample_size < len(bench_messages):
            random.seed(42)
            bench_messages = random.sample(bench_messages, sample_size)
        print(f"  Using {len(bench_messages)} gold-labeled messages")
    else:
        print("\n[10-12/20] Sampling benchmark messages...")
        bench_messages = sample_messages(real, n=sample_size, seed=42)

    # Task 13-15: Run benchmarks (ALL MODELS IN PARALLEL)
    print("\n[13-15/20] Running benchmarks (parallel)...")
    print("=" * 60)
    if not resume:
        clear_partial()
    summaries, all_results = await run_all_benchmarks_async(clients, models_to_run, bench_messages)

    if not summaries:
        print("No benchmarks completed!")
        sys.exit(1)

    # Task 16
    print("\n" + "=" * 60)
    print("[16/20] Results Comparison")
    print("=" * 60)
    print_comparison_table(summaries)

    # Task 17
    print(f"\n[17/20] Content Type Distribution")
    print("-" * 60)
    print_content_type_dist(summaries)

    # Task 18
    print(f"\n[18/20] Cost Projections (monthly, after pre-filter)")
    print("-" * 60)
    cost_projections = compute_cost_projections(summaries)

    # Task 19
    print(f"\n[19/20] Weighted Scoring Matrix")
    print("-" * 60)
    scores = compute_scores(summaries)

    # Task 20
    print(f"\n[20/20] Saving results...")
    save_results(
        prefilter_stats=prefilter_stats,
        token_stats=token_stats,
        volume_stats=volume_stats,
        summaries=summaries,
        all_results=all_results,
        cost_projections=cost_projections,
        scores=scores,
        sample_size=len(bench_messages),
        total_corpus=len(raw),
        real_count=len(real),
    )

    # Full run succeeded — clean up partial file
    clear_partial()

    # Close async clients
    for client in clients.values():
        await client.close()

    duration = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"DONE in {duration:.1f}s")
    print(f"Models tested: {len(summaries)}/{len(MODELS)}")
    print(f"Winner: {scores[0]['model']} (score: {scores[0]['weighted_total']:.1%})")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="LLM benchmark")
    parser.add_argument("--resume", action="store_true", help="Resume from benchmark_partial.json")
    parser.add_argument("--paid-only", action="store_true", help="Only run paid models (skip :free)")
    parser.add_argument("--free-only", action="store_true", help="Only run free models")
    parser.add_argument("--top6", action="store_true", help="Only run top 6 models (100%% JSON valid)")
    parser.add_argument("--round2", action="store_true", help="Run 15 new cheap paid models")
    parser.add_argument("--from-gold", action="store_true", help="Load messages from gold_labels.json instead of sampling")
    parser.add_argument("-n", "--sample-size", type=int, default=50, help="Number of messages to sample (default: 50)")
    args = parser.parse_args()
    asyncio.run(main_async(resume=args.resume, paid_only=args.paid_only, free_only=args.free_only, top6=args.top6, round2=args.round2, from_gold=args.from_gold, sample_size=args.sample_size))
