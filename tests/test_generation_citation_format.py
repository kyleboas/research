import re
from types import SimpleNamespace

from src.config import Settings
from src.generation.revision_pass import run_revision_pass
from src.generation.sub_agent import SubAgentResult
from src.generation.synthesis_pass import run_synthesis_pass


class _FakeMessages:
    def __init__(self, text: str):
        self._text = text

    def create(self, **_kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)])


class _FakeAnthropic:
    def __init__(self, *, api_key: str):
        self.api_key = api_key
        self.messages = _FakeMessages(
            "## Key Findings\nA grounded claim backed by evidence [S1:C10]."
        )


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


def test_synthesis_and_final_outputs_include_required_citation_markers(monkeypatch) -> None:
    monkeypatch.setattr("src.generation.synthesis_pass.Anthropic", _FakeAnthropic)
    monkeypatch.setattr("src.generation.revision_pass.Anthropic", _FakeAnthropic)

    subagent_results = [
        SubAgentResult(
            angle="Angle",
            angle_slug="angle",
            chunks=[{"source_id": 1, "chunk_id": 10, "text": "evidence", "combined_score": 1.0}],
            summary="Summary [S1:C10]",
            citations=["[S1:C10]"],
            search_trajectory=[],
            total_rounds=1,
            elapsed_s=0.1,
            input_tokens=1,
            output_tokens=1,
            error=None,
        )
    ]
    citation_pattern = re.compile(r"\[S\d+:C\d+\]")

    draft = run_synthesis_pass(topic="topic", subagent_results=subagent_results, settings=_settings())
    final = run_revision_pass(
        topic="topic",
        context_packet=SimpleNamespace(to_json=lambda: "{}"),
        draft_markdown=draft,
        critique_markdown="keep citations",
        settings=_settings(),
    )

    assert citation_pattern.search(draft)
    assert citation_pattern.search(final)
