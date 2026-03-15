from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class RunHandle:
    run_id: int
    step: str
    started_at: datetime
    trigger_source: str
    parent_run_id: int | None = None


@dataclass(frozen=True)
class ModelPricing:
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    cached_input_cost_per_million: float | None = None


class UsageTracker:
    def __init__(self) -> None:
        self._lock = Lock()
        self._summary = {
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_prompt_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "llm_cost_usd": 0.0,
            "unpriced_calls": 0,
            "models": {},
            "unpriced_models": [],
        }

    def record(self, *, model: str, operation: str, usage: dict[str, int], cost_usd: float | None) -> None:
        model_name = str(model or "").strip() or "(unknown)"
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        cached_prompt_tokens = int(usage.get("cached_prompt_tokens", 0) or 0)
        reasoning_tokens = int(usage.get("reasoning_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)

        with self._lock:
            self._summary["llm_calls"] += 1
            self._summary["prompt_tokens"] += prompt_tokens
            self._summary["completion_tokens"] += completion_tokens
            self._summary["cached_prompt_tokens"] += cached_prompt_tokens
            self._summary["reasoning_tokens"] += reasoning_tokens
            self._summary["total_tokens"] += total_tokens
            if cost_usd is None:
                self._summary["unpriced_calls"] += 1
                if model_name not in self._summary["unpriced_models"]:
                    self._summary["unpriced_models"].append(model_name)
            else:
                self._summary["llm_cost_usd"] = round(float(self._summary["llm_cost_usd"]) + float(cost_usd), 6)

            model_summary = self._summary["models"].setdefault(
                model_name,
                {
                    "operation": operation,
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_prompt_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "priced": cost_usd is not None,
                },
            )
            model_summary["operation"] = operation
            model_summary["calls"] += 1
            model_summary["prompt_tokens"] += prompt_tokens
            model_summary["completion_tokens"] += completion_tokens
            model_summary["cached_prompt_tokens"] += cached_prompt_tokens
            model_summary["reasoning_tokens"] += reasoning_tokens
            model_summary["total_tokens"] += total_tokens
            model_summary["priced"] = model_summary.get("priced", False) or cost_usd is not None
            if cost_usd is not None:
                model_summary["cost_usd"] = round(float(model_summary["cost_usd"]) + float(cost_usd), 6)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._summary, sort_keys=True))


_TRACKER_LOCK = Lock()
_TRACKER_STACK: list[UsageTracker] = []

