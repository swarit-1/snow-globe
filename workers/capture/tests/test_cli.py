"""The console script: argument parsing and the network-free ``status`` command."""

from __future__ import annotations

import json

import pytest

from lookout_capture.cli import build_parser, main


def test_parser_knows_every_command():
    parser = build_parser()
    assert parser.parse_args(["once", "--only", "point-reyes.beach"]).only == ["point-reyes.beach"]
    loop = parser.parse_args(["loop", "--interval", "300"])
    assert loop.interval == 300 and loop.jitter == 30
    day = parser.parse_args(
        [
            "archive",
            "day",
            "point-reyes.beach",
            "2026-11-17",
            "--daytime-only",
            "--hires",
            "--dry-run",
        ]
    )
    assert day.dates == ["2026-11-17"] and day.hires and day.daytime_only and day.dry_run
    assert day.max_gb == 1.0 and day.pick == "all"
    sample = parser.parse_args(
        ["archive", "sample", "grand-canyon.yavapai", "2024-07-15", "2025-07-15"]
    )
    assert sample.pick == "midday" and len(sample.dates) == 2
    check = parser.parse_args(["archive-check", "--viewpoint", "acadia.mcfarland"])
    assert check.viewpoint == ["acadia.mcfarland"] and check.budget is None
    with pytest.raises(SystemExit):
        parser.parse_args(["archive", "day", "point-reyes.beach"])  # the date is required


def test_storage_report_is_an_alias_of_storage(data_root, capsys):
    """CLAUDE.md lists the offline command as ``storage-report``; both names run it."""
    parser = build_parser()
    for name in ("storage", "storage-report"):
        args = parser.parse_args([name, "--json"])
        assert args.func.__name__ == "_cmd_storage" and args.json
        assert not getattr(args, "nps_fetcher", False)  # no network, no fetcher lock
    assert main(["storage-report", "--json"]) == 0
    assert "frames_span_utc" in json.loads(capsys.readouterr().out)


def test_status_runs_without_network(data_root, capsys):
    assert main(["status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["cameras"]) == 8
    assert all(row.get("state") == "none" for row in report["cameras"])


def test_archive_commands_refuse_the_rainier_park_camera(data_root):
    with pytest.raises(SystemExit, match="no NPS archive"):
        main(["archive", "days", "rainier.paradise-mountain"])


def test_archive_downloads_past_the_held_out_guard_are_refused_before_any_request(
    data_root, monkeypatch
):
    import lookout_capture.archive as archive

    def no_client(*args, **kwargs):  # the refusal comes before any client exists
        raise AssertionError("an archive client was built")

    monkeypatch.setattr(archive, "default_archive_client", no_client)
    with pytest.raises(SystemExit, match="held-out guard"):
        main(["archive", "day", "point-reyes.beach", "2026-07-15"])
    with pytest.raises(SystemExit, match="2026-11-17 is on or after 2026-06-01"):
        main(["archive", "sample", "point-reyes.beach", "2025-07-15", "2026-11-17", "--hires"])
    with pytest.raises(SystemExit, match="written reason"):
        main(["archive", "day", "point-reyes.beach", "2026-07-15", "--held-out-override", "x"])
    args = build_parser().parse_args(
        ["archive", "day", "point-reyes.beach", "2026-07-15", "--held-out-override", "scoring M4"]
    )
    assert args.held_out_override == "scoring M4" and args.nps_fetcher is True
    assert not getattr(build_parser().parse_args(["status"]), "nps_fetcher", False)
