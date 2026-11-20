"""Re-measure the plan's logged inputs (metadata only; no image is opened, nothing is fetched).

Reads, read-only:
- ~/lookout-data/archive/<vid>/<date>/*.json for dates before 2026-06-01 (sidecars only);
- the file sizes of ~/lookout-data/cache/nps-archive/search/<code>/<date>.json before 2026-06-01;
- ~/lookout-data/capture_log/2026-11-19.jsonl and 2026-11-20.jsonl.

Never reads ~/lookout-data/quarantine/. Prints one JSON document.
Run with any Python 3.13: python3 -I -B measure_inputs.py
"""

from __future__ import annotations

import json
import statistics as st
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

DATA = Path.home() / "lookout-data"
GUARD = date(2026, 6, 1)
SPLIT_V2 = "2026-11-20T02:39:39"  # capture 0.2.0 started (lead's note)


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
    return datetime.fromisoformat(rec["t"].replace("Z", "+00:00"))


def load(name: str) -> list[dict]:
    rows = []
    for line in (DATA / "capture_log" / name).read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def main() -> None:
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
    spacing = {"pairs": len(s2s), "min_end_to_start_s": round(min(e2s), 3)}
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
    main()