_MODEL_PRICING: dict[str, ModelPricing] = {
    "anthropic/claude-sonnet-4": ModelPricing(
        input_cost_per_million=3.0,
        output_cost_per_million=15.0,
        cached_input_cost_per_million=0.3,
    ),
    "openai/text-embedding-3-small": ModelPricing(input_cost_per_million=0.02),
    "workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast": ModelPricing(
        input_cost_per_million=0.293,
        output_cost_per_million=2.253,
    ),
    "workers-ai/@cf/baai/bge-m3": ModelPricing(input_cost_per_million=0.012),
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"

    total_seconds = max(0.0, float(seconds))
    if total_seconds < 1.0:
        return f"{total_seconds:.2f}s"
    if total_seconds < 60.0:
        return f"{total_seconds:.1f}s"

    rounded = int(math.ceil(total_seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def ensure_pipeline_runs_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id BIGSERIAL PRIMARY KEY,
                step TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                trigger_source TEXT NOT NULL DEFAULT 'manual',
                parent_run_id BIGINT,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                duration_seconds DOUBLE PRECISION,
                exit_code INT,
                llm_calls INT NOT NULL DEFAULT 0,
                llm_prompt_tokens BIGINT NOT NULL DEFAULT 0,
                llm_completion_tokens BIGINT NOT NULL DEFAULT 0,
                llm_cached_prompt_tokens BIGINT NOT NULL DEFAULT 0,
                llm_reasoning_tokens BIGINT NOT NULL DEFAULT 0,
                llm_total_tokens BIGINT NOT NULL DEFAULT 0,
                llm_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
                summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS llm_calls INT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS llm_prompt_tokens BIGINT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS llm_completion_tokens BIGINT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS llm_cached_prompt_tokens BIGINT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS llm_reasoning_tokens BIGINT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS llm_total_tokens BIGINT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS llm_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pipeline_runs_step_started_at
            ON pipeline_runs (step, started_at DESC, id DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pipeline_runs_parent_run_id
            ON pipeline_runs (parent_run_id, started_at DESC, id DESC)
            """
        )


def save_pipeline_state(conn, key: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_state (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = NOW()
            """,
            (key, str(value)),
        )


def _get_nested_value(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _extract_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    prompt_tokens = _int_value(_get_nested_value(usage, "prompt_tokens") or _get_nested_value(usage, "input_tokens"))
    completion_tokens = _int_value(
        _get_nested_value(usage, "completion_tokens") or _get_nested_value(usage, "output_tokens")
    )
    total_tokens = _int_value(_get_nested_value(usage, "total_tokens"))
    cached_prompt_tokens = _int_value(
        _get_nested_value(usage, "prompt_tokens_details", "cached_tokens")
        or _get_nested_value(usage, "input_tokens_details", "cached_tokens")
        or _get_nested_value(usage, "cache_read_input_tokens")
    )
    reasoning_tokens = _int_value(
        _get_nested_value(usage, "completion_tokens_details", "reasoning_tokens")
        or _get_nested_value(usage, "output_tokens_details", "reasoning_tokens")
        or _get_nested_value(usage, "reasoning_tokens")
    )
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _pricing_for_model(model_name: str) -> ModelPricing | None:
    normalized = str(model_name or "").strip()
    for prefix, pricing in sorted(_MODEL_PRICING.items(), key=lambda item: len(item[0]), reverse=True):
        if normalized.startswith(prefix):
            return pricing
    return None


def _estimate_cost_usd(model_name: str, usage: dict[str, int]) -> float | None:
    pricing = _pricing_for_model(model_name)
    if pricing is None:
        return None
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    cached_prompt_tokens = min(prompt_tokens, int(usage.get("cached_prompt_tokens", 0) or 0))
    uncached_prompt_tokens = max(0, prompt_tokens - cached_prompt_tokens)
    cached_rate = (
        pricing.cached_input_cost_per_million
        if pricing.cached_input_cost_per_million is not None
        else pricing.input_cost_per_million
    )
    return round(
        (uncached_prompt_tokens / 1_000_000.0) * pricing.input_cost_per_million
        + (cached_prompt_tokens / 1_000_000.0) * cached_rate
        + (completion_tokens / 1_000_000.0) * pricing.output_cost_per_million,
        6,
    )


@contextmanager
def llm_usage_tracking():
    tracker = UsageTracker()
    with _TRACKER_LOCK:
        _TRACKER_STACK.append(tracker)
    try:
        yield tracker
    finally:
        with _TRACKER_LOCK:
            if _TRACKER_STACK and _TRACKER_STACK[-1] is tracker:
                _TRACKER_STACK.pop()
            elif tracker in _TRACKER_STACK:
                _TRACKER_STACK.remove(tracker)


def record_llm_usage(response: Any, *, model_name: str, operation: str) -> dict[str, Any]:
    usage = _extract_usage(response)
    cost_usd = _estimate_cost_usd(model_name, usage)
    with _TRACKER_LOCK:
        tracker = _TRACKER_STACK[-1] if _TRACKER_STACK else None
    if tracker is not None:
        tracker.record(model=model_name, operation=operation, usage=usage, cost_usd=cost_usd)
    return {
        **usage,
        "llm_cost_usd": cost_usd,
    }


def summarize_llm_usage(summary: dict | None) -> dict[str, Any]:
    payload = summary if isinstance(summary, dict) else {}
    llm_usage = payload.get("llm_usage") if isinstance(payload.get("llm_usage"), dict) else payload
    return {
        "llm_calls": _int_value(llm_usage.get("llm_calls")),
        "llm_prompt_tokens": _int_value(llm_usage.get("prompt_tokens")),
        "llm_completion_tokens": _int_value(llm_usage.get("completion_tokens")),
        "llm_cached_prompt_tokens": _int_value(llm_usage.get("cached_prompt_tokens")),
        "llm_reasoning_tokens": _int_value(llm_usage.get("reasoning_tokens")),
        "llm_total_tokens": _int_value(llm_usage.get("total_tokens")),
        "llm_cost_usd": round(float(llm_usage.get("llm_cost_usd", 0.0) or 0.0), 6),
    }


def start_run(
    conn,
    *,
    step: str,
    trigger_source: str,
    parent_run_id: int | None = None,
    started_at: datetime | None = None,
) -> RunHandle:
    ensure_pipeline_runs_table(conn)
    started_at = started_at or utc_now()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_runs (
                step,
                status,
                trigger_source,
                parent_run_id,
                started_at
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (step, "running", trigger_source, parent_run_id, started_at),
        )
        run_id = int(cur.fetchone()[0])

    save_pipeline_state(conn, f"last_{step}_run_id", str(run_id))
    save_pipeline_state(conn, f"last_{step}_run_started_at", started_at.isoformat())
    save_pipeline_state(conn, f"last_{step}_run_finished_at", "")
    save_pipeline_state(conn, f"last_{step}_run_duration_seconds", "")
    save_pipeline_state(conn, f"last_{step}_run_duration_human", "")
    save_pipeline_state(conn, f"last_{step}_run_status", "running")
    save_pipeline_state(conn, f"last_{step}_run_exit_code", "")
    save_pipeline_state(conn, f"last_{step}_run_llm_calls", "0")
    save_pipeline_state(conn, f"last_{step}_run_llm_prompt_tokens", "0")
    save_pipeline_state(conn, f"last_{step}_run_llm_completion_tokens", "0")
    save_pipeline_state(conn, f"last_{step}_run_llm_cached_prompt_tokens", "0")
    save_pipeline_state(conn, f"last_{step}_run_llm_reasoning_tokens", "0")
    save_pipeline_state(conn, f"last_{step}_run_llm_total_tokens", "0")
    save_pipeline_state(conn, f"last_{step}_run_llm_cost_usd", "0.000000")
    save_pipeline_state(conn, f"last_{step}_run_trigger", trigger_source)
    save_pipeline_state(conn, f"last_{step}_run_parent_id", "" if parent_run_id is None else str(parent_run_id))
    return RunHandle(
        run_id=run_id,
        step=step,
        started_at=started_at,
        trigger_source=trigger_source,
        parent_run_id=parent_run_id,
    )


def finish_run(
    conn,
    *,
    run: RunHandle,
    status: str,
    finished_at: datetime | None = None,
    exit_code: int | None = None,
    summary: dict | None = None,
) -> float:
    ensure_pipeline_runs_table(conn)
    finished_at = finished_at or utc_now()
    duration_seconds = max(0.0, (finished_at - run.started_at).total_seconds())
    llm_usage = summarize_llm_usage(summary)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pipeline_runs
            SET status = %s,
                finished_at = %s,
                duration_seconds = %s,
                exit_code = %s,
                llm_calls = %s,
                llm_prompt_tokens = %s,
                llm_completion_tokens = %s,
                llm_cached_prompt_tokens = %s,
                llm_reasoning_tokens = %s,
                llm_total_tokens = %s,
                llm_cost_usd = %s,
                summary = COALESCE(%s::jsonb, '{}'::jsonb)
            WHERE id = %s
            """,
            (
                status,
                finished_at,
                duration_seconds,
                exit_code,
                llm_usage["llm_calls"],
                llm_usage["llm_prompt_tokens"],
                llm_usage["llm_completion_tokens"],
                llm_usage["llm_cached_prompt_tokens"],
                llm_usage["llm_reasoning_tokens"],
                llm_usage["llm_total_tokens"],
                llm_usage["llm_cost_usd"],
                json.dumps(summary or {}, sort_keys=True),
                run.run_id,
            ),
        )

    save_pipeline_state(conn, f"last_{run.step}_run_finished_at", finished_at.isoformat())
    save_pipeline_state(conn, f"last_{run.step}_run_duration_seconds", f"{duration_seconds:.3f}")
    save_pipeline_state(conn, f"last_{run.step}_run_duration_human", format_duration(duration_seconds))
    save_pipeline_state(conn, f"last_{run.step}_run_status", status)
    save_pipeline_state(conn, f"last_{run.step}_run_exit_code", "" if exit_code is None else str(exit_code))
    save_pipeline_state(conn, f"last_{run.step}_run_llm_calls", str(llm_usage["llm_calls"]))
    save_pipeline_state(conn, f"last_{run.step}_run_llm_prompt_tokens", str(llm_usage["llm_prompt_tokens"]))
    save_pipeline_state(conn, f"last_{run.step}_run_llm_completion_tokens", str(llm_usage["llm_completion_tokens"]))
    save_pipeline_state(conn, f"last_{run.step}_run_llm_cached_prompt_tokens", str(llm_usage["llm_cached_prompt_tokens"]))
    save_pipeline_state(conn, f"last_{run.step}_run_llm_reasoning_tokens", str(llm_usage["llm_reasoning_tokens"]))
    save_pipeline_state(conn, f"last_{run.step}_run_llm_total_tokens", str(llm_usage["llm_total_tokens"]))
    save_pipeline_state(conn, f"last_{run.step}_run_llm_cost_usd", f"{llm_usage['llm_cost_usd']:.6f}")
    return duration_seconds
