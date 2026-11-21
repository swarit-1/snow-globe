"""Re-measure the plan's logged inputs (metadata only; no image is opened, nothing is fetched).

Feeds the measured inputs of docs/research/archive-backfill-plan.md, section 6 (PRD §15.3).

Reads, read-only:
- $SNOW_GLOBE_DATA_DIR/archive/<vid>/<UTC date>/*.json for dates before 2026-06-01 (sidecars only);
- the file sizes of $SNOW_GLOBE_DATA_DIR/cache/nps-archive/search/<code>/<date>.json before
  2026-06-01;
- the capture logs of 19 and 20 Nov 2026 (``--logs``, default $SNOW_GLOBE_DATA_DIR/capture_log);
- the archive queue (``--queue``, default $SNOW_GLOBE_DATA_DIR/state/archive/queue.json), only to
  tell the frames of hourly entries from those of midday entries.

Never reads $SNOW_GLOBE_DATA_DIR/quarantine/. Prints one JSON document.
Run with Python 3.13 (it uses itertools.pairwise), from eval/:
    uv run --no-sync python tools/backfill_plan/measure_inputs.py [--logs DIR] [--queue FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics as st
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path


def _data_dir() -> Path:
    """``$SNOW_GLOBE_DATA_DIR``, else ``$LOOKOUT_DATA_DIR``, else ``~/snow-globe-data`` if it
    exists, else ``~/lookout-data`` if it exists, else ``~/snow-globe-data``: the workers' order
    (D-080), written out here because this script imports nothing of the project. Like the
    workers, it refuses while both folders exist and are different folders."""
    for name in ("SNOW_GLOBE_DATA_DIR", "LOOKOUT_DATA_DIR"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return Path(value).expanduser()
    home = Path.home()
    new, old = home / "snow-globe-data", home / "lookout-data"
    if new.exists():
        if old.exists() and not os.path.samefile(new, old):
            raise SystemExit(
                f"measure_inputs.py: both {new} and {old} exist and are different folders, so "
                "the data dir is ambiguous (D-080); set SNOW_GLOBE_DATA_DIR"
            )
        return new
    return old if old.exists() else new


DATA = _data_dir()
GUARD = date(2026, 6, 1)
SPLIT_V2 = "2026-11-20T02:39:39"  # capture 0.2.0 started (lead's note)
#: The first archive-queue day: every archive request in this day's log is the queue's (D-052).
QUEUE_DAY = "2026-11-20.jsonl"
#: The lead queued the first tranche at 09:50:30 UTC on 20 Nov; the archive check, the only
#: earlier archive fetcher, ended on 19 Nov. Sidecars fetched from then on are the queue's.
QUEUE_FROM = "2026-11-20T09:50:30"
#: Consecutive pass starts further apart than this mean the loop was not running between them.
OUTAGE_GAP_S = 400.0


def q(values: list[float], p: float) -> dict[str, float]:
    """The p-quantile three ways: lower nearest rank, inclusive and exclusive interpolation."""
    xs = sorted(values)
    n = len(xs)
    k = max(0, min(n - 1, int(p * n) - 1 if (p * n).is_integer() else int(p * n)))
    out = {"nearest_rank_lower": xs[max(0, int(p * n) - 1)], "nearest_rank": xs[k]}
    if n >= 2:
        cut = round(p * 100)
        out["inclusive"] = st.quantiles(xs, n=100, method="inclusive")[cut - 1]
        out["exclusive"] = st.quantiles(xs, n=100, method="exclusive")[cut - 1]
    return {k2: round(v, 3) for k2, v in out.items()}


def t_of(rec: dict) -> datetime:
    """The record's log time (written after the event), as an aware datetime."""
    return datetime.fromisoformat(rec["t"].replace("Z", "+00:00"))


LOGS = DATA / "capture_log"


