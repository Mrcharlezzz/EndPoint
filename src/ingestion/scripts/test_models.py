"""Quick parallel test of all models — verifies each one responds to a classification prompt.

Retries 429s, handles None content, runs with concurrency limits.
"""

import asyncio
import json
import os
import time
from pathlib import Path

# Load .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from openai import AsyncOpenAI

PROMPT = '''You classify university chat messages. Return ONLY a JSON object with these fields:
- content_type: one of "assignment", "announcement", "event", "reminder", "material", "raw_message"
- title: short title or null
- confidence: 0.0 to 1.0
- due_at: ISO 8601 datetime or null
- action_items: list of strings or empty list

Return ONLY the JSON, no markdown fences, no explanation.

Message: "Don't forget the quiz tomorrow at 9am"'''

MODELS = [
    # Groq
    ("groq", "llama-3.1-8b-instant"),
    ("groq", "meta-llama/llama-4-scout-17b-16e-instruct"),
    ("groq", "qwen/qwen3-32b"),
    # OpenRouter free
    ("openrouter", "google/gemma-3-4b-it:free"),
    ("openrouter", "google/gemma-3-12b-it:free"),
    ("openrouter", "google/gemma-3-27b-it:free"),
    ("openrouter", "google/gemma-3n-e2b-it:free"),
    ("openrouter", "google/gemma-3n-e4b-it:free"),
    ("openrouter", "qwen/qwen3-4b:free"),
    ("openrouter", "qwen/qwen3-coder:free"),
    ("openrouter", "qwen/qwen3-next-80b-a3b-instruct:free"),
    ("openrouter", "meta-llama/llama-3.2-3b-instruct:free"),
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ("openrouter", "nvidia/nemotron-3-nano-30b-a3b:free"),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
    ("openrouter", "nvidia/nemotron-nano-9b-v2:free"),
    ("openrouter", "nvidia/nemotron-nano-12b-v2-vl:free"),
    ("openrouter", "mistralai/mistral-small-3.1-24b-instruct:free"),
    ("openrouter", "nousresearch/hermes-3-llama-3.1-405b:free"),
    ("openrouter", "stepfun/step-3.5-flash:free"),
    ("openrouter", "z-ai/glm-4.5-air:free"),
    ("openrouter", "arcee-ai/trinity-large-preview:free"),
    ("openrouter", "arcee-ai/trinity-mini:free"),
    ("openrouter", "liquid/lfm-2.5-1.2b-instruct:free"),
    ("openrouter", "liquid/lfm-2.5-1.2b-thinking:free"),
    ("openrouter", "cognitivecomputations/dolphin-mistral-24b-venice-edition:free"),
    # OpenRouter paid
    ("openrouter", "meta-llama/llama-3.1-8b-instruct"),
    ("openrouter", "mistralai/mistral-nemo"),
    ("openrouter", "cohere/command-r7b-12-2024"),
    ("openrouter", "qwen/qwen3.5-9b"),
    ("openrouter", "openai/gpt-5-nano"),
    ("openrouter", "qwen/qwen3-8b"),
    ("openrouter", "microsoft/phi-4"),
    ("openrouter", "qwen/qwen3-14b"),
    ("openrouter", "google/gemini-2.0-flash-lite-001"),
    ("openrouter", "qwen/qwen3-32b"),
    ("openrouter", "openai/gpt-4o-mini"),
]

MAX_RETRIES = 3
RETRY_BACKOFF = [3, 8, 15]


