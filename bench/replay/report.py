"""Render the replay report as Markdown (PRD §10.6 "Report"; bench/README.md "Scoring").

`markdown()` takes the dict that score.py's `build_report()` makes (the same one written as
replay-report.json) and returns the text of replay-report.md. It starts with the provenance, any
stage of the scorer that failed, and the replay set's known bias. Then for each setup (speech
setup and levers):

- the latency budgets against idle trials on the test split (PRD §10.1 "p95, idle");
- the accuracy targets against the whole test split, every state, with idle beside them (PRD §14
  "replay test set", §7.1 "Wilson upper bound on the test set"), the wrong-paint target with its
  Wilson bounds;
- the budget hit rates with their denominators named;
- accuracy and wrong paints in detail, every stage's p50 and p95 with intervals, the
  state-by-display summary, the follow-ups, the dev split, and a per-utterance table.

It adds no number of its own.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

STATE_ORDER = ("all", "idle", "warm", "cold", "after_wake", "after_launch", "unknown")
DISPLAY_ORDER = ("all", "on", "asleep", "unknown")
STATE_NAMES = {
    "all": "all states",
    "idle": "idle (≥ 30 min, the headline)",
    "warm": "warm (< 60 s)",
    "cold": "cold (caches cleared)",
    "after_wake": "after wake",
    "after_launch": "after launch",
    "unknown": "state not recorded",
}
SETUP_KEYS = (
    "engine",
    "silence_pad_ms",
    "model_retention",
    "warm_up",
    "pre_render",
    "partial_paint",
    "paint_capture",
)


def ms(v: float | None) -> str:
    """Milliseconds with one decimal, or a dash."""
    return "–" if v is None else f"{v:,.1f}"


def pct(v: float | None) -> str:
    """A share as a percentage with one decimal, or a dash."""
    return "–" if v is None else f"{100 * v:.1f} %"


def ci(lo: float | None, hi: float | None, fmt=ms) -> str:
    """An interval, or a dash."""
    return "–" if lo is None or hi is None else f"{fmt(lo)} to {fmt(hi)}"


def _value(group: Mapping[str, Any], metric: str, q: str) -> Mapping[str, Any] | None:
    return group["values_ms"].get(metric, {}).get(q)


def _ratio(group: Mapping[str, Any], metric: str) -> Mapping[str, Any] | None:
    return group["ratios"].get(metric)


def _count(r: Mapping[str, Any] | None) -> str:
    if not r:
        return "–"
    return f"{r['numerator']:g} of {r['n']:g}"


def _ratio_cell(r: Mapping[str, Any] | None, fmt=pct) -> str:
    if not r:
        return "–"
    return f"{fmt(r['value'])} ({_count(r)})"


def _value_cell(v: Mapping[str, Any] | None) -> str:
    return "–" if not v else ms(v["value"])


def _find(groups: Sequence[Mapping[str, Any]], **key: Any) -> Mapping[str, Any] | None:
    for g in groups:
        if all(g[k] == v for k, v in key.items()):
            return g
    return None


def _setup_key(item: Mapping[str, Any]) -> tuple:
    return tuple(item["setup"][k] for k in SETUP_KEYS)


def _flag(v: bool | None) -> str:
    return "not recorded" if v is None else ("on" if v else "off")


def _setup_title(setup: Mapping[str, Any]) -> str:
    pad = "not recorded" if setup["silence_pad_ms"] is None else f"{setup['silence_pad_ms']} ms"
    return (
        f"{setup['engine']} · silence pad {pad} · model retention {setup['model_retention']} · "
        f"warm-up {_flag(setup['warm_up'])} · pre-render {_flag(setup['pre_render'])} · "
        f"partial paint {_flag(setup['partial_paint'])} · paint capture "
        f"{_flag(setup['paint_capture'])}"
    )


def _not_attempted(g: Mapping[str, Any]) -> str:
    if g["tier2_basis"] == "run manifest":
        return "not attempted (the run manifest says Tier 2 was off)"
    return "not attempted (no Tier 2 activity in these traces)"


def _wilson_cells(w: Mapping[str, Any]) -> tuple[str, str, str]:
    """(estimate, interval, n) for a Wilson block: both bounds, labelled, since which one PRD
    §14's "Wilson upper bound" means is for the lead."""
    return (
        f"{pct(w['rate'])} ({w['k']} of {w['n']})",
        f"Wilson two-sided 95 % {ci(*w['two_sided_95'], fmt=pct)}; one-sided 95 % upper bound "
        f"{pct(w['one_sided_95_upper'])}",
        f"{w['n']}",
    )


