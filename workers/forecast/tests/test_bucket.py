"""Anonymous HTTPS to the bucket: listing XML, retries, strict ranges (CLAUDE.md "NOAA")."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from lookout_common.http import DEFAULT_USER_AGENT

from lookout_forecast.bucket import (
    BucketError,
    HrrrBucket,
    ObjectNotFoundError,
    RangeNotHonouredError,
    TransferLedger,
    parse_list_page,
    transfer_delta,
)

from .conftest import FIXTURES


def test_parse_real_truncated_page() -> None:
    page = parse_list_page((FIXTURES / "list_t07z_page1_max6.xml").read_bytes())
    assert len(page.objects) == 6 and page.is_truncated and page.next_token
    first = page.objects[0]
    assert first.key == "hrrr.20261119/conus/hrrr.t07z.wrfsfcf00.grib2"
    assert first.size > 100_000_000
    assert first.last_modified.isoformat() == "2026-11-19T07:53:48+00:00"
    assert first.etag and '"' not in first.etag


def test_parse_real_full_page() -> None:
    page = parse_list_page((FIXTURES / "list_t07z_full.xml").read_bytes())
    assert len(page.objects) == 38 and not page.is_truncated and page.next_token is None


def test_s3_error_and_garbage() -> None:
    with pytest.raises(BucketError, match="NoSuchBucket"):
        parse_list_page(b"<Error><Code>NoSuchBucket</Code><Message>x</Message></Error>")
    with pytest.raises(BucketError):
        parse_list_page(b"not xml")


def _client(handler, **kw) -> HrrrBucket:
    sleeps: list[float] = []
    client = HrrrBucket(transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw)
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def test_project_user_agent_and_pagination() -> None:
    seen: list[httpx.Request] = []
    page1 = (FIXTURES / "list_t07z_page1_max6.xml").read_bytes()
    page2 = (FIXTURES / "list_t07z_full.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        token = request.url.params.get("continuation-token")
        return httpx.Response(200, content=page2 if token else page1)

    with _client(handler) as client:
        objects = client.list_objects("hrrr.20261119/conus/hrrr.t07z.wrfsfcf")
    assert len(objects) == 6 + 38
    assert seen[0].headers["user-agent"] == DEFAULT_USER_AGENT
    assert seen[0].url.params["list-type"] == "2"
    assert seen[1].url.params["continuation-token"].startswith("1K+tIRws")
    assert "cookie" not in seen[0].headers
    assert client.ledger.requests["listing"] == 2


def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="1:0:d=2026111906:A:surface:anl:\n")

    with _client(handler, backoff_s=1.0) as client:
        assert client.get_text("x.idx").startswith("1:0")
    assert client.sleeps == [1.0, 2.0]
    assert client.ledger.retries == 2 and client.ledger.requests == {"idx": 1}


def test_retry_after_is_honoured_and_attempts_are_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"})

    with _client(handler, max_attempts=3) as client, pytest.raises(BucketError, match="3 attempts"):
        client.get_text("x.idx")
    assert client.sleeps == [7.0, 7.0]


def test_transport_errors_are_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, text="ok")

    with _client(handler) as client:
        assert client.get_text("x.idx") == "ok"


def test_not_found_is_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with _client(handler) as client, pytest.raises(ObjectNotFoundError):
        client.get_text("missing.idx")
    assert client.sleeps == []


def test_range_must_be_honoured_exactly() -> None:
    body = bytes(range(256)) * 4

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("full"):
            return httpx.Response(200, content=body)
        if request.url.path.endswith("wrong"):
            return httpx.Response(
                206, content=body[:10], headers={"Content-Range": "bytes 0-9/1024"}
            )
        if request.url.path.endswith("short"):
            return httpx.Response(
                206, content=body[5:9], headers={"Content-Range": "bytes 5-10/1024"}
            )
        start, end = 5, 10
        return httpx.Response(
            206,
            content=body[start : end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/1024"},
        )

    with _client(handler) as client:
        assert client.get_range("ok", 5, 10) == body[5:11]
        with pytest.raises(RangeNotHonouredError, match="expected 206"):
            client.get_range("full", 5, 10)
        with pytest.raises(RangeNotHonouredError, match="does not match"):
            client.get_range("wrong", 5, 10)
        with pytest.raises(RangeNotHonouredError, match="got 4 bytes"):
            client.get_range("short", 5, 10)
        with pytest.raises(ValueError):
            client.get_range("ok", 10, 5)
    # Every byte received is counted, including the 4 of the rejected short body.
    assert client.ledger.bytes["grib"] == 6 + 4


def test_transfer_delta_reports_only_what_came_after() -> None:
    ledger = TransferLedger()
    ledger.add("listing", 100, 0.5)
    ledger.add("grib", 1000, 1.0)
    before = ledger.snapshot()
    ledger.add("grib", 250, 0.25)
    ledger.add("idx", 10, 0.125)
    ledger.add_retry()
    assert ledger.since(before) == {
        "requests": {"grib": 1, "idx": 1},
        "bytes": {"grib": 250, "idx": 10},
        "seconds": {"grib": 0.25, "idx": 0.125},
        "retries": 1,
        "total_bytes": 260,
    }
    assert transfer_delta(before, before)["total_bytes"] == 0
    assert ledger.snapshot()["total_bytes"] == 1360  # the lifetime count is unchanged


def test_clients_of_finished_threads_are_closed() -> None:
    """Review T6: a client per pool thread used to stay open until the bucket closed."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x"))
    with HrrrBucket(transport=transport) as bucket:
        bucket.get_text("a.idx")  # the main thread's client
        opened: list[int] = []
        earlier: list[httpx.Client] = []
        for _ in range(9):
            with ThreadPoolExecutor(max_workers=4) as pool:
                barrier = threading.Barrier(4)

                def fetch(_: int, barrier: threading.Barrier = barrier) -> None:
                    barrier.wait(timeout=5)  # four distinct threads, each with a client
                    bucket.get_text("a.idx")

                list(pool.map(fetch, range(4)))
            opened.append(bucket.open_clients)
            current = [client for _, client in bucket._clients]
            assert all(client.is_closed for client in earlier if client not in current)
            earlier = current
        assert opened == [5] * 9  # main + the last pool's 4 threads, never more
        last = [client for _, client in bucket._clients]
    assert all(client.is_closed for client in last)
