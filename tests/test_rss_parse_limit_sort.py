from src.ingestion.rss import parse_feed


def test_parse_feed_sorts_before_applying_latest_limit():
    xml = b"""
    <rss><channel>
      <item><title>Z title</title><link>https://example.com/z</link></item>
      <item><title>A title</title><link>https://example.com/a</link></item>
      <item><title>M title</title><link>https://example.com/m</link></item>
    </channel></rss>
    """

    records = parse_feed(xml, feed_name="Example", latest_limit=2)

    assert [record.title for record in records] == ["A title", "M title"]
