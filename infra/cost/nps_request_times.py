"""Collect measured nps.gov request times from capture_log files, for the cost simulation (PRD §17).

infra/cost/sim_capture_s3.py can make each simulated NPS request take a measured time on its fake clock
(``--request-times``). Capture 0.2.1 and later space a camera's polls from the previous *answer* (D-065), so how
long requests take decides how far each camera's slot moves from one pass to the next, and so how long a Lambda pass
waits, which Lambda bills. This script reads ``request`` records of the live capture loop and writes their measured
durations (``elapsed_ms``), by stage and connection state, to a small JSON file. It writes nothing else and reads
only the files it is given.

- **Which records:** ``event`` ``request`` of one runner (default ``cli-loop``, the live loop) whose
  ``capture_version`` stamp is ``<package>/<--version>``, whatever the package was called (D-080), sent before
  ``--until``.
- **Stage:** ``json`` (an ARD camera's feed poll), ``head`` (Rainier's poll), ``image`` (an image GET).
- **Connection state:** ``cold`` when no earlier selected request exists, when the previous one failed, or when
  more than 5 s passed from the previous one's answer to this send (httpx's keep-alive expiry, which the polite
  client keeps); else ``warm``. A cold request pays DNS, TCP and TLS set-up first. The rules are httpcore's
  (1.0.9, the capture venv's): a connection goes back to the pool only when its response was read in full, so a
  failed request leaves none to reuse (``HTTP11Connection._response_closed``), and an idle one expires once the
  clock is *past* its answer plus the keep-alive (``has_expired``: exactly 5 s after is still warm). The one
  failure that never reaches httpx, a request capture abandons before it starts (its 30 s send deadline), comes
  more than 30 s after the previous answer, so the request after it is cold by the 5 s rule as well.
- **Answered requests only:** a record without a ``status`` (no response came back in full) is left out and
  counted. Its time is not a request time: capture bounds such an answer separately (D-066).

Only durations leave the log: no URL, no time stamp of a request. Standard library only. The committed
nps_request_times_0.2.1.json came from (DATA is the data dir):

    python3 -I infra/cost/nps_request_times.py --version 0.2.1 --until 2026-11-21T07:00:00Z \\
        --out infra/cost/nps_request_times_0.2.1.json "$DATA"/capture_log/2026-11-2[01].jsonl
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

KEEPALIVE_S = 5.0  # httpx's default keep-alive expiry; snow_globe_common.http.polite_client keeps it
STAGES = ("json", "head", "image")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="+", type=Path, help="capture_log/<date>.jsonl files to read")
    parser.add_argument("--out", required=True, type=Path, help="the JSON file to write")
    parser.add_argument("--version", required=True, help="the capture version whose records to keep, such as 0.2.1")
    parser.add_argument("--runner", default="cli-loop")
    parser.add_argument("--until", required=True, help="keep requests sent before this UTC time (ISO 8601)")
    args = parser.parse_args()
    until = parse_time(args.until)

    records = []
    for path in args.logs:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a torn last line of a log being written
                if (
                    isinstance(record, dict)
                    and record.get("event") == "request"
                    and record.get("runner") == args.runner
                    and str(record.get("capture_version", "")).rpartition("/")[2] == args.version
                    and parse_time(record["sent_at"]) < until
                ):
                    records.append(record)
    records.sort(key=lambda r: r["sent_at"])
    if not records:
        raise SystemExit("no matching request records")

    pools: dict[str, dict[str, list[float]]] = {stage: {"cold": [], "warm": []} for stage in STAGES}
    unanswered = 0
    previous_answer: datetime | None = None
    for record in records:
        sent = parse_time(record["sent_at"])
        cold = previous_answer is None or (sent - previous_answer).total_seconds() > KEEPALIVE_S
        # A failed request's connection is closed, not pooled: the next request opens a new one.
        failed = record.get("status") is None
        previous_answer = None if failed else parse_time(record["answered_at"])
        if failed or record.get("elapsed_ms") is None:
            unanswered += 1
            continue
        pools[record["stage"]]["cold" if cold else "warm"].append(float(record["elapsed_ms"]))
    for by_state in pools.values():
        for values in by_state.values():
            values.sort()

    answered = sum(len(v) for s in pools.values() for v in s.values())
    doc = {
        "description": (
            "Measured nps.gov request times (elapsed_ms) of the laptop's live capture loop, by stage and connection "
            "state; input to infra/cost/sim_capture_s3.py --request-times (infra/cost/nps_request_times.py wrote it)"
        ),
        "source": {
            "log_files": [f"capture_log/{p.name}" for p in args.logs],
            "capture_version": args.version,
            "runner": args.runner,
            "first_sent_at": records[0]["sent_at"],
            "last_sent_at": records[-1]["sent_at"],
            "until": args.until,
            "requests": len(records),
            "answered": answered,
            "left_out_unanswered": unanswered,
        },
        "cold_rule": (
            f"cold: no earlier request, the previous request failed, or more than {KEEPALIVE_S:g} s from the "
            "previous request's answer to this send (httpx keep-alive expiry); else warm"
        ),
        "counts": {stage: {state: len(v) for state, v in by_state.items()} for stage, by_state in pools.items()},
        "elapsed_ms": pools,
    }
    args.out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"source": doc["source"], "counts": doc["counts"]}, indent=1))


if __name__ == "__main__":
    main()
