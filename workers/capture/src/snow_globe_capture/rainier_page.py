"""One look at the Mount Rainier webcam page for any archive of the park camera (M0.5 check).

D-018 found no NPS archive for ``webcams-mora/mountain.jpg``: the ``mora`` code in the
AirWebCams archive belongs to the Paradise *air* camera (512x288, looking WSW). This fetches
``https://www.nps.gov/mora/learn/photosmultimedia/webcams.htm`` once, through the archive
client (same 5 s spacing and request budget), and lists every link or phrase that could
point at past images of the mountain camera.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

from snow_globe_common.timeutil import isoformat_z, utcnow

from .archive import ArchiveClient, ArchiveError

__all__ = ["RAINIER_WEBCAMS_URL", "check_rainier_page", "scan_page"]

RAINIER_WEBCAMS_URL = "https://www.nps.gov/mora/learn/photosmultimedia/webcams.htm"
_KEYWORDS = re.compile(
    r"archiv|airwebcams|past (images|photos|webcam)|previous|time[- ]?lapse|history|"
    r"yesterday|last 24|loop|movie|animation",
    re.IGNORECASE,
)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.images: list[str] = []
        self.text: list[str] = []
        self._href: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "a":
            self._href = values.get("href", "")
            self._buffer = []
        elif tag == "img" and values.get("src"):
            self.images.append(values["src"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append({"href": self._href, "text": " ".join("".join(self._buffer).split())})
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buffer.append(data)
        self.text.append(data)


def scan_page(html: str) -> dict[str, Any]:
    """Pull out the links, images and sentences that mention archives or past images."""
    parser = _Links()
    parser.feed(html)
    text = " ".join(unescape(" ".join(parser.text)).split())
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if _KEYWORDS.search(s)]
    archive_links = [
        link
        for link in parser.links
        if _KEYWORDS.search(link["href"]) or _KEYWORDS.search(link["text"])
    ]
    webcam_images = [src for src in parser.images if "webcam" in src.lower()]
    return {
        "links_total": len(parser.links),
        "archive_like_links": archive_links,
        "archive_like_sentences": sentences[:20],
        "webcam_images": webcam_images,
        "mentions_mountain_jpg": "mountain.jpg" in html,
        "mentions_airwebcams": "airwebcams" in html.lower(),
    }


def check_rainier_page(archive: ArchiveClient, url: str = RAINIER_WEBCAMS_URL) -> dict[str, Any]:
    """Fetch the page once (counted and spaced like every archive request) and scan it."""
    finding: dict[str, Any] = {"url": url, "checked_at": isoformat_z(utcnow(), "seconds")}
    try:
        response = archive.get_page(url)
    except ArchiveError as exc:
        finding["error"] = str(exc)[:200]
        return finding
    finding["status"] = response.status_code
    finding["bytes"] = len(response.content)
    finding.update(scan_page(response.text))
    return finding