def _target_cells(g: Mapping[str, Any], metric: str, q: float | None) -> tuple[str, str, str]:
    """(estimate, interval, n) for one target metric in group `g`."""
    if q is not None:
        v = _value(g, metric, f"p{round(q * 100)}")
        return (ms(v["value"]) + " ms", ci(*v["ci"]), f"{v['n']:g}") if v else ("–", "–", "0")
    if metric == "hit_tier2_3000" and not g["tier2_attempted"]:
        return _not_attempted(g), "–", "0"
    if metric == "wrong_paint_per_partial_paint":
        w = g["wilson"].get(metric)
        return _wilson_cells(w) if w else ("–", "–", "0")
    r = _ratio(g, metric)
    if not r:
        return "–", "–", "0"
    return pct(r["value"]), "bootstrap " + ci(*r["ci"], fmt=pct), _count(r)


def _label(doc: Mapping[str, Any], metric: str, q: float | None) -> str:
    labels = doc["labels"]
    if q is not None:
        return f"{labels['values'][metric]}, p{round(q * 100)}"
    return labels["ratios"][metric]


def _headline(doc: Mapping[str, Any], g: Mapping[str, Any]) -> list[str]:
    out = [
        "| Metric | Estimate | 95 % CI | n | PRD target |",
        "|---|---|---|---|---|",
    ]
    for t in doc["targets"]:
        if t["on"] != "idle":
            continue
        cells = _target_cells(g, t["metric"], t["quantile"])
        label = _label(doc, t["metric"], t["quantile"])
        out.append(f"| {label} | {cells[0]} | {cells[1]} | {cells[2]} | {t['target']} |")
    labels = doc["labels"]
    for metric in ("wer_raw", "wer_normalised", "place_non_pending", "still_in_speech_at_key_up"):
        r = _ratio(g, metric)
        cells = (pct(r["value"]), ci(*r["ci"], fmt=pct), _count(r)) if r else ("–", "–", "0")
        out.append(f"| {labels['ratios'][metric]} | {cells[0]} | {cells[1]} | {cells[2]} | |")
    for metric in ("partial_lag", "stable_lag"):
        for q in ("p50", "p95"):
            v = _value(g, metric, q)
            cells = (ms(v["value"]) + " ms", ci(*v["ci"]), f"{v['n']:g}") if v else ("–", "–", "0")
            out.append(
                f"| {labels['values'][metric]}, {q} | {cells[0]} | {cells[1]} | {cells[2]} | |"
            )
    return out


def _test_set_targets(
    doc: Mapping[str, Any], every: Mapping[str, Any] | None, idle: Mapping[str, Any] | None
) -> list[str]:
    out = [
        "| Metric | Trials | Estimate | 95 % interval | n | PRD target |",
        "|---|---|---|---|---|---|",
    ]
    for t in doc["targets"]:
        if t["on"] != "test set":
            continue
        label = _label(doc, t["metric"], t["quantile"])
        for name, g, target in (
            ("test split, every state", every, t["target"]),
            ("idle only, beside it", idle, ""),
        ):
            cells = _target_cells(g, t["metric"], t["quantile"]) if g else ("no trials", "–", "0")
            out.append(f"| {label} | {name} | {cells[0]} | {cells[1]} | {cells[2]} | {target} |")
            label = ""
    return out


def _hit_rates(doc: Mapping[str, Any], g: Mapping[str, Any]) -> list[str]:
    labels = doc["labels"]["ratios"]
    out = [
        "| Budget | Hit rate | 95 % CI | Hits | Denominator |",
        "|---|---|---|---|---|",
    ]
    for metric, denominator in doc["hit_rates"].items():
        if metric == "hit_tier2_3000" and not g["tier2_attempted"]:
            out.append(f"| {labels[metric]} | {_not_attempted(g)} | – | – | {denominator} |")
            continue
        r = _ratio(g, metric)
        cells = (pct(r["value"]), ci(*r["ci"], fmt=pct), _count(r)) if r else ("–", "–", "–")
        out.append(f"| {labels[metric]} | {cells[0]} | {cells[1]} | {cells[2]} | {denominator} |")
    return out


