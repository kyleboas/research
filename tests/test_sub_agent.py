from types import SimpleNamespace

import pytest

from src.config import Settings
from src.generation.lead_agent import TaskDescription
from src.generation.sub_agent import run_parallel_subagents, run_subagent
from src.processing.retrieval import RetrievedChunk


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _PsycopgModule:
    def connect(self, _dsn):
        return _Connection()


class _Embeddings:
    def create(self, **_kwargs):
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])


class _OpenAIClient:
    def __init__(self, *args, **kwargs):
        self.embeddings = _Embeddings()


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


def _task() -> TaskDescription:
    return TaskDescription(
        angle="Regulation",
        angle_slug="regulation",
        objective="Track global AI policy moves",
        output_format="bullets",
        search_guidance="AI policy updates",
        task_boundaries="No implementation details",
    )


def test_run_subagent_iterative_loop_and_trajectory(monkeypatch) -> None:
    searches = []

    def _fake_hybrid_search(_conn, *, query_text, query_embedding, top_k):
        searches.append(query_text)
        return [
            RetrievedChunk(
                chunk_id=100 + len(searches),
                source_id=1,
                chunk_index=0,
                content=f"content for {query_text}",
                source_type="rss",
                source_key="k",
                source_title="t",
                source_metadata={},
                chunk_metadata={},
                combined_score=0.9,
                text_rank=1,
                vector_rank=1,
            )
        ]

    evals = [
        {"sufficient": False, "gaps": ["need regulatory timeline"], "next_query": "AI policy timeline"},
        {"sufficient": True, "gaps": [], "next_query": None},
    ]

    monkeypatch.setitem(__import__("sys").modules, "psycopg", _PsycopgModule())
    monkeypatch.setattr("src.generation.sub_agent.OpenAI", _OpenAIClient)
    monkeypatch.setattr("src.generation.sub_agent.hybrid_search", _fake_hybrid_search)
    monkeypatch.setattr("src.generation.sub_agent._evaluate_search_results", lambda *args, **kwargs: evals.pop(0))
    monkeypatch.setattr("src.generation.sub_agent.build_subagent_prompt", lambda **kwargs: ("sys", "user"))

    class _Messages:
        def create(self, **_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="Summary [S1:C101]")])

    class _AnthropicClient:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr("src.generation.sub_agent.Anthropic", _AnthropicClient)

    result = run_subagent(_task(), "postgresql://example", _settings(), max_search_rounds=3)

    assert result.error is None
    assert result.total_rounds == 2
    assert len(result.search_trajectory) == 2
    assert searches == ["AI policy updates", "AI policy timeline"]
    assert result.summary


def test_run_subagent_returns_sentinel_on_failure(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "psycopg", _PsycopgModule())
    monkeypatch.setattr("src.generation.sub_agent._run_search_round", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    result = run_subagent(_task(), "postgresql://example", _settings())

    assert result.error is not None
    assert result.summary == ""
    assert result.search_trajectory == []


def test_run_parallel_subagents_raises_when_all_fail(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.generation.sub_agent.run_subagent",
        lambda task, postgres_dsn, settings, max_search_rounds: SimpleNamespace(error="failed", angle_slug=task.angle_slug),
    )

    with pytest.raises(RuntimeError):
        run_parallel_subagents([_task()], "postgresql://example", _settings())
