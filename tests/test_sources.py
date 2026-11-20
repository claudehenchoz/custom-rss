from __future__ import annotations

import threading

import pytest

from custom_rss.models import Source
from custom_rss.sources import fetch_feed, fetch_feeds

RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>Publisher</title><link>https://publisher.example/</link><description>Feed</description>
<item><title>One</title><link>https://publisher.example/one</link></item>
</channel></rss>
"""


class FakeHttp:
    def __init__(self, mapping: dict[str, bytes | Exception]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def get_bytes(self, url: str, *, accept: str) -> bytes:
        self.calls.append(url)
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        return value


def test_fetch_feed_rejects_malformed_payload() -> None:
    source = Source(name="Broken", url="https://broken.example/rss")
    http = FakeHttp({source.url: b"<html><body>not a feed</body></html>"})

    with pytest.raises(ValueError, match="Broken"):
        fetch_feed(source, http)


def rss(title: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<rss version="2.0"><channel><title>{title}</title>'
        "<link>https://publisher.example/</link><description>Feed</description>"
        "</channel></rss>"
    ).encode()


def test_sources_are_fetched_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    barrier = threading.Barrier(2, timeout=2)
    threads: set[int] = set()

    class BarrierHttp:
        def get_bytes(self, url: str, *, accept: str) -> bytes:
            threads.add(threading.get_ident())
            barrier.wait()
            return RSS

    sources = (
        Source(name="One", url="https://one.example/rss"),
        Source(name="Two", url="https://two.example/rss"),
    )

    results = fetch_feeds(sources, BarrierHttp(), max_workers=2)  # type: ignore[arg-type]

    assert len(results) == 2
    assert len(threads) == 2


def test_results_keep_configured_order_while_requests_overlap() -> None:
    first_arrived = threading.Event()
    second_finished = threading.Event()

    class OrderedHttp:
        def get_bytes(self, url: str, *, accept: str) -> bytes:
            if url == "https://first.example/rss":
                first_arrived.set()
                assert second_finished.wait(2)
                return rss("First Publisher")
            assert first_arrived.wait(2)
            second_finished.set()
            return rss("Second Publisher")

    sources = (
        Source(name="First", url="https://first.example/rss"),
        Source(name="Second", url="https://second.example/rss"),
    )

    results = fetch_feeds(sources, OrderedHttp(), max_workers=2)  # type: ignore[arg-type]

    assert [parsed.feed.title for parsed in results] == ["First Publisher", "Second Publisher"]


def test_any_source_failure_aborts_the_whole_aggregation() -> None:
    good = Source(name="Good", url="https://good.example/rss")
    bad = Source(name="Bad Source", url="https://bad.example/rss")
    http = FakeHttp({good.url: RSS, bad.url: OSError("connection refused")})

    with pytest.raises(RuntimeError) as caught:
        fetch_feeds((good, bad), http)  # type: ignore[arg-type]

    message = str(caught.value)
    assert "Bad Source" in message
    assert bad.url in message
    assert "connection refused" in message
