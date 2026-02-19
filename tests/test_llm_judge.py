import json
from types import SimpleNamespace

from src.config import Settings
from src.verification.llm_judge import run_llm_judge


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


def test_run_llm_judge_parses_scores_and_average(monkeypatch) -> None:
    payload = json.dumps(
        {
            "factual_accuracy": 0.8,
            "citation_accuracy": 0.7,
            "completeness": 0.9,
            "source_quality": 0.6,
            "source_diversity": 0.5,
            "overall_pass": True,
        }
    )

    class _Messages:
        def create(self, **_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=payload)])

    class _AnthropicClient:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr("src.verification.llm_judge.Anthropic", _AnthropicClient)

    result = run_llm_judge("report", {10: "chunk"}, _settings())

    assert result.source_diversity == 0.5
    assert result.average_score() == (0.8 + 0.7 + 0.9 + 0.6 + 0.5) / 5


def test_run_llm_judge_returns_sentinel_on_malformed_json(monkeypatch) -> None:
    class _Messages:
        def create(self, **_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="not-json")])

    class _AnthropicClient:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr("src.verification.llm_judge.Anthropic", _AnthropicClient)

    result = run_llm_judge("report", {10: "chunk"}, _settings())

    assert result.overall_pass is False
    assert result.average_score() == 0.0