def load(name: str) -> list[dict]:
    """One capture-log day as records (a line that is not JSON is skipped)."""
    rows = []
    for line in (LOGS / name).read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def outcome_of(rec: dict) -> str:
    """ok, connect_timeout, read_timeout, disconnect, http_error or other, for one request."""
    error = str(rec.get("error") or "")
    if not error and (rec.get("status") or 0) < 400:
        return "ok"
    if "ConnectTimeout" in error:
        return "connect_timeout"
    if "ReadTimeout" in error:
        return "read_timeout"
    if "RemoteProtocolError" in error:
        return "disconnect"
    if (rec.get("status") or 0) >= 400:
        return "http_error"
    return "other"


def summary(xs: list[float]) -> dict[str, object]:
    """n, median, mean, p90 (inclusive) and max of a list of seconds or bytes."""
    return {
        "n": len(xs),
        "median": round(st.median(xs), 4),
        "mean": round(st.mean(xs), 4),
        "p90": q(xs, 0.9),
        "max": max(xs),
    }


def exact_seconds(stamp: str) -> Decimal:
    """A log stamp (``2026-11-20T09:56:47.412Z``) as exact seconds since 0001-01-01."""
    day, clock = stamp.rstrip("Z").split("T")
    hh, mm, ss = clock.split(":")
    ordinal = date.fromisoformat(day).toordinal()
    return Decimal(ordinal) * 86400 + Decimal(hh) * 3600 + Decimal(mm) * 60 + Decimal(ss)


#: The end-to-start bins reported by :func:`end_to_start_spacing` (seconds).
SPACING_CUTS = (Decimal("5.000"), Decimal("5.0009"), Decimal("5.005"), Decimal("5.05"))


def end_to_start_spacing(reqs: list[dict], lowest: int = 10) -> dict[str, object]:
    """Exact end-to-start gaps of consecutive archive requests, in decimal arithmetic.

    A request's end is its log stamp ``t`` (1 ms resolution, written after the answer), its
    start ``t`` minus ``elapsed_ms`` (0.1 ms). The gap is the next start minus this end, so
    a value within about 1 ms of 5.000 s cannot be told from 5.000 s. Counts the gaps in
    each bin between SPACING_CUTS and lists the ``lowest`` with their stamps (PRD §15.3's
    5 s rule).
    """
    reqs = sorted(reqs, key=lambda r: r["t"])
    ends = [exact_seconds(r["t"]) for r in reqs]
    starts = [e - Decimal(str(r["elapsed_ms"])) / 1000 for e, r in zip(ends, reqs, strict=True)]
    gaps = sorted(
        (starts[i + 1] - ends[i], reqs[i]["t"], reqs[i + 1]["t"]) for i in range(len(reqs) - 1)
    )
    edges = ["0", *(str(c) for c in SPACING_CUTS), "inf"]
    names = [f"[{edges[k]}, {edges[k + 1]})" for k in range(len(edges) - 1)]
    bins = dict.fromkeys(names, 0)
    for gap, _, _ in gaps:
        bins[names[sum(1 for c in SPACING_CUTS if gap >= c)]] += 1
    return {
        "pairs": len(gaps),
        "bins_s": bins,
        "lowest": [{"gap_s": str(g), "end_t": a, "next_t": b} for g, a, b in gaps[:lowest]],
    }


