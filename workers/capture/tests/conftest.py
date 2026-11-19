"""Shared test fixtures: a clean LOOKOUT_* environment, a temp data dir, a fake clock,
synthetic JPEGs (no NPS frame ever enters the repo) and a fake nps.gov behind
``httpx.MockTransport`` that records every request it receives."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import ExifTags, Image

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop LOOKOUT_* and AWS_* variables from the developer's shell."""
    for name in list(os.environ):
        if name.startswith(("LOOKOUT_", "AWS_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_host_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see this machine's processes: a real Rainier stopgap may be running,
    and no test sends anything to nps.gov. Guard tests pass their own process lists."""
    import lookout_capture.fetcher_guard as guard

    monkeypatch.setattr(guard, "list_processes", lambda: [])


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh data dir, exported as LOOKOUT_DATA_DIR."""
    root = tmp_path / "lookout-data"
    root.mkdir()
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(root))
    return root


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials so boto3 never reaches a real account (moto intercepts calls)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


class FakeClock:
    """A controllable clock: ``now()`` as aware UTC, ``time()`` as Unix seconds, ``sleep``."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.current

    def time(self) -> float:
        return self.current.timestamp()

    def monotonic(self) -> float:
        return self.current.timestamp()

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if seconds > 0:  # like time.sleep, a positive sleep always lets time pass (>= 1 us)
            self.current += max(timedelta(seconds=seconds), timedelta(microseconds=1))

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 19, 8, 0, 30, tzinfo=UTC))


def make_jpeg(
    width: int = 320,
    height: int = 180,
    *,
    color: tuple[int, int, int] = (110, 140, 170),
    exif_datetime: str | None = None,
    exif_offset: str | None = None,
    tz_offset_hours: int | None = None,
    subsec: str | None = None,
    artist: str | None = None,
    make: str | None = None,
    model: str | None = None,
    software: str | None = None,
    pattern: int = 0,
    quality: int = 90,
    ramp: str | None = None,
) -> bytes:
    """A synthetic JPEG with chosen EXIF fields (shapes vary with ``pattern``).

    ``ramp="up"``/``"down"`` paints a left-to-right brightness ramp first, which moves every
    dHash bit: the tests' stand-in for a moved camera.
    """
    image = Image.new("RGB", (width, height), color)
    if ramp:
        for x in range(width):
            level = round(255 * x / max(1, width - 1))
            level = level if ramp == "up" else 255 - level
            image.paste((level, level, level), (x, 0, x + 1, height))
    for i in range(8):
        x0 = (i * 37 + pattern * 53) % max(1, width - 10)
        y0 = (i * 23 + pattern * 31) % max(1, height - 10)
        shade = (i * 29 + pattern * 41) % 255
        image.paste(
            (shade, 255 - shade, (shade * 3) % 255),
            (x0, y0, min(width, x0 + width // 6), min(height, y0 + height // 5)),
        )
    exif = Image.Exif()
    if exif_datetime:
        exif[ExifTags.Base.DateTime] = exif_datetime
    for tag, value in (
        (ExifTags.Base.Artist, artist),
        (ExifTags.Base.Copyright, artist),
        (ExifTags.Base.Make, make),
        (ExifTags.Base.Model, model),
        (ExifTags.Base.Software, software),
    ):
        if value:
            exif[tag] = value
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    if exif_datetime:
        sub[ExifTags.Base.DateTimeOriginal] = exif_datetime
    if subsec:
        sub[ExifTags.Base.SubsecTimeOriginal] = subsec
    if exif_offset:
        sub[ExifTags.Base.OffsetTimeOriginal] = exif_offset
    if tz_offset_hours is not None:
        sub[ExifTags.Base.TimeZoneOffset] = (tz_offset_hours,)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality, exif=exif.tobytes())
    return buffer.getvalue()


@dataclass
class Resource:
    """One fake URL: body, validators, and how often it was fetched."""

    body: bytes
    content_type: str = "image/jpeg"
    last_modified: datetime | None = None
    etag: str | None = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def response_headers(self) -> dict[str, str]:
        out = {"Content-Type": self.content_type, **self.headers}
        if self.last_modified is not None:
            out["Last-Modified"] = format_datetime(self.last_modified, usegmt=True)
        if self.etag is not None:
            out["ETag"] = self.etag
        return out


class FakeNPS:
    """A stand-in for www.nps.gov: live feeds, the park camera and the archive API."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock
        self.resources: dict[str, Resource] = {}
        self.requests: list[dict[str, Any]] = []
        self.search_pages: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.assets: dict[str, bytes] = {}
        self.days: dict[str, dict[str, Any]] = {}
        self.fail_next: dict[str, int] = {}

    # -- setup helpers --------------------------------------------------------------
    def set(self, url: str, body: bytes, **kwargs: Any) -> Resource:
        res = Resource(body, **kwargs)
        self.resources[url] = res
        return res

    def count(self, method: str | None = None, contains: str = "") -> int:
        return sum(
            1
            for r in self.requests
            if (method is None or r["method"] == method) and contains in r["url"]
        )

    # -- transport ------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        when = self.clock.now() if self.clock else None
        self.requests.append(
            {"method": request.method, "url": url, "t": when, "headers": dict(request.headers)}
        )
        for pattern, remaining in list(self.fail_next.items()):
            if pattern in url and remaining > 0:
                self.fail_next[pattern] = remaining - 1
                return httpx.Response(503, headers={"Retry-After": "120"}, text="busy")
        base = url.split("?", 1)[0]
        if "/airwebcams/api/GetAvailableDays/" in base:
            code = base.rsplit("/", 1)[-1]
            if code.lower() not in self.days:
                return httpx.Response(404)
            return httpx.Response(200, json=self.days[code.lower()])
        if base.endswith("/airwebcams/api/Search/Execute") or "/api/Search/Execute/" in base:
            body = json.loads(request.content)
            code = body["Operand"]["LeftOperand"]["Attribute"]
            day = body["Operand"]["RightOperand"]["Attribute"]
            pages = self.search_pages.get((code, day))
            if pages is None:
                return httpx.Response(200, json={"Results": [], "ResultCount": 0, "PageCount": 0})
            page = int(body.get("Page") or body.get("CurrentPage") or 1)
            return httpx.Response(200, json=pages[min(page, len(pages)) - 1])
        if "/airwebcams/GetAsset/" in base:
            asset_id = base.split("/GetAsset/", 1)[1].split("/", 1)[0]
            kind = "hires" if base.endswith("/proxy/hires") else "thumbmedium"
            data = self.assets.get(f"{asset_id}:{kind}")
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, content=data, headers={"Content-Type": "image/jpeg"})
        res = self.resources.get(base)
        if res is None:
            return httpx.Response(404, text="not found")
        headers = res.response_headers()
        if res.status != 200:
            return httpx.Response(res.status, headers=headers)
        inm = request.headers.get("If-None-Match")
        ims = request.headers.get("If-Modified-Since")
        if (inm and res.etag and inm == res.etag) or (
            not inm
            and ims
            and res.last_modified
            and ims == format_datetime(res.last_modified, usegmt=True)
        ):
            return httpx.Response(304, headers=headers)
        if request.method == "HEAD":
            headers["Content-Length"] = str(len(res.body))
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, content=res.body, headers=headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake_nps(clock: FakeClock) -> FakeNPS:
    return FakeNPS(clock)


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def jpeg_factory() -> Callable[..., bytes]:
    return make_jpeg


@pytest.fixture
def s3_bucket(aws_env) -> Iterator[Any]:
    """A moto S3 bucket and client."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="lookout-test")
        yield client
