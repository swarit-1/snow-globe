"""The Horizons client and parser on recorded responses (PRD §8.4; never the network)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import FIXTURES
from snow_globe_common.http import MinIntervalLimiter

from snow_globe_eval.ephem_validate.horizons import (
    API_URL,
    HorizonsCache,
    HorizonsQuery,
    HorizonsQueryError,
    HorizonsUnavailableError,
    fetch,
    parse_result,
)
from snow_globe_eval.ephem_validate.plan import AIRLESS_QUANTITIES, REFRACTED_QUANTITIES

HORIZONS = FIXTURES / "ephem" / "horizons"
#: The site and day the fixtures were recorded for (Rainier's manifest position on 19 Nov).
SITE = {"lat_deg": 46.78455, "lon_deg": -121.74065, "height_m": 1643.0}
DAY = (datetime(2026, 11, 21, tzinfo=UTC), datetime(2026, 11, 22, tzinfo=UTC))


def _query(body: str, apparent: str, *, step: str = "1h", window=DAY) -> HorizonsQuery:
    quantities = AIRLESS_QUANTITIES if apparent == "airless" else REFRACTED_QUANTITIES
    return HorizonsQuery(
        body=body,  # type: ignore[arg-type]
        start=window[0],
        stop=window[1],
        step=step,
        apparent=apparent,  # type: ignore[arg-type]
        quantities=quantities,
        **SITE,
    )


def _record(name_part: str) -> dict:
    (path,) = sorted(HORIZONS.glob(f"fixture.rainier.{name_part}.*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def _fast_limiter() -> MinIntervalLimiter:
    """A process-local limiter on a fake clock, so the tests never sleep for real."""
    now = [1_000_000.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return MinIntervalLimiter("jpl-test", 2.0, mode="process", clock=clock, sleep=sleep)


def test_query_parameters_are_the_recorded_ones() -> None:
    """The fixtures were fetched with exactly these parameters: their keys must not drift.

    A changed parameter would change every cache key, and the next run would fetch all 32
    tables again; this pins the format.
    """
    for name, query in (
        ("sun.airless.20261121", _query("sun", "airless")),
        ("sun.refracted.20261121", _query("sun", "refracted")),
        ("moon.airless.20261121", _query("moon", "airless")),
        ("moon.refracted.20261121", _query("moon", "refracted")),
    ):
        record = _record(name)
        assert record["params"] == query.params()
        assert record["key"] == query.key()
        assert query.params()["SITE_COORD"] == "'-121.740650,46.784550,1.6430'"
    assert _query("sun", "airless").params()["APPARENT"] == "'AIRLESS'"
    assert len(_query("sun", "airless").epochs()) == 25


def test_query_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="body"):
        HorizonsQuery("mars", 0, 0, 0, *DAY, "1h", "airless", "4")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="step"):
        _query("sun", "airless", step="1 hour")
    with pytest.raises(ValueError, match="whole minutes"):
        _query("sun", "airless", window=(datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC), DAY[1]))


def test_parse_airless_table() -> None:
    record = _record("sun.airless.20261121")
    table = parse_result(record["response"]["result"])
    assert len(table) == 25
    assert table.times_utc[0] == DAY[0] and table.times_utc[-1] == DAY[1]
    assert set(table.values) == {
        "ra_deg",
        "dec_deg",
        "az_deg",
        "el_deg",
        "illum_pct",
        "delta_au",
        "deldot_km_s",
    }
    # The first row as Horizons printed it (2026-Nov-21 00:00, Sun at 3.23 deg).
    assert table.values["az_deg"][0] == pytest.approx(236.137934347, abs=1e-9)
    assert table.values["el_deg"][0] == pytest.approx(3.233547908, abs=1e-9)
    assert table.values["delta_au"][0] == pytest.approx(0.98799926760041, abs=1e-14)
    assert table.solar_flags[0] == "*" and table.lunar_flags[0] == "m"
    assert table.header["Atmos refraction"] == "NO (AIRLESS)"
    assert "DE441" in table.header["Target body name"]
    assert table.header["EOP file"].startswith("eop.")


def test_parse_refracted_table() -> None:
    table = parse_result(_record("sun.refracted.20261121")["response"]["result"])
    assert set(table.values) == {"az_deg", "el_deg"}
    assert table.header["Atmos refraction"].startswith("YES")
    assert table.values["el_deg"][0] == pytest.approx(3.451137220, abs=1e-9)


def test_parse_rejects_a_text_without_an_ephemeris() -> None:
    with pytest.raises(HorizonsQueryError, match=r"\$\$SOE"):
        parse_result("no ephemeris here")


def _cache_with(tmp_path: Path, *names: str) -> HorizonsCache:
    folder = tmp_path / "horizons"
    folder.mkdir(parents=True)
    for name in names:
        (path,) = sorted(HORIZONS.glob(f"fixture.rainier.{name}.*.json"))
        shutil.copy(path, folder / path.name)
    return HorizonsCache(tmp_path)


def test_cached_response_is_used_offline(tmp_path: Path) -> None:
    cache = _cache_with(tmp_path, "sun.airless.20261121")
    record = fetch(_query("sun", "airless"), cache=cache, offline=True)
    assert record["key"] == _query("sun", "airless").key()
    with pytest.raises(HorizonsUnavailableError, match="fetching is off"):
        fetch(_query("moon", "airless"), cache=cache, offline=True)


def test_fetch_caches_and_never_fetches_twice(tmp_path: Path) -> None:
    recorded = _record("moon.refracted.20261121")["response"]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=recorded)

    cache = HorizonsCache(tmp_path)
    query = _query("moon", "refracted")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = fetch(query, cache=cache, client=client, limiter=_fast_limiter(), label="t")
        second = fetch(query, cache=cache, client=client, limiter=_fast_limiter(), label="t")
    assert len(seen) == 1
    assert str(seen[0].url).startswith(API_URL)
    assert seen[0].url.params["COMMAND"] == "'301'"
    assert first["response"] == recorded == second["response"]
    assert Path(first["path"]).name == f"t.{query.key()[:20]}.json"


def test_fetch_backs_off_on_503_then_succeeds(tmp_path: Path) -> None:
    recorded = _record("sun.refracted.20261121")["response"]
    answers = [httpx.Response(503, text="busy"), httpx.Response(200, json=recorded)]

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    limiter = _fast_limiter()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        record = fetch(
            _query("sun", "refracted"),
            cache=HorizonsCache(tmp_path),
            client=client,
            limiter=limiter,
        )
    assert record["http_status"] == 200
    assert answers == []


def test_fetch_gives_up_after_the_retries(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HorizonsUnavailableError, match="re-run to resume"),
    ):
        fetch(
            _query("sun", "refracted"),
            cache=HorizonsCache(tmp_path),
            client=client,
            limiter=_fast_limiter(),
            max_attempts=3,
        )
    assert not (tmp_path / "horizons").exists() or not any((tmp_path / "horizons").iterdir())


def test_a_rejected_query_is_raised_and_not_cached(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "Cannot interpret date", "signature": {}})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HorizonsQueryError, match="no ephemeris"),
    ):
        fetch(
            _query("sun", "airless"),
            cache=HorizonsCache(tmp_path),
            client=client,
            limiter=_fast_limiter(),
        )
    assert HorizonsCache(tmp_path).find(_query("sun", "airless")) is None