def queue_requests(rows: list[dict]) -> dict[str, object]:
    """One day of archive-queue requests (D-052): outcomes, times, spacing, span, bytes.

    Each request's start is its log time minus ``elapsed_ms`` (the record is written after the
    answer or the failure); ``waited_s`` is the limiter's wait before it. ``hours_to_1000th``
    is the time from the first request's start to the 1,000th request's end: one UTC day's
    budget, the figure loop_sim.py's hours mode predicts (1.835 h at 20 Nov's inputs).
    """
    reqs = sorted((r for r in rows if r.get("event") == "archive_request"), key=t_of)
    outcomes = [outcome_of(r) for r in reqs]
    ok = [r for r, o in zip(reqs, outcomes, strict=True) if o == "ok"]
    starts = [t_of(r).timestamp() - r["elapsed_ms"] / 1000 for r in reqs]
    ends = [t_of(r).timestamp() for r in reqs]
    s2s = [b - a for a, b in pairwise(starts)]
    e2s = [b - a for a, b in zip(ends, starts[1:], strict=False)]
    by_kind = Counter(str(r.get("kind")) for r in reqs)
    return {
        "requests": len(reqs),
        "by_kind": dict(sorted(by_kind.items())),
        "first_start": reqs[0]["t"],
        "last_end": reqs[-1]["t"],
        "hours_first_start_to_last_end": round((ends[-1] - starts[0]) / 3600, 3),
        "outcomes": dict(Counter(outcomes)),
        "failed": sum(1 for o in outcomes if o != "ok"),
        "error_rate": round(sum(1 for o in outcomes if o != "ok") / len(reqs), 4),
        "failures": [
            {
                "t": r["t"],
                "kind": r.get("kind"),
                "elapsed_ms": r.get("elapsed_ms"),
                "waited_s": r.get("waited_s"),
                "outcome": o,
            }
            for r, o in zip(reqs, outcomes, strict=True)
            if o != "ok"
        ],
        "sequence": outcomes,
        "elapsed_s_ok": {
            k: summary([r["elapsed_ms"] / 1000 for r in ok if r.get("kind") == k])
            for k in sorted({str(r.get("kind")) for r in ok})
        },
        "bytes_ok": {
            k: sum(int(r.get("bytes") or 0) for r in ok if r.get("kind") == k)
            for k in sorted({str(r.get("kind")) for r in ok})
        },
        "search_bytes_ok": summary(
            [float(r.get("bytes") or 0) for r in ok if r.get("kind") == "search"]
        ),
        "spacing": {
            "min_end_to_start_s": round(min(e2s), 3),
            "start_to_start_median_s": round(st.median(s2s), 3),
            "start_to_start_under_6_5_s": summary([x for x in s2s if x < 6.5]),
            "end_to_start_exact": end_to_start_spacing(reqs),
        },
        "hours_to_1000th": round((ends[999] - starts[0]) / 3600, 3) if len(reqs) >= 1000 else None,
    }