def _accuracy(doc: Mapping[str, Any], g: Mapping[str, Any]) -> list[str]:
    script = doc["script"]
    pending = ", ".join(script["owner_pending"]) or "none"
    text_no = ", ".join(script["text_level_no"]) or "none"
    lines = []
    for metric, how in (
        (
            "exact_non_pending",
            "**Exact place and time, without the owner-pending rows (D-057):** denominator = "
            "these trials of every row not tagged owner-pending",
        ),
        (
            "exact_settled",
            "Exact place and time on the settled rows only: denominator = the trials above "
            f"less those of the rows the parser gets wrong on the script's own text ({text_no})",
        ),
        (
            "exact_text_no",
            f"Rows the parser gets wrong on the script's own text ({text_no}), reported apart "
            "until the lead resolves them",
        ),
        (
            "exact_pending",
            f"Owner-pending rows ({pending}), reported apart until the owner rules",
        ),
        (
            "exact_or_soft_non_pending",
            "Exact or soft (a rolled flag, or a miss against the fallback), rows not owner-pending",
        ),
        ("place_non_pending", "Place right, rows not owner-pending"),
        ("place_settled", "Place right, settled rows"),
        ("place_pending", "Place right, owner-pending rows (apart)"),
        (
            "fallback",
            "Re-parse sent to the fallback (denominator: every re-parsed trial; an empty "
            "transcript is not re-parsed)",
        ),
    ):
        r = _ratio(g, metric)
        if r:
            lines.append(
                f"- {how}: {_count(r)} trials, {pct(r['value'])} (95 % CI {ci(*r['ci'], fmt=pct)})."
            )
        else:
            lines.append(f"- {how}: no trials.")
    return lines


def _wrong_paints(doc: Mapping[str, Any], g: Mapping[str, Any]) -> list[str]:
    labels = doc["labels"]["ratios"]
    lines = []
    for metric in ("wrong_paint_per_partial_paint", "wrong_paint_per_trial"):
        r, w = _ratio(g, metric), g["wilson"].get(metric)
        if not r or not w:
            lines.append(f"- {labels[metric]}: no trials.")
            continue
        lines.append(
            f"- {labels[metric]}: {w['k']} of {w['n']} ({pct(w['rate'])}); Wilson two-sided 95 % "
            f"{ci(*w['two_sided_95'], fmt=pct)}, one-sided 95 % upper bound "
            f"{pct(w['one_sided_95_upper'])}; cluster bootstrap 95 % CI "
            f"{ci(*r['ci'], fmt=pct)} (not the target's bound: it is 0 to 0 when no paint was "
            "wrong)."
        )
    for metric in ("wrong_final_paint", "partial_painted", "flips_per_query"):
        r = _ratio(g, metric)
        fmt = (lambda v: "–" if v is None else f"{v:.3f}") if metric == "flips_per_query" else pct
        lines.append(f"- {labels[metric]}: {_ratio_cell(r, fmt)}.")
    v50, v95 = _value(g, "visible_wrong", "p50"), _value(g, "visible_wrong", "p95")
    lines.append(
        f"- Visible-wrong time per wrong paint: p50 {_value_cell(v50)} ms, p95 "
        f"{_value_cell(v95)} ms; wrong paints never replaced within their trace: "
        f"{g['unreplaced_wrong_paints']}."
    )
    return lines


def _stages(doc: Mapping[str, Any], g: Mapping[str, Any]) -> list[str]:
    labels = doc["labels"]["values"]
    out = ["| Stage (ms) | n | p50 | 95 % CI | p95 | 95 % CI |", "|---|---|---|---|---|---|"]
    for metric, label in labels.items():
        v50, v95 = _value(g, metric, "p50"), _value(g, metric, "p95")
        if not v50:
            continue
        out.append(
            f"| {label} | {v50['n']:g} | {ms(v50['value'])} | {ci(*v50['ci'])} | "
            f"{ms(v95['value'])} | {ci(*v95['ci'])} |"
        )
    return out


