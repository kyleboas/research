from src.processing.chunking import chunk_text


def test_chunk_text_is_deterministic_for_boundaries_and_indices() -> None:
    text = " ".join(f"token-{i}" for i in range(1, 121))

    first = chunk_text(text, window_size=25, overlap=5)
    second = chunk_text(text, window_size=25, overlap=5)

    assert [chunk.chunk_index for chunk in first] == [chunk.chunk_index for chunk in second]
    assert [chunk.content for chunk in first] == [chunk.content for chunk in second]
    assert [chunk.token_count for chunk in first] == [chunk.token_count for chunk in second]

    # Sanity check that boundaries are what we expect for this window/overlap setup.
    assert first[0].content.startswith("token-1")
    assert first[1].content.startswith("token-21")
    assert first[-1].content.endswith("token-120")
