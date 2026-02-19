import json
from types import SimpleNamespace

from src.config import Settings
from src.generation.lead_agent import TaskDescription, _cosine_similarity, run_lead_agent
from src.generation.sub_agent import SearchRound, SubAgentResult, run_parallel_subagents


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


def test_parallel_generation_simulation(monkeypatch) -> None:
    lead_payload = json.dumps(
        {
            "complexity": "moderate",
            "reasoning": "Two independent angles.",
            "task_descriptions": [
                {
                    "angle": "Regulation",
                    "angle_slug": "regulation",
                    "objective": "Map policy changes by jurisdiction",
                    "output_format": "bullets",
                    "search_guidance": "AI regulation by country",
                    "task_boundaries": "Exclude technical evals",
                },
                {
                    "angle": "Adoption",
                    "angle_slug": "adoption",
                    "objective": "Measure enterprise deployment trends",
                    "output_format": "narrative",
                    "search_guidance": "enterprise AI adoption survey",
                    "task_boundaries": "Exclude policy analysis",
                },
            ],
        }
    )

    class _LeadMessages:
        def create(self, **_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=lead_payload)])

    class _AnthropicClient:
        def __init__(self, *args, **kwargs):
            self.messages = _LeadMessages()

    class _Embeddings:
        def create(self, model, input):
            if isinstance(input, list) and len(input) == 2:
                return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 0.0]), SimpleNamespace(embedding=[0.0, 1.0])])
            # objective similarity check
            return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 0.0]), SimpleNamespace(embedding=[0.0, 1.0])])

    class _OpenAIClient:
        def __init__(self, *args, **kwargs):
            self.embeddings = _Embeddings()

    def _fake_run_subagent(task: TaskDescription, postgres_dsn: str, settings: Settings, max_search_rounds: int):
        return SubAgentResult(
            angle=task.angle,
            angle_slug=task.angle_slug,
            chunks=[{"chunk_id": 1, "source_id": 1}],
            summary="summary [S1:C1]",
            citations=["[S1:C1]"],
            search_trajectory=[
                SearchRound(
                    round_number=1,
                    query=task.search_guidance,
                    chunks_retrieved=[{"chunk_id": 1}],
                    chunk_count=1,
                    evaluation={"sufficient": True, "gaps": [], "next_query": None},
                )
            ],
            total_rounds=1,
            elapsed_s=0.1,
            input_tokens=10,
            output_tokens=10,
            error=None,
        )

    monkeypatch.setattr("src.generation.lead_agent.Anthropic", _AnthropicClient)
    monkeypatch.setattr("src.generation.lead_agent.OpenAI", _OpenAIClient)
    monkeypatch.setattr("src.generation.sub_agent.run_subagent", _fake_run_subagent)

    settings = _settings()
    lead = run_lead_agent("AI policy and adoption in enterprises", settings)
    results = run_parallel_subagents(lead.task_descriptions, "postgresql://example", settings)

    embed_client = _OpenAIClient()
    objective_embeddings = [
        list(row.embedding)
        for row in embed_client.embeddings.create(model=settings.openai_embedding_model, input=[t.objective for t in lead.task_descriptions]).data
    ]
    similarity = _cosine_similarity(objective_embeddings[0], objective_embeddings[1])

    assert similarity <= 0.85
    assert all(result.search_trajectory for result in results)