def _states_table(groups: Sequence[Mapping[str, Any]]) -> list[str]:
    out = [
        "| State | Display | Trials | Tier 0 p95 (ms) | Tier 0 ≤ 300 ms | Release → final p95 "
        "(ms) | First partial after onset p50 (ms) | Exact, not owner-pending | WER, normalised | "
        "Wrong paints per partial paint |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    rank = {
        (s, d): i for i, (s, d) in enumerate((s, d) for s in STATE_ORDER for d in DISPLAY_ORDER)
    }
    for g in sorted(groups, key=lambda g: rank[(g["state"], g["display"])]):
        out.append(
            f"| {STATE_NAMES[g['state']]} | {g['display']} | {g['trials']} | "
            f"{_value_cell(_value(g, 'tier0', 'p95'))} | "
            f"{_ratio_cell(_ratio(g, 'hit_tier0_300'))} | "
            f"{_value_cell(_value(g, 'release_to_final', 'p95'))} | "
            f"{_value_cell(_value(g, 'first_partial_after_onset', 'p50'))} | "
            f"{_ratio_cell(_ratio(g, 'exact_non_pending'))} | "
            f"{_ratio_cell(_ratio(g, 'wer_normalised'))} | "
            f"{_ratio_cell(_ratio(g, 'wrong_paint_per_partial_paint'))} |"
        )
    return out


