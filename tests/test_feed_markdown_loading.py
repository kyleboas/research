from src.ingestion.rss import load_feed_configs_from_env
from src.ingestion.youtube import load_youtube_channel_configs_from_env


def test_load_rss_configs_from_markdown_when_env_missing(monkeypatch):
    monkeypatch.delenv("RSS_FEEDS", raising=False)
    configs = load_feed_configs_from_env()

    assert len(configs) > 0
    assert any(config.name == "Opta Analyst" for config in configs)
    assert any(config.url == "https://theanalyst.com/feed" for config in configs)


def test_load_youtube_configs_from_markdown_when_env_missing(monkeypatch):
    monkeypatch.delenv("YOUTUBE_CHANNELS", raising=False)
    configs = load_youtube_channel_configs_from_env()

    assert len(configs) > 0
    assert any(config.name == "BeanymanSports" for config in configs)
    assert any(config.channel_id == "UCiVg6vRhuyjsWgHkDNOig6A" for config in configs)


def test_env_overrides_markdown_for_feed_sources(monkeypatch):
    monkeypatch.setenv("RSS_FEEDS", "Example|https://example.com/feed")
    monkeypatch.setenv("YOUTUBE_CHANNELS", "Example Channel|abc123")

    rss_configs = load_feed_configs_from_env()
    youtube_configs = load_youtube_channel_configs_from_env()

    assert [(config.name, config.url) for config in rss_configs] == [
        ("Example", "https://example.com/feed")
    ]
    assert [(config.name, config.channel_id) for config in youtube_configs] == [
        ("Example Channel", "abc123")
    ]
