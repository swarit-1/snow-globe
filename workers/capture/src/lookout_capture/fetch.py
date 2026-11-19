"""A fully read HTTP response with a size limit, shared by live capture and the archive."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["Fetched", "ResponseTooLarge", "fetch"]


class ResponseTooLarge(httpx.HTTPError):
    """The body grew past the caller's byte limit; the download was abandoned."""

    def __init__(self, url: str, limit: int) -> None:
        super().__init__(f"{url} is larger than {limit} bytes")


@dataclass
class Fetched:
    """Status, headers and the whole (transfer-decoded) body of one response."""

    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str

    @property
    def text(self) -> str:
        """The body as UTF-8 text, replacing undecodable bytes."""
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The body parsed as JSON."""
        return json.loads(self.content)


def fetch(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    max_bytes: int = 40_000_000,
) -> Fetched:
    """Send one request and read the body, giving up past ``max_bytes`` (ResponseTooLarge)."""
    with client.stream(method, url, headers=headers, json=json_body) as response:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLarge(url, max_bytes)
            chunks.append(chunk)
        return Fetched(response.status_code, response.headers, b"".join(chunks), url)
