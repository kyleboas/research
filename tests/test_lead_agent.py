import json
from types import SimpleNamespace

from src.config import Settings
from src.generation.lead_agent import _heuristic_complexity, run_lead_agent


class _FakeMessages:
    def __init__(self, payload: str):
        self._payload = payload

    def create(self, **_kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._payload)])


class _FakeAnthropic:
    def __init__(self, payload: str):
        self.messages = _FakeMessages(payload)


def _settings() -> Settings:
    return Settings(
        supabase_url="x",
        supabase_service_role_key="x",
        postgres_dsn="postgresql://example",
        anthropic_api_key="anthropic-key",
        anthropic_model_id="model",
        anthropic_lead_model_id="lead-model",
        anthropic_small_model_id="small-model",
        anthropic_trend_model_id="trend-model",
        openai_api_key="openai-key",
        openai_embedding_model="embed",
        transcript_api_key="transcript-key",
        github_token="gh",
        github_owner="owner",
        github_repo="repo",
        github_default_branch="main",
    )


def test_heuristic_complexity_tiers() -> None:
    assert _heuristic_complexity("AI") == "simple"
    assert _heuristic_complexity("AI policy market dynamics trends") == "moderate"
    assert _heuristic_complexity("AI regulation and market adoption across healthcare and finance") == "complex"


def test_run_lead_agent_parses_response_and_task_boundaries(monkeypatch) -> None:
    payload = json.dumps(
        {
            "complexity": "moderate",
            "reasoning": "Needs multiple angles but bounded scope.",
            "task_descriptions": [
                {
                    "angle": "Regulation",
                    "angle_slug": "regulation",
                    "objective": "Map major regulatory initiatives.",
                    "output_format": "Bulleted notes",
                    "search_guidance": "Start broad with AI regulation 2024.",
                    "task_boundaries": "Exclude model internals.",
                },
                {
                    "angle": "Market impact",
                    "angle_slug": "market-impact",
                    "objective": "Assess enterprise adoption trends.",
                    "output_format": "Short narrative",
                    "search_guidance": "Look for enterprise AI surveys.",
                    "task_boundaries": "Exclude consumer applications.",
                },
            ],
        }
    )

    monkeypatch.setattr(
        "src.generation.lead_agent.Anthropic",
        lambda api_key: _FakeAnthropic(payload),
    )
    monkeypatch.setattr("src.generation.lead_agent._drop_duplicate_tasks", lambda tasks, settings: tasks)

    result = run_lead_agent("AI regulation and adoption", _settings())

    assert result.complexity == "moderate"
    assert result.subagent_count == 2
    assert all(task.task_boundaries for task in result.task_descriptions)


def test_run_lead_agent_drops_overlapping_tasks(monkeypatch) -> None:
    payload = json.dumps(
        {
            "complexity": "moderate",
            "reasoning": "Two tasks overlap heavily; one should be removed.",
            "task_descriptions": [
                {
                    "angle": "Regulation",
                    "angle_slug": "regulation",
                    "objective": "Assess global AI regulation updates.",
                    "output_format": "Bulleted notes",
                    "search_guidance": "Search AI regulation updates.",
                    "task_boundaries": "Exclude product benchmarks.",
                },
                {
                    "angle": "Policy overlap",
                    "angle_slug": "policy-overlap",
                    "objective": "Assess global AI regulation updates.",
                    "output_format": "Bulleted notes",
                    "search_guidance": "Search AI regulation changes.",
                    "task_boundaries": "Exclude product benchmarks.",
                },
            ],
        }
    )

    class _Embeddings:
        def create(self, **_kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[1.0, 0.0]),
                    SimpleNamespace(embedding=[1.0, 0.0]),
                ]
            )

    class _OpenAIClient:
        def __init__(self, *args, **kwargs):
            self.embeddings = _Embeddings()

    monkeypatch.setattr(
        "src.generation.lead_agent.Anthropic",
        lambda api_key: _FakeAnthropic(payload),
    )
    monkeypatch.setattr("src.generation.lead_agent.OpenAI", _OpenAIClient)

    result = run_lead_agent("AI policy", _settings())

    assert result.subagent_count == 1
    assert [task.angle_slug for task in result.task_descriptions] == ["regulation"]
