from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


GENERIC_TITLES = {"announcement", "assignment", "event", "material"}


class ScriptError(Exception):
    """A configuration, database, or filesystem problem prevented inspection."""


def build_inspector_rows(
    raw_data: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in raw_data.get("classified_rows", []):
        rows.append(
            {
                "classified_item_id": row.get("classified_item_id"),
                "ingested_item_id": row.get("ingested_item_id"),
                "external_id": row.get("external_id"),
                "platform": row.get("platform"),
                "entity_type": row.get("entity_type"),
                "course": {
                    "id": row.get("course_id"),
                    "name": row.get("course_name"),
                    "code": row.get("course_code"),
                },
                "classified": {
                    "title": _optional_text(row.get("title")),
                    "body": _optional_text(row.get("body")),
                    "published_at": row.get("published_at"),
                    "source_url": _optional_text(row.get("source_url")),
                    "dedup_status": row.get("dedup_status"),
                    "canonical_entity_id": row.get("canonical_entity_id"),
                },
                "assignment": {
                    "deadline": row.get("deadline"),
                    "submission_format": _optional_text(row.get("submission_format")),
                },
                "event": {
                    "start_time": row.get("start_time"),
                    "end_time": row.get("end_time"),
                    "location": _optional_text(row.get("location")),
                },
                "source": {
                    "content_type": row.get("source_content_type"),
                    "title": _optional_text(row.get("source_title")),
                    "body": _optional_text(row.get("source_body")),
                    "published_at": row.get("source_published_at"),
                    "payload": row.get("source_payload") or {},
                },
                "classification": {
                    "entity_type": row.get("classification_entity_type"),
                    "method": row.get("classification_method"),
                    "model": _optional_text(row.get("llm_model")),
                    "confidence": row.get("confidence"),
                    "processor_version": _optional_text(row.get("processor_version")),
                    "prompt_version": _optional_text(row.get("prompt_version")),
                    "processing_time_ms": row.get("processing_time_ms"),
                    "has_raw_output": bool(_optional_text(row.get("llm_raw_output"))),
                },
                "dedup": {
                    "status": row.get("dedup_log_status"),
                    "match_strategy": _optional_text(row.get("match_strategy")),
                    "resolution_strategy": _optional_text(
                        row.get("resolution_strategy")
                    ),
                    "score": row.get("dedup_score"),
                },
                "counts": {
                    "links": row.get("link_count") or 0,
                    "attachments": row.get("attachment_count") or 0,
                },
                "flags": quality_flags(row),
            }
        )
    return rows


def quality_flags(row: dict[str, Any]) -> list[str]:
    title = _optional_text(row.get("title"))
    body = _optional_text(row.get("body"))
    entity_type = _optional_text(row.get("entity_type"))
    flags: list[str] = []

    if title is None and body is None:
        flags.append("empty_publishable")
    if title is None:
        flags.append("missing_title")
    if body is None:
        flags.append("missing_body")
    if _looks_like_json_body(body):
        flags.append("json_body")
    if entity_type == "event" and row.get("start_time") is None:
        flags.append("missing_start_time")
    if entity_type == "assignment" and row.get("deadline") is None:
        flags.append("missing_deadline")
    if _optional_text(row.get("source_url")) is None:
        flags.append("missing_source_url")
    if (
        row.get("classification_method") == "llm"
        and _optional_text(row.get("llm_raw_output")) is None
    ):
        flags.append("missing_llm_raw_output")
    if title is None or title.lower() in GENERIC_TITLES:
        flags.append("generic_title")

    return flags


def summarize_flags(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counter: Counter[str] = Counter()
    flagged_rows = 0
    for row in rows:
        flags = list(row.get("flags", []))
        if flags:
            flagged_rows += 1
        counter.update(flags)
    return {
        "rows": len(rows),
        "flagged_rows": flagged_rows,
        "flags": dict(sorted(counter.items())),
    }


def build_inspector_document(
    raw_data: dict[str, list[dict[str, Any]]],
    *,
    run_dir: Path,
    generated_at: datetime,
) -> dict[str, Any]:
    rows = build_inspector_rows(raw_data)
    return {
        "generated_at": _format_datetime(generated_at),
        "run_dir": str(run_dir),
        "summary": {
            **summarize_flags(rows),
            "by_platform": _count_rows(rows, "platform"),
            "by_entity_type": _count_rows(rows, "entity_type"),
        },
        "rows": rows,
    }


def render_html(document: dict[str, Any]) -> str:
    summary = document["summary"]
    rows = list(document["rows"])
    data_json = _safe_json(document)
    initial_rows = "\n".join(_render_row(row) for row in rows)
    flag_options = "\n".join(
        f'<option value="{_esc(flag)}">{_esc(flag)}</option>'
        for flag in sorted(summary.get("flags", {}))
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Endpoint Static Run Inspector</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; color: #171717; }}
    header {{ padding: 16px 20px; border-bottom: 1px solid #d9dde3; }}
    main {{ padding: 20px; }}
    .summary, .filters {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    .metric, .row {{ border: 1px solid #d9dde3; padding: 10px; }}
    .metric strong {{ display: block; font-size: 22px; }}
    .filters {{ margin: 18px 0; }}
    label {{ display: grid; gap: 4px; font-size: 13px; font-weight: 600; }}
    input, select {{ border: 1px solid #b9c0ca; font: inherit; padding: 7px; }}
    .rows {{ display: grid; gap: 12px; }}
    .row h2 {{ font-size: 18px; margin: 0 0 6px; }}
    .meta {{ color: #505866; margin: 4px 0; }}
    .flags {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }}
    .flag {{ border: 1px solid #c28c00; padding: 2px 6px; font-size: 12px; }}
    .body {{ white-space: pre-wrap; }}
    pre {{ overflow: auto; background: #f6f7f9; padding: 10px; }}
  </style>
</head>
<body>
  <header>
    <h1>Endpoint Static Run Inspector</h1>
    <p>Run: {_esc(document["run_dir"])}</p>
    <p>Generated: {_esc(document["generated_at"])}</p>
  </header>
  <main>
    <section class="summary">
      <div class="metric"><span>Rows</span><strong>{summary["rows"]}</strong></div>
      <div class="metric"><span>Flagged</span><strong>{summary["flagged_rows"]}</strong></div>
      <div class="metric"><span>Platforms</span><strong>{len(summary["by_platform"])}</strong></div>
      <div class="metric"><span>Entity types</span><strong>{len(summary["by_entity_type"])}</strong></div>
    </section>
    <section class="filters" aria-label="Inspector filters">
      <label>Search<input id="search" type="search"></label>
      <label>Platform<select id="platform"><option value="">All</option></select></label>
      <label>Entity<select id="entity"><option value="">All</option></select></label>
      <label>Course<select id="course"><option value="">All</option></select></label>
      <label>Flag<select id="flag"><option value="">All</option>{flag_options}</select></label>
      <label>Only flagged<input id="onlyFlagged" type="checkbox"></label>
    </section>
    <section id="rows" class="rows">{initial_rows}</section>
  </main>
  <script type="application/json" id="inspector-data">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById("inspector-data").textContent);
    const rowsEl = document.getElementById("rows");
    const searchEl = document.getElementById("search");
    const platformEl = document.getElementById("platform");
    const entityEl = document.getElementById("entity");
    const courseEl = document.getElementById("course");
    const flagEl = document.getElementById("flag");
    const onlyFlaggedEl = document.getElementById("onlyFlagged");

    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#x27;");
    }}
    function unique(values) {{
      return [...new Set(values.filter(Boolean))].sort();
    }}
    function addOptions(select, values) {{
      for (const value of unique(values)) {{
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
      }}
    }}
    addOptions(platformEl, data.rows.map((row) => row.platform));
    addOptions(entityEl, data.rows.map((row) => row.entity_type));
    addOptions(courseEl, data.rows.map((row) => row.course?.name || "No course"));
    function searchable(row) {{
      return [
        row.external_id,
        row.platform,
        row.entity_type,
        row.course?.name,
        row.classified?.title,
        row.classified?.body,
        row.source?.body,
      ].join(" ").toLowerCase();
    }}
    function renderRow(row) {{
      const title = row.classified?.title || "[missing title]";
      const body = row.classified?.body || "";
      const sourceUrl = row.classified?.source_url;
      const sourceUrlHtml = sourceUrl
        ? `<a href="${{escapeHtml(sourceUrl)}}" target="_blank" rel="noreferrer">${{escapeHtml(sourceUrl)}}</a>`
        : "missing";
      const flags = (row.flags || [])
        .map((flag) => `<span class="flag">${{escapeHtml(flag)}}</span>`)
        .join("");
      return `<article class="row">
        <h2>${{escapeHtml(title)}}</h2>
        <p class="meta">${{escapeHtml(row.platform)}} / ${{escapeHtml(row.entity_type)}} / ${{escapeHtml(row.course?.name || "No course")}}</p>
        <p class="meta">Published: ${{escapeHtml(row.classified?.published_at || "-")}} | Model: ${{escapeHtml(row.classification?.model || "-")}} | Confidence: ${{escapeHtml(row.classification?.confidence ?? "-")}} | Dedup: ${{escapeHtml(row.dedup?.status || row.classified?.dedup_status || "-")}}</p>
        <p class="meta"><strong>Source URL:</strong> ${{sourceUrlHtml}}</p>
        <div class="flags">${{flags}}</div>
        <h3>Body</h3>
        <div class="body">${{escapeHtml(body)}}</div>
        <details><summary>Full row JSON</summary><pre>${{escapeHtml(JSON.stringify(row, null, 2))}}</pre></details>
      </article>`;
    }}
    function render() {{
      const q = searchEl.value.trim().toLowerCase();
      const platform = platformEl.value;
      const entity = entityEl.value;
      const course = courseEl.value;
      const flag = flagEl.value;
      const onlyFlagged = onlyFlaggedEl.checked;
      const filtered = data.rows.filter((row) => {{
        if (q && !searchable(row).includes(q)) return false;
        if (platform && row.platform !== platform) return false;
        if (entity && row.entity_type !== entity) return false;
        if (course && (row.course?.name || "No course") !== course) return false;
        if (flag && !(row.flags || []).includes(flag)) return false;
        if (onlyFlagged && !(row.flags || []).length) return false;
        return true;
      }});
      rowsEl.innerHTML = filtered.map(renderRow).join("")
        || '<p class="row">No rows match these filters.</p>';
    }}
    for (const el of [searchEl, platformEl, entityEl, courseEl, flagEl, onlyFlaggedEl]) {{
      el.addEventListener("input", render);
      el.addEventListener("change", render);
    }}
  </script>
</body>
</html>
"""


def fetch_inspector_data(repo_root: Path) -> dict[str, list[dict[str, Any]]]:
    postgres_db = _read_config_value(repo_root, "POSTGRES_DB")
    postgres_user = _read_config_value(repo_root, "POSTGRES_USER")
    if not postgres_db or not postgres_user:
        raise ScriptError(
            "POSTGRES_DB and POSTGRES_USER must be set in the environment or .env"
        )

    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(repo_root / "docker-compose.yml"),
            "exec",
            "-T",
            "db",
            "psql",
            f"--username={postgres_user}",
            f"--dbname={postgres_db}",
            "--no-align",
            "--tuples-only",
            "--quiet",
            "--command",
            _inspector_sql(),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        message = (
            result.stderr.strip() or result.stdout.strip() or "unknown psql failure"
        )
        raise ScriptError(f"Database query failed: {message}")

    try:
        raw_data = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ScriptError(f"Database query returned invalid JSON: {exc}") from exc
    return _normalize_inspector_data(raw_data)


def write_artifacts(document: dict[str, Any], run_dir: Path) -> tuple[Path, Path]:
    inspection_dir = run_dir / "inspection"
    inspection_dir.mkdir(parents=True, exist_ok=True)
    data_path = inspection_dir / "inspector-data.json"
    html_path = inspection_dir / "index.html"
    data_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    html_path.write_text(render_html(document), encoding="utf-8")
    return html_path, data_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a static HTML inspector for a processor integration run."
    )
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    try:
        raw_data = fetch_inspector_data(repo_root)
        document = build_inspector_document(
            raw_data,
            run_dir=args.run_dir,
            generated_at=datetime.now(UTC),
        )
        html_path, data_path = write_artifacts(document, args.run_dir)
    except (OSError, ScriptError) as exc:
        print(f"Script error: {exc}", file=sys.stderr)
        return 2

    print(f"Inspector written: {html_path}")
    print(f"Inspector data written: {data_path}")
    return 0


def _render_row(row: dict[str, Any]) -> str:
    title = _nested_text(row, "classified", "title") or "[missing title]"
    body = _nested_text(row, "classified", "body") or ""
    source_url = _nested_text(row, "classified", "source_url")
    source_url_html = (
        f'<a href="{_esc(source_url)}" target="_blank" rel="noreferrer">{_esc(source_url)}</a>'
        if source_url
        else "missing"
    )
    flags = "".join(
        f'<span class="flag">{_esc(flag)}</span>' for flag in row.get("flags", [])
    )
    return (
        '<article class="row">'
        f"<h2>{_esc(title)}</h2>"
        f'<p class="meta">{_esc(row.get("platform"))} / '
        f"{_esc(row.get('entity_type'))} / "
        f"{_esc(_nested_text(row, 'course', 'name') or 'No course')}</p>"
        f'<p class="meta">Published: {_esc(_nested_text(row, "classified", "published_at") or "-")} | '
        f"Model: {_esc(_nested_text(row, 'classification', 'model') or '-')} | "
        f"Dedup: {_esc(_nested_text(row, 'dedup', 'status') or _nested_text(row, 'classified', 'dedup_status') or '-')}</p>"
        f'<p class="meta"><strong>Source URL:</strong> {source_url_html}</p>'
        f'<div class="flags">{flags}</div>'
        "<h3>Body</h3>"
        f'<div class="body">{_esc(body)}</div>'
        "<details><summary>Full row JSON</summary>"
        f"<pre>{_esc(json.dumps(row, indent=2, sort_keys=True))}</pre>"
        "</details></article>"
    )


def _normalize_inspector_data(raw_data: object) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw_data, dict):
        raise ScriptError("Database query returned JSON that was not an object")
    rows = raw_data.get("classified_rows", [])
    if not isinstance(rows, list):
        raise ScriptError("Database query returned non-list classified_rows")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ScriptError("Database query returned non-object classified row")
        normalized.append(row)
    return {"classified_rows": normalized}


def _inspector_sql() -> str:
    return """
SELECT jsonb_build_object(
    'classified_rows', (
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'classified_item_id', b.id::text,
            'ingested_item_id', i.id::text,
            'external_id', i.external_id,
            'platform', i.platform,
            'processing_status', i.processing_status,
            'entity_type', b.entity_type,
            'course_id', c.id::text,
            'course_name', c.name,
            'course_code', c.code,
            'title', b.title,
            'body', b.body,
            'published_at', b.published_at,
            'source_url', i.source_url,
            'dedup_status', b.dedup_status,
            'canonical_entity_id', b.canonical_entity_id::text,
            'source_content_type', i.payload->>'content_type',
            'source_title', i.payload->>'title',
            'source_body', i.payload->>'body',
            'source_published_at', i.payload->>'published_at',
            'source_payload', i.payload,
            'deadline', a.deadline,
            'submission_format', a.submission_format,
            'start_time', e.start_time,
            'end_time', e.end_time,
            'location', e.location,
            'classification_entity_type', cl.entity_type,
            'classification_method', cl.classification_method,
            'llm_model', cl.llm_model,
            'confidence', cl.confidence,
            'processor_version', cl.processor_version,
            'prompt_version', cl.prompt_version,
            'processing_time_ms', cl.processing_time_ms,
            'llm_raw_output', cl.llm_raw_output,
            'dedup_log_status', dl.status,
            'match_strategy', dl.match_strategy,
            'resolution_strategy', dl.resolution_strategy,
            'dedup_score', dl.score,
            'link_count', (
                SELECT COUNT(*) FROM links l WHERE l.classified_item_id = b.id
            ),
            'attachment_count', (
                SELECT COUNT(*) FROM attachments att WHERE att.classified_item_id = b.id
            )
        ) ORDER BY b.created_at, b.id), '[]'::jsonb)
        FROM base_classified_items b
        JOIN ingested_items i ON i.id = b.ingested_item_id
        LEFT JOIN courses c ON c.id = b.course_id
        LEFT JOIN assignments a ON a.baseclassifieditem_ptr_id = b.id
        LEFT JOIN events e ON e.baseclassifieditem_ptr_id = b.id
        LEFT JOIN LATERAL (
            SELECT *
            FROM classification_logs cl
            WHERE cl.classified_item_id = b.id
            ORDER BY cl.created_at DESC, cl.id DESC
            LIMIT 1
        ) cl ON true
        LEFT JOIN LATERAL (
            SELECT *
            FROM dedup_logs dl
            WHERE dl.classified_item_id = b.id
            ORDER BY dl.created_at DESC, dl.id DESC
            LIMIT 1
        ) dl ON true
    )
)::text
"""


def _count_rows(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = row.get(key)
        if value is not None:
            counts[str(value)] += 1
    return dict(sorted(counts.items()))


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _nested_text(row: dict[str, Any], outer_key: str, inner_key: str) -> str | None:
    value = row.get(outer_key)
    if not isinstance(value, dict):
        return None
    return _optional_text(value.get(inner_key))


def _looks_like_json_body(value: str | None) -> bool:
    if value is None:
        return False
    stripped = value.strip()
    return stripped.startswith("{") or "scopeId" in stripped or "storageId" in stripped


def _read_config_value(repo_root: Path, key: str) -> str | None:
    env_value = os.environ.get(key)
    if env_value:
        return env_value

    env_path = repo_root / ".env"
    if not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        env_key, value = stripped.split("=", 1)
        if env_key.strip() == key:
            return _strip_env_value(value)
    return None


def _strip_env_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


if __name__ == "__main__":
    raise SystemExit(main())
