from types import SimpleNamespace

from src.config import Settings
from src.generation.sub_agent import SubAgentResult
from src.generation.synthesis_pass import run_synthesis_pass


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


def _result(angle: str, slug: str, chunk_id: int, *, error: str | None = None) -> SubAgentResult:
    return SubAgentResult(
        angle=angle,
        angle_slug=slug,
        chunks=[] if error else [{"chunk_id": chunk_id, "source_id": 1, "combined_score": 0.9, "text": "x"}],
        summary="summary [S1:C10]" if error is None else "",
        citations=["[S1:C10]"] if error is None else [],
        search_trajectory=[],
        total_rounds=1 if error is None else 0,
        elapsed_s=0.1,
        input_tokens=10,
        output_tokens=10,
        error=error,
    )


def test_synthesis_deduplicates_chunks_and_mentions_failed_angles(monkeypatch) -> None:
    captured = {}

    def _fake_prompt(**kwargs):
        captured.update(kwargs)
        return "sys", "user"

    class _Messages:
        def create(self, **_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="# Report\n\nClaim [S1:C10]")])

    class _AnthropicClient:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr("src.generation.synthesis_pass.build_synthesis_prompt", _fake_prompt)
    monkeypatch.setattr("src.generation.synthesis_pass.Anthropic", _AnthropicClient)

    markdown = run_synthesis_pass(
        "Topic",
        [
            _result("Regulation", "regulation", 10),
            _result("Economics", "economics", 10),
            _result("Background", "background", 20, error="failed"),
        ],
        _settings(),
    )

    assert "[S1:C10]" in markdown
    assert "Background" in markdown
    assert captured["chunks_json"].count('"chunk_id": 10') == 1
