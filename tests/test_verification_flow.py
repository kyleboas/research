import json
import sys
from types import SimpleNamespace

from src.config import Settings
from src.pipeline import run_verification


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self._last_query = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self._last_query = " ".join(query.split())

        if "FROM reports" in self._last_query:
            self.connection._selected_run = params[0]
        elif self._last_query.startswith("DELETE FROM claims"):
            self.connection.claim_rows = []
        elif self._last_query.startswith("UPDATE reports"):
            self.connection.updated_verification = json.loads(params[0])

    def executemany(self, _query, rows):
        self.connection.claim_rows.extend(rows)

    def fetchone(self):
        if self.connection._selected_run == "run-1":
            return (
                77,
                "Supported claim with overlap tokens [S1:C10].\nUnsupported claim text [S1:C20].",
            )
        return None

    def fetchall(self):
        if "FROM chunks" in self._last_query:
            return [
                (10, 1, "supported claim with overlap tokens and evidence"),
                (20, 1, "completely unrelated material"),
            ]
        return []


class _Connection:
    def __init__(self):
        self._selected_run = None
        self.claim_rows = []
        self.updated_verification = None
        self.commit_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commit_count += 1


class _PsycopgModule:
    def __init__(self, connection):
        self._connection = connection

    def connect(self, _dsn):
        return self._connection


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


def test_verification_persists_supported_and_unsupported_claims_with_scores(monkeypatch) -> None:
    fake_connection = _Connection()
    monkeypatch.setattr("src.pipeline.load_settings", _settings)
    monkeypatch.setitem(sys.modules, "psycopg", _PsycopgModule(fake_connection))

    run_verification(pipeline_run_id="run-1")

    assert fake_connection.commit_count == 1
    assert len(fake_connection.claim_rows) == 2

    statuses = [json.loads(row[5])["verification_status"] for row in fake_connection.claim_rows]
    scores = [json.loads(row[5])["verification_score"] for row in fake_connection.claim_rows]

    assert "supported" in statuses
    assert "unsupported" in statuses
    assert all(isinstance(score, float) for score in scores)
    assert fake_connection.updated_verification["supported_claims"] == 1
    assert fake_connection.updated_verification["unsupported_claims"] == 1