def _per_utterance(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    out = [
        "| Row | Split | Category | Trials | Exact | Place | WER, normalised | Tier 0 p50 (ms) | "
        "Release → final p50 (ms) | First partial p50 (ms) | Wrong paints | Still in speech "
        "at key-up | Context differed | Stage failed |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for u in rows:
        out.append(
            f"| {u['id']} | {u['split']} | {u['category']} | {u['trials']} | "
            f"{u['exact']} of {u['scored']} | {u['place']} of {u['scored']} | "
            f"{pct(u['wer_normalised'])} | {ms(u['tier0_p50_ms'])} | "
            f"{ms(u['release_to_final_p50_ms'])} | {ms(u['first_partial_after_onset_p50_ms'])} | "
            f"{u['wrong_partial_paints']} of {u['partial_paint_trials']} | "
            f"{u['still_in_speech_at_key_up'] or '–'} | {u['context_differed']} | "
            f"{u['failures']} |"
        )
    return out


def _counts(d: Mapping[str, int]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in d.items()) or "none"


def _parser_line(inputs: Mapping[str, Any]) -> str:
    sha = inputs.get("parser_sha256") or ""
    line = (
        f"- **Parser:** `{inputs.get('parser_binary')}`, SHA-256 `{sha[:12]}…`, "
        f"{inputs.get('parser_runs')} runs"
    )
    if inputs.get("parser_older_than_sources"):
        line += "; older than its sources: " + ", ".join(inputs["parser_older_than_sources"][:5])
    matches = inputs.get("parser_matches_run")
    if matches is True:
        line += "; the run's own parser"
    elif matches is False:
        run_sha = inputs.get("run_parser_sha256") or ""
        line += f"; **not the run's parser** (the run's is `{run_sha[:12]}…`)"
    else:
        line += "; the run manifest names no parser"
    return line + "."


def markdown(doc: Mapping[str, Any]) -> str:
    """The Markdown report for `doc` (module doc)."""
    prov = doc["provenance"]
    inputs, lines_, script = doc["inputs"], doc["lines"], doc["script"]
    out = ["# Snow Globe replay report", ""]
    banner = "**" + prov["statement"] + "**"
    out += [f"> {banner}", ""]
    manifest = prov.get("run_manifest") or {}
    if manifest.get("statement"):
        out += [f"> Run manifest: {manifest['statement']}", ""]
    if prov["kind"] != "measured":
        out += [
            "> The PRD targets are shown beside the numbers for orientation only: nothing here is "
            "assessed against them.",
            "",
        ]
    if lines_["trials_with_failures"]:
        out += [
            f"> **A stage of the scorer failed on {lines_['trials_with_failures']} trials** "
            f"({_counts(lines_['failures'])}). Those trials are left out of every figure the "
            "stage feeds, so the figures below are incomplete, and score.py exited 1.",
            "",
        ]
    if inputs.get("parser_matches_run") is False:
        out += [
            "> **Re-parsed with another parser than the run's build** (see Parser below): the "
            "accuracy figures are not the frozen build's.",
            "",
        ]
    out += [f"> Known bias: {doc['method']['known_bias']}.", ""]
    out += [
        f"- **Generated:** {doc['generated_at']} (report version {doc['report_version']}).",
        f"- **Script:** hash `{script['hash']}`, {script['rows']} rows. Owner-pending rows: "
        f"{', '.join(script['owner_pending']) or 'none'}. Rows the parser gets wrong on the "
        f"script's own text: {', '.join(script['text_level_no']) or 'none'}.",
        _parser_line(inputs),
        f"- **Method:** {doc['method']['bootstrap']}. Quantiles: {doc['method']['quantiles']}. "
        f"Wilson: {doc['method']['wilson']}. WER: {doc['method']['wer']}. Exact: "
        f"{doc['method']['exact']}. Paint time: {doc['method']['paint_time']}. Targets: "
        f"{doc['method']['targets']}.",
        f"- **Lines:** {lines_['read']} read; {lines_['not_replay']} not replay trials; rejected: "
        f"{_counts(lines_['rejected'])}; {lines_['trials']} trials scored; transcripts with no "
        f"trace line: {lines_['transcripts_without_trace']}. Unscorable parts: "
        f"{_counts(lines_['unscorable'])}. Data issues: {_counts(lines_['issues'])}. Stage "
        f"failures: {_counts(lines_['failures'])}.",
        "",
    ]
    groups = doc["groups"]
    setups: list[tuple] = []
    for g in groups:
        if _setup_key(g) not in setups:
            setups.append(_setup_key(g))
    for key in setups:
        mine = [g for g in groups if _setup_key(g) == key]
        out += [f"## Setup: {_setup_title(mine[0]['setup'])}", ""]
        head = _find(mine, split="test", state="idle", display="all", subset="all rows")
        every = _find(mine, split="test", state="all", display="all", subset="all rows")
        if head is None:
            out += ["No idle trials on the test split: there is no headline for this setup.", ""]
        else:
            out += [
                "### Headline: idle trials, test split",
                "",
                f"{head['trials']} trials of {head['utterances']} utterances "
                f"({head['recordings']} takes) over {head['sessions']} sessions; bootstrap by "
                f"{head['bootstrap']['method']}. {head['display_asleep_trials']} began with the "
                "display asleep (D-068; they are inside these totals and also apart below).",
                "",
                *_headline(doc, head),
                "",
            ]
        if every is not None:
            out += [
                "### Targets on the test set: every state, idle beside",
                "",
                f"{every['trials']} trials of {every['utterances']} utterances over "
                f"{every['sessions']} sessions. PRD §14 sets these on the replay test set, not on "
                "idle trials alone.",
                "",
                *_test_set_targets(doc, every, head),
                "",
            ]
        if head is not None:
            out += ["### Budget hit rates: idle, test split", "", *_hit_rates(doc, head), ""]
        for title, g in (("test split, every state", every), ("idle, test split", head)):
            if g is None:
                continue
            out += [
                f"### Accuracy: {title}",
                "",
                *_accuracy(doc, g),
                "",
                f"### Wrong paints: {title}",
                "",
                *_wrong_paints(doc, g),
                "",
            ]
        if head is not None:
            out += ["### Stages: idle, test split", "", *_stages(doc, head), ""]
        for split in ("test", "dev"):
            rows = [g for g in mine if g["split"] == split and g["subset"] == "all rows"]
            if not rows:
                continue
            title = "test split" if split == "test" else "dev split (development rows only)"
            out += [f"### By state and display: {title}", "", *_states_table(rows), ""]
        follow = [g for g in mine if g["subset"] == "follow-ups"]
        alls = [g for g in mine if g["state"] == "all" and g["display"] == "all"]
        if follow or alls:
            out += ["### Follow-ups", ""]
            for g in follow:
                out.append(
                    f"- Idle follow-ups, {g['split']} split (D-068): exact "
                    f"{_ratio_cell(_ratio(g, 'exact_non_pending'))}; live context not the "
                    f"script's: {_ratio_cell(_ratio(g, 'context_differed'))}; why: "
                    f"{_counts(g['context_differed_by_reason'])}."
                )
            for g in alls:
                out.append(
                    f"- All states, {g['split']} split: trials whose context differed, by "
                    f"reason: {_counts(g['context_differed_by_reason'])}."
                )
            out.append("")
        mine_rows = [u for u in doc["per_utterance"] if _setup_key(u) == key]
        if mine_rows:
            out += [
                "### Per utterance (every state)",
                "",
                *_per_utterance(mine_rows),
                "",
            ]
    return "\n".join(out).rstrip() + "\n"
