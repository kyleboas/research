from src.pipeline import _persist_stage_cost_metrics


class _UndefinedColumnError(Exception):
    sqlstate = "42703"


class _Cursor:
    def __init__(self, scripted_fetches, *, fail_run_name_select: bool = False, fail_run_name_insert: bool = False):
        self._scripted_fetches = list(scripted_fetches)
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self.fail_run_name_select = fail_run_name_select
        self.fail_run_name_insert = fail_run_name_insert

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))
        if self.fail_run_name_select and "WHERE run_name = %s" in normalized:
            self.fail_run_name_select = False
            raise _UndefinedColumnError("column run_name does not exist")
        if self.fail_run_name_insert and "INSERT INTO pipeline_runs (run_name" in normalized:
            self.fail_run_name_insert = False
            raise _UndefinedColumnError("column run_name does not exist")

    def fetchone(self):
        if not self._scripted_fetches:
            return None
        return self._scripted_fetches.pop(0)


class _Connection:
    def __init__(self, scripted_fetches, **cursor_flags):
        self.cursor_obj = _Cursor(scripted_fetches, **cursor_flags)

    def cursor(self):
        return self.cursor_obj


def test_persist_stage_cost_metrics_updates_without_metadata_column() -> None:
    connection = _Connection(
        [
            (True, False, True),  # has_run_name, has_metadata, has_cost_estimate_json
            (11, {"stages": {}}),  # existing pipeline_runs row
        ]
    )

    _persist_stage_cost_metrics(
        connection,
        pipeline_run_id="run-123",
        stage="ingestion",
        metrics={"token_count": 0, "estimated_cost_usd": 0.0},
    )

    select_query, _ = connection.cursor_obj.executed[1]
    assert "SELECT id, cost_estimate_json FROM pipeline_runs" in select_query
    assert "WHERE run_name = %s" in select_query

    update_query, update_params = connection.cursor_obj.executed[-1]
    assert "SET cost_estimate_json = %s::jsonb, status = %s" in update_query
    assert "metadata = %s::jsonb" not in update_query
    assert update_params is not None
    assert update_params[-1] == 11


def test_persist_stage_cost_metrics_inserts_without_metadata_or_cost_columns() -> None:
    connection = _Connection(
        [
            (True, False, False),  # has_run_name, has_metadata, has_cost_estimate_json
            None,  # no existing pipeline_runs row
            (42,),  # RETURNING id for inserted row
        ]
    )

    _persist_stage_cost_metrics(
        connection,
        pipeline_run_id="run-legacy",
        stage="ingestion",
        metrics={"token_count": 0, "estimated_cost_usd": 0.0},
    )

    insert_query, _ = connection.cursor_obj.executed[2]
    assert "INSERT INTO pipeline_runs (run_name, status)" in insert_query

    update_query, _ = connection.cursor_obj.executed[-1]
    assert "SET status = %s" in update_query
    assert "metadata" not in update_query
    assert "cost_estimate_json" not in update_query


def test_persist_stage_cost_metrics_uses_metadata_when_run_name_missing() -> None:
    connection = _Connection(
        [
            (False, True, True),  # has_run_name, has_metadata, has_cost_estimate_json
            (101, {"pipeline_run_id": "run-xyz"}, {"stages": {}}),
        ]
    )

    _persist_stage_cost_metrics(
        connection,
        pipeline_run_id="run-xyz",
        stage="ingestion",
        metrics={"token_count": 0, "estimated_cost_usd": 0.0},
    )

    select_query, select_params = connection.cursor_obj.executed[1]
    assert "WHERE metadata ->> 'pipeline_run_id' = %s" in select_query
    assert select_params == ("run-xyz",)


def test_persist_stage_cost_metrics_inserts_without_run_name_column() -> None:
    connection = _Connection(
        [
            (False, True, False),  # has_run_name, has_metadata, has_cost_estimate_json
            None,
            (77,),
        ]
    )

    _persist_stage_cost_metrics(
        connection,
        pipeline_run_id="run-no-name",
        stage="ingestion",
        metrics={"token_count": 0, "estimated_cost_usd": 0.0},
    )

    insert_query, insert_params = connection.cursor_obj.executed[2]
    assert "INSERT INTO pipeline_runs (status, metadata)" in insert_query
    assert insert_params is not None
    assert insert_params[0] == "running"


def test_persist_stage_cost_metrics_falls_back_when_run_name_query_fails() -> None:
    connection = _Connection(
        [
            (True, True, True),
            (9, {"pipeline_run_id": "run-9"}, {"stages": {}}),
        ],
        fail_run_name_select=True,
    )

    _persist_stage_cost_metrics(
        connection,
        pipeline_run_id="run-9",
        stage="ingestion",
        metrics={"token_count": 0, "estimated_cost_usd": 0.0},
    )

    queries = [query for query, _ in connection.cursor_obj.executed]
    assert any("WHERE run_name = %s" in query for query in queries)
    assert any("WHERE metadata ->> 'pipeline_run_id' = %s" in query for query in queries)


def test_persist_stage_cost_metrics_retries_insert_without_run_name() -> None:
    connection = _Connection(
        [
            (True, True, False),
            None,
            (303,),
        ],
        fail_run_name_insert=True,
    )

    _persist_stage_cost_metrics(
        connection,
        pipeline_run_id="run-insert",
        stage="ingestion",
        metrics={"token_count": 0, "estimated_cost_usd": 0.0},
    )

    queries = [query for query, _ in connection.cursor_obj.executed]
    assert any("INSERT INTO pipeline_runs (run_name, status, metadata)" in query for query in queries)
    assert any("INSERT INTO pipeline_runs (status, metadata)" in query for query in queries)