async def test_model(client, provider: str, model_id: str, sem: asyncio.Semaphore, progress=None) -> dict:
    for attempt in range(MAX_RETRIES):
        async with sem:
            start = time.time()
            try:
                r = await client.chat.completions.create(
                    model=model_id,
                    messages=[{"role": "user", "content": PROMPT}],
                    max_tokens=200,
                    temperature=0.1,
                )
                latency = round(time.time() - start, 2)

                # Handle None content
                raw_content = r.choices[0].message.content if r.choices and r.choices[0].message.content else ""
                content = raw_content.strip()

                if not content:
                    result = {
                        "provider": provider,
                        "model_id": model_id,
                        "status": "OK",
                        "latency": latency,
                        "json_valid": False,
                        "content_type": "EMPTY",
                        "raw": "(empty response)",
                    }
                    if progress:
                        progress.advance(progress.task_id)
                    return result

                # Try parse JSON
                cleaned = content
                if cleaned.startswith("```"):
                    cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                try:
                    parsed = json.loads(cleaned)
                    has_ct = "content_type" in parsed
                    ct = parsed.get("content_type", "?")
                except (json.JSONDecodeError, TypeError):
                    has_ct = False
                    ct = "PARSE_FAIL"

                result = {
                    "provider": provider,
                    "model_id": model_id,
                    "status": "OK",
                    "latency": latency,
                    "json_valid": has_ct,
                    "content_type": ct,
                    "raw": content[:120],
                }
                if progress:
                    progress.advance(progress.task_id)
                return result

            except Exception as e:
                err_str = str(e)
                # Retry on 429 rate limit
                if "429" in err_str and attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    await asyncio.sleep(wait)
                    continue

                result = {
                    "provider": provider,
                    "model_id": model_id,
                    "status": "ERROR",
                    "latency": round(time.time() - start, 2),
                    "json_valid": False,
                    "content_type": "ERROR",
                    "error": err_str[:200],
                }
                if progress:
                    progress.advance(progress.task_id)
                return result

    # Max retries exhausted
    if progress:
        progress.advance(progress.task_id)
    return {
        "provider": provider, "model_id": model_id, "status": "ERROR",
        "latency": 0, "json_valid": False, "content_type": "ERROR",
        "error": "max retries (429)",
    }


async def main():
    clients = {}
    groq_key = os.environ.get("GROQ_API_KEY", "")
    or_key = os.environ.get("OPENROUTER_API_KEY", "")

    if groq_key:
        clients["groq"] = AsyncOpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key)
    if or_key:
        clients["openrouter"] = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=or_key)

    # Low concurrency to avoid 429s — only 2 OpenRouter at a time
    sems = {"groq": asyncio.Semaphore(10), "openrouter": asyncio.Semaphore(2)}

    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

    eligible = [(p, m) for p, m in MODELS if p in clients]
    start = time.time()

    class ProgressHandle:
        def __init__(self, prog, tid):
            self.progress = prog
            self.task_id = tid
        def advance(self, tid):
            self.progress.advance(tid)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
    ) as prog:
        tid = prog.add_task("Testing models", total=len(eligible))
        handle = ProgressHandle(prog, tid)

        tasks = []
        for provider, model_id in eligible:
            tasks.append(test_model(clients[provider], provider, model_id, sems[provider], progress=handle))

        results = await asyncio.gather(*tasks)

    # Print results
    ok = [r for r in results if r["status"] == "OK" and r["json_valid"]]
    ok_no_json = [r for r in results if r["status"] == "OK" and not r["json_valid"]]
    errors = [r for r in results if r["status"] == "ERROR"]

    print(f"\n{'Status':<6} {'Provider':<12} {'Model':<58} {'Lat':>6} {'JSON':>5} {'CT':<15}")
    print("-" * 108)
    for r in sorted(results, key=lambda x: (x["status"] != "OK", not x["json_valid"], x["model_id"])):
        status = "OK" if r["status"] == "OK" else "ERR"
        json_ok = "Y" if r["json_valid"] else "N"
        model_short = r["model_id"][:56]
        ct = r["content_type"][:14]
        lat = f"{r['latency']}s"
        print(f"{status:<6} {r['provider']:<12} {model_short:<58} {lat:>6} {json_ok:>5} {ct:<15}")
        if r["status"] == "ERROR":
            print(f"       -> {r.get('error', '')[:100]}")

    elapsed = round(time.time() - start, 1)
    print(f"\n{'='*60}")
    print(f"Done in {elapsed}s")
    print(f"  OK + JSON valid:   {len(ok)}/{len(results)}")
    print(f"  OK but bad JSON:   {len(ok_no_json)}/{len(results)}")
    print(f"  Errors:            {len(errors)}/{len(results)}")
    print(f"{'='*60}")

    # Close clients
    for c in clients.values():
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