def hourly_entry_days(queue_path: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(viewpoint, date) pairs of the queue's hourly entries and of its midday entries."""
    doc = json.loads(queue_path.read_text(encoding="utf-8"))
    hourly = {(e["viewpoint_id"], e["date"]) for e in doc["entries"] if e.get("pick") == "hourly"}
    midday = {(e["viewpoint_id"], e["date"]) for e in doc["entries"] if e.get("pick") == "midday"}
    return hourly, midday


def by_queue(doc: dict) -> bool:
    """True for a sidecar the archive queue wrote: fetched once the queue had started."""
    return str(doc.get("fetched_at_utc", "")) >= QUEUE_FROM


def queue_frames(sidecars: list[dict], queue_path: Path) -> dict[str, object]:
    """Sizes of the hires frames the queue stored (its sidecars), by aspect and pick.

    A frame belongs to an hourly entry when its viewpoint and the date of its
    ImageCreateDateTime (the date the entry searched) have an hourly entry in the queue.
    """
    hourly, midday = hourly_entry_days(queue_path)
    frames = [d for d in sidecars if d.get("kind") == "hires" and by_queue(d)]

    def entry_day(d: dict) -> tuple[str, str]:
        month, day, year = str(d["image_create_datetime_raw"]).split()[0].split("/")
        return d["viewpoint_id"], f"{year}-{int(month):02d}-{int(day):02d}"

    def stats(rows: list[dict]) -> dict[str, object]:
        sizes = sorted(int(d["content_length"]) for d in rows)
        if not sizes:
            return {"n": 0}
        cuts = st.quantiles(sizes, n=20, method="inclusive") if len(sizes) > 1 else sizes * 19
        return {
            "n": len(sizes),
            "mean": round(st.mean(sizes), 1),
            "median": st.median(sizes),
            "p5": round(cuts[0], 1),
            "p95": round(cuts[-1], 1),
            "min": sizes[0],
            "max": sizes[-1],
        }

    on_hourly = [d for d in frames if entry_day(d) in hourly]
    on_midday_only = [d for d in frames if entry_day(d) in midday and entry_day(d) not in hourly]
    by_vid = {
        vid: stats([d for d in on_hourly if d["viewpoint_id"] == vid])
        for vid in sorted({d["viewpoint_id"] for d in on_hourly})
    }
    return {
        "all_0.2.0_hires": stats(frames),
        "aspect": {
            f"{w}x{h}": stats(
                [d for d in frames if (d["original_width"], d["original_height"]) == (w, h)]
            )
            for w, h in sorted({(d["original_width"], d["original_height"]) for d in frames})
        },
        "hourly_entries": stats(on_hourly),
        "hourly_entries_16x9": stats([d for d in on_hourly if d["original_height"] == 562]),
        "hourly_entries_by_viewpoint": by_vid,
        "midday_entries": stats(on_midday_only),
    }


def loop_uptime(passes: list[dict]) -> dict[str, object]:
    """Hours the loop ran per UTC day: gaps under OUTAGE_GAP_S between pass starts, split by day.

    A gap of OUTAGE_GAP_S or more counts as an outage (the Mac asleep or the loop stopped).
    The last day is counted only up to its last logged pass.
    """
    starts = [t_of(r).timestamp() - float(r["duration_s"]) for r in passes]
    per_day: Counter[str] = Counter()
    outages: list[dict[str, object]] = []
    for a, b in pairwise(starts):
        if b - a >= OUTAGE_GAP_S:
            outages.append(
                {
                    "from": datetime.fromtimestamp(a, UTC).isoformat(timespec="seconds"),
                    "to": datetime.fromtimestamp(b, UTC).isoformat(timespec="seconds"),
                    "gap_s": round(b - a, 1),
                }
            )
            continue
        t = a
        while t < b:  # split a running gap at UTC midnight
            day = datetime.fromtimestamp(t, UTC).date()
            midnight = datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() + 86400
            end = min(b, midnight)
            per_day[day.isoformat()] += end - t
            t = end
    return {
        "hours_by_utc_day": {k: round(v / 3600, 2) for k, v in sorted(per_day.items())},
        "outages": outages,
        "first_pass": passes[0]["t"],
        "last_pass": passes[-1]["t"],
    }


def main(queue_path: Path, extra_days: Sequence[str] = ()) -> None:
    """Measure everything and print one JSON document.

    ``extra_days`` names later capture-log days (``2026-11-21.jsonl``) whose archive-queue
    requests are measured the same way, for the time model's out-of-sample check.
    """
    out: dict[str, object] = {}
    # --- sidecars ------------------------------------------------------------------------
    sidecars = []
    for path in sorted((DATA / "archive").glob("*/*/*.json")):
        day = date.fromisoformat(path.parent.name)
        if day >= GUARD:
            continue
        text = path.read_text(encoding="utf-8")
        doc = json.loads(text)
        sidecars.append((path, path.stat().st_size, len(text), doc))
    hires = [d for _, _, _, d in sidecars if d.get("kind") == "hires"]
    thumbs = [d for _, _, _, d in sidecars if d.get("kind") == "thumbmedium"]
    out["sidecars"] = {
        "n": len(sidecars),
        "mean_file_bytes": round(st.mean(s for _, s, _, _ in sidecars), 1),
        "mean_chars": round(st.mean(c for _, _, c, _ in sidecars), 1),
        "latest_date": max(p.parent.name for p, _, _, _ in sidecars),
    }
    queue_sidecars = [s for _, s, _, d in sidecars if by_queue(d)]
    out["sidecars_queue"] = {
        "n": len(queue_sidecars),
        "mean_file_bytes": round(st.mean(queue_sidecars), 1) if queue_sidecars else None,
    }
    hl = [int(d["content_length"]) for d in hires]
    out["hires"] = {
        "n": len(hl),
        "mean": round(st.mean(hl), 1),
        "median": st.median(hl),
        "min": min(hl),
        "max": max(hl),
        "sizes": sorted((d["original_width"], d["original_height"]) for d in hires),
        "dates": sorted(d["image_time_utc"][:10] for d in hires),
    }
    tl = [int(d["content_length"]) for d in thumbs]
    out["thumbmedium"] = {"n": len(tl), "mean": round(st.mean(tl), 1)}
    preguard_assets = {d["asset_id"] for d in hires + thumbs}

    # --- cached searches (sizes only) -----------------------------------------------------
    sizes = []
    for path in (DATA / "cache" / "nps-archive" / "search").glob("*/*.json"):
        if date.fromisoformat(path.stem) < GUARD:
            sizes.append(path.stat().st_size)
    out["cached_search_files"] = {"n": len(sizes), "mean_bytes": round(st.mean(sizes), 1)}

    # --- 19 Nov archive requests ------------------------------------------------------------
    reqs = [r for r in load("2026-11-19.jsonl") if r.get("event") == "archive_request"]
    reqs.sort(key=t_of)
    # The two deliberate paging probes of the archive check (docs/research/nps-archive-check.md,
    # "Paging is not working"): page 2 by URL and page 2 in the body, both HTTP 500.
    probes = [r for r in reqs if r.get("status") == 500 and r.get("kind") == "search"]
    real = [r for r in reqs if r not in probes]
    failed = [r for r in real if r.get("error") or (r.get("status") or 0) >= 400]
    ok = [r for r in real if r not in failed]
    search_ok = [r for r in ok if r.get("kind") == "search"]
    out["archive_requests_19nov"] = {
        "all": len(reqs),
        "paging_probes_http500": len(probes),
        "counted": len(real),
        "failed": len(failed),
        "error_rate": round(len(failed) / len(real), 4),
        "failures": [
            {
                "t": r["t"],
                "kind": r.get("kind"),
                "status": r.get("status"),
                "elapsed_ms": r.get("elapsed_ms"),
                "error": (r.get("error") or "")[:90],
            }
            for r in failed
        ],
        "by_kind_ok": {
            k: sum(1 for r in ok if r.get("kind") == k) for k in sorted({r["kind"] for r in ok})
        },
    }
    se = [r["elapsed_ms"] / 1000 for r in search_ok]
    out["search_ok"] = {
        "n": len(se),
        "bytes_mean": round(st.mean(r["bytes"] for r in search_ok), 1),
        "elapsed_median_s": round(st.median(se), 4),
        "elapsed_mean_s": round(st.mean(se), 4),
        "elapsed_p90_s": q(se, 0.9),
        "elapsed_max_s": max(se),
    }
    pre = []
    for r in ok:
        url = str(r.get("url"))
        if "/GetAsset/" in url:
            asset = url.split("/GetAsset/", 1)[1].split("/", 1)[0]
            if asset in preguard_assets:
                kind = "hires" if url.endswith("/proxy/hires") else "thumbmedium"
                pre.append((kind, r["elapsed_ms"] / 1000))
    for kind in ("hires", "thumbmedium"):
        xs = [e for k, e in pre if k == kind]
        if xs:
            out[f"{kind}_get_ok_preguard"] = {
                "n": len(xs),
                "elapsed_median_s": round(st.median(xs), 4),
                "elapsed_mean_s": round(st.mean(xs), 4),
                "elapsed_max_s": max(xs),
            }
    # Spacing of consecutive requests: start = t - elapsed (t is written after the request).
    starts = [t_of(r).timestamp() - r["elapsed_ms"] / 1000 for r in reqs]
    ends = [t_of(r).timestamp() for r in reqs]
    s2s = [b - a for a, b in pairwise(starts)]
    e2s = [b - a for a, b in zip(ends, starts[1:], strict=False)]
    spacing = {
        "pairs": len(s2s),
        "min_end_to_start_s": round(min(e2s), 3),
        "end_to_start_exact": end_to_start_spacing(reqs),
    }
    for cut in (6.5, 10.0, 25.0, 30.0, None):
        xs = [x for x in s2s if cut is None or x < cut]
        ys = [y for x, y in zip(s2s, e2s, strict=True) if cut is None or x < cut]
        spacing[f"start_to_start_under_{cut}"] = {
            "n": len(xs),
            "median": round(st.median(xs), 3),
            "mean": round(st.mean(xs), 3),
            "p90": q(xs, 0.9),
            "end_to_start_median": round(st.median(ys), 3),
        }
    out["spacing_19nov"] = spacing

    # --- live passes --------------------------------------------------------------------
    passes = [
        r
        for name in ("2026-11-19.jsonl", "2026-11-20.jsonl")
        for r in load(name)
        if r.get("event") == "pass" and r.get("worker") == "capture"
    ]
    passes.sort(key=t_of)

    def pass_stats(rows: list[dict]) -> dict[str, object]:
        d = [float(r["duration_s"]) for r in rows]
        st_starts = [t_of(r).timestamp() - float(r["duration_s"]) for r in rows]
        gaps = [b - a for a, b in pairwise(st_starts)]
        short = [g for g in gaps if g < 400]
        return {
            "n": len(d),
            "first": rows[0]["t"],
            "last": rows[-1]["t"],
            "duration_median_s": round(st.median(d), 3),
            "duration_mean_s": round(st.mean(d), 3),
            "duration_p90_s": q(d, 0.9),
            "duration_max_s": max(d),
            "start_gap_median_all_s": round(st.median(gaps), 2) if gaps else None,
            "start_gap_median_under_400_s": round(st.median(short), 2) if short else None,
            "start_gap_mean_under_400_s": round(st.mean(short), 2) if short else None,
            "gaps_over_400": sum(1 for g in gaps if g >= 400),
        }

    out["passes_first_73"] = pass_stats(passes[:73])
    out["passes_all_so_far"] = pass_stats(passes)
    v2 = [r for r in passes if r["t"] >= SPLIT_V2]
    if len(v2) >= 2:
        out["passes_capture_0.2.0"] = pass_stats(v2)
    out["loop_uptime"] = loop_uptime(passes)

    # --- the archive queue's first day (20 Nov), and any later day asked for --------------
    out["queue_requests_20nov"] = queue_requests(load(QUEUE_DAY))
    for name in extra_days:
        out[f"queue_requests_{name}"] = queue_requests(load(name))
    out["queue_hires_frames"] = queue_frames([d for _, _, _, d in sidecars], queue_path)
    lines = (LOGS / QUEUE_DAY).read_text().splitlines()
    sizes: dict[str, list[int]] = {"archive_request": [], "stored": [], "queue_done": []}
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("worker") == "archive" and rec.get("event") in sizes:
            sizes[rec["event"]].append(len(line.encode()) + 1)
    out["archive_log_line_bytes_20nov"] = {
        k: {"n": len(v), "mean": round(st.mean(v), 1)} for k, v in sizes.items() if v
    }

    # --- live traffic, 20 Nov 00:04-03:06 UTC ----------------------------------------------
    live = [
        r
        for r in load("2026-11-20.jsonl")
        if r.get("event") == "request" and r.get("worker") == "capture"
    ]
    window = [r for r in live if "2026-11-20T00:04" <= r["t"] < "2026-11-20T03:07"]
    if window:
        span_h = (t_of(window[-1]) - t_of(window[0])).total_seconds() / 3600
        nbytes = sum(int(r.get("bytes") or 0) for r in window)
        out["live_traffic_20nov_0004_0306"] = {
            "requests": len(window),
            "bytes": nbytes,
            "span_h": round(span_h, 3),
            "per_24h_requests": round(len(window) / span_h * 24),
            "per_24h_gb": round(nbytes / span_h * 24 / 1e9, 3),
        }
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--logs", type=Path, default=LOGS, help="folder of capture-log days")
    parser.add_argument(
        "--queue",
        type=Path,
        default=DATA / "state" / "archive" / "queue.json",
        help="archive queue file or a copy of it (read only)",
    )
    parser.add_argument(
        "--day",
        action="append",
        default=[],
        help="also measure this capture-log day's queue requests (e.g. 2026-11-21.jsonl)",
    )
    args = parser.parse_args()
    LOGS = args.logs
    main(args.queue, args.day)
