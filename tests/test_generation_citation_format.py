import re
from types import SimpleNamespace

from src.config import Settings
from src.generation.draft_pass import run_draft_pass
from src.generation.research_pass import ContextPacket
from src.generation.revision_pass import run_revision_pass


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
        anthropic_small_model_id="small-model",
        openai_api_key="openai-key",
        openai_embedding_model="embed",
        transcript_api_key="transcript-key",
        github_token="gh",
        github_owner="owner",
        github_repo="repo",
        github_default_branch="main",
    )


def test_draft_and_final_outputs_include_required_citation_markers(monkeypatch) -> None:
    monkeypatch.setattr("src.generation.draft_pass.Anthropic", _FakeAnthropic)
    monkeypatch.setattr("src.generation.revision_pass.Anthropic", _FakeAnthropic)

    context_packet = ContextPacket(topic="topic", queries=[], chunks=[])
    citation_pattern = re.compile(r"\[S\d+:C\d+\]")

    draft = run_draft_pass(topic="topic", context_packet=context_packet, settings=_settings())
    final = run_revision_pass(
        topic="topic",
        context_packet=context_packet,
        draft_markdown=draft,
        critique_markdown="keep citations",
        settings=_settings(),
    )

    assert citation_pattern.search(draft)
    assert citation_pattern.search(final)
