import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from runtime_logging import format_duration, llm_usage_tracking, record_llm_usage, start_run, summarize_llm_usage


class FakeResponse:
    def __init__(self, usage):
        self.usage = usage


class FakeUsage:
    def __init__(
        self,
        *,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
    ):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.prompt_tokens_details = type("PromptDetails", (), {"cached_tokens": cached_tokens})()
        self.completion_tokens_details = type("CompletionDetails", (), {"reasoning_tokens": reasoning_tokens})()


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))

    def fetchone(self):
        return (42,)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()

    def cursor(self):
        return self.cursor_obj


class RuntimeLoggingTests(unittest.TestCase):
    def test_format_duration_handles_short_and_long_runs(self):
        self.assertEqual(format_duration(0.245), "0.24s")
        self.assertEqual(format_duration(12.0), "12.0s")
        self.assertEqual(format_duration(138.4), "2m 19s")
        self.assertEqual(format_duration(3671.0), "1h 1m 11s")

    def test_record_llm_usage_tracks_priced_models(self):
        response = FakeResponse(
            FakeUsage(
                prompt_tokens=1200,
                completion_tokens=300,
                total_tokens=1500,
                cached_tokens=200,
                reasoning_tokens=50,
            )
        )

        with llm_usage_tracking() as tracker:
            record_llm_usage(response, model_name="anthropic/claude-sonnet-4-6", operation="chat")
            summary = tracker.summary()

        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["prompt_tokens"], 1200)
        self.assertEqual(summary["completion_tokens"], 300)
        self.assertEqual(summary["cached_prompt_tokens"], 200)
        self.assertEqual(summary["reasoning_tokens"], 50)
        self.assertEqual(summary["total_tokens"], 1500)
        self.assertAlmostEqual(summary["llm_cost_usd"], 0.00756, places=6)
        self.assertEqual(summary["unpriced_calls"], 0)

    def test_record_llm_usage_marks_unpriced_models(self):
        response = FakeResponse(FakeUsage(prompt_tokens=500, completion_tokens=100, total_tokens=600))

        with llm_usage_tracking() as tracker:
            record_llm_usage(response, model_name="google/gemini-2.5-flash", operation="chat")
            summary = tracker.summary()

        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["llm_cost_usd"], 0.0)
        self.assertEqual(summary["unpriced_calls"], 1)
        self.assertEqual(summary["unpriced_models"], ["google/gemini-2.5-flash"])

    def test_summarize_llm_usage_reads_nested_payload(self):
        summary = summarize_llm_usage(
            {
                "llm_usage": {
                    "llm_calls": 2,
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "cached_prompt_tokens": 10,
                    "reasoning_tokens": 5,
                    "total_tokens": 140,
                    "llm_cost_usd": 0.012345,
                }
            }
        )

        self.assertEqual(summary["llm_calls"], 2)
        self.assertEqual(summary["llm_prompt_tokens"], 100)
        self.assertEqual(summary["llm_completion_tokens"], 40)
        self.assertEqual(summary["llm_cached_prompt_tokens"], 10)
        self.assertEqual(summary["llm_reasoning_tokens"], 5)
        self.assertEqual(summary["llm_total_tokens"], 140)
        self.assertEqual(summary["llm_cost_usd"], 0.012345)

    def test_start_run_clears_stale_run_metadata(self):
        conn = FakeConn()
        saved = {}

        def capture_state(_conn, key, value):
            saved[key] = value

        with patch("runtime_logging.save_pipeline_state", side_effect=capture_state):
            handle = start_run(
                conn,
                step="ingest",
                trigger_source="cli",
                started_at=datetime(2026, 3, 13, 12, 0, tzinfo=UTC),
            )

        self.assertEqual(handle.run_id, 42)
        self.assertEqual(saved["last_ingest_run_status"], "running")
        self.assertEqual(saved["last_ingest_run_finished_at"], "")
        self.assertEqual(saved["last_ingest_run_duration_seconds"], "")
        self.assertEqual(saved["last_ingest_run_duration_human"], "")
        self.assertEqual(saved["last_ingest_run_exit_code"], "")
        self.assertEqual(saved["last_ingest_run_llm_calls"], "0")
        self.assertEqual(saved["last_ingest_run_llm_cost_usd"], "0.000000")


if __name__ == "__main__":
    unittest.main()
