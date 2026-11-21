//! `m1-report`: reads M1 1a spike runs (PLAN.md M1 1a) without touching any device.
//!
//! - `m1-report show <run-id|path> [--write]` prints every check's verdict; `--write` appends the
//!   cross-process verdicts (check 9 `inject`, check 8 `sck-tag-join`) to the run file.
//! - `m1-report list` lists the runs under `$SNOW_GLOBE_DATA_DIR/spikes/m1/`.
//! - `m1-report note <run-id> <check> key=value...` appends an owner observation, for example
//!   `m1-report note <run> consent prompt_seen=false scenario=after-reboot`.
//! - `m1-report checks` prints the eleven checks, criteria and permissions.

use std::path::PathBuf;
use std::process::ExitCode;

use m1_spike_common::args::Args;
use m1_spike_common::checks::{CHECKS, STOP_RULE};
use m1_spike_common::jsonl::{RunLog, Status, read_run};
use m1_spike_common::{paths, report};

const USAGE: &str = "usage:
  m1-report show <run-id|path> [--write]
  m1-report list
  m1-report note <run-id> <check> key=value [key=value...]
  m1-report checks";

/// A run id, or an existing `.jsonl` path. Writes through either go through `RunLog::open_at`,
/// which refuses a path inside the repository.
fn resolve(run: &str) -> Result<(PathBuf, String), String> {
    let p = PathBuf::from(run);
    if p.extension().is_some_and(|e| e == "jsonl") && p.exists() {
        let id = p
            .file_stem()
            .and_then(|s| s.to_str())
            .ok_or("bad file name")?
            .to_string();
        return Ok((p, id));
    }
    Ok((paths::run_path(run)?, run.to_string()))
}

fn run() -> Result<ExitCode, String> {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let args = Args::parse(argv.clone(), &["write"])?;
    match args.command.as_deref() {
        Some("show") => {
            let write = args.flag("write");
            args.finish()?;
            let run = args.positional.first().ok_or(USAGE)?;
            let (path, run_id) = resolve(run)?;
            let (recs, bad) = read_run(&path).map_err(|e| format!("{}: {e}", path.display()))?;
            print!("{}", report::render(&recs, bad));
            if write {
                let log = RunLog::open_at(&path, &run_id, "report", "m1-report", &argv)
                    .map_err(|e| e.to_string())?;
                for (slug, status, criterion, v) in report::join_results(&recs) {
                    log.result(&slug, status, &criterion, v)
                        .map_err(|e| e.to_string())?;
                    println!("wrote {slug}: {status:?}");
                }
            }
            let any_fail = report::latest_results(&recs)
                .values()
                .any(|r| r.status == Some(Status::Fail))
                || report::join_results(&recs)
                    .iter()
                    .any(|j| j.1 == Status::Fail);
            Ok(if any_fail {
                ExitCode::from(1)
            } else {
                ExitCode::SUCCESS
            })
        }
        Some("list") => {
            args.finish()?;
            let dir = paths::spike_dir()?;
            let mut entries: Vec<_> = std::fs::read_dir(&dir)
                .map_err(|e| format!("{}: {e}", dir.display()))?
                .filter_map(Result::ok)
                .map(|e| e.path())
                .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
                .collect();
            entries.sort();
            for p in entries {
                let (recs, _) = read_run(&p).map_err(|e| e.to_string())?;
                let verdicts: Vec<String> = report::latest_results(&recs)
                    .values()
                    .map(|r| format!("{}={:?}", r.name, r.status.unwrap_or(Status::Incomplete)))
                    .collect();
                println!(
                    "{}  {}",
                    p.file_stem().and_then(|s| s.to_str()).unwrap_or("?"),
                    verdicts.join(" ")
                );
            }
            Ok(ExitCode::SUCCESS)
        }
        Some("note") => {
            args.finish()?;
            let [run, check, pairs @ ..] = args.positional.as_slice() else {
                return Err(USAGE.into());
            };
            if pairs.is_empty() {
                return Err(USAGE.into());
            }
            let (path, run_id) = resolve(run)?;
            if !path.exists() {
                return Err(format!("{} does not exist", path.display()));
            }
            let v = report::normalise_note(check, report::note_values(pairs)?)?;
            let log = RunLog::open_at(&path, &run_id, check, "m1-report", &argv)
                .map_err(|e| e.to_string())?;
            log.note(check, v).map_err(|e| e.to_string())?;
            println!("noted in {}", path.display());
            Ok(ExitCode::SUCCESS)
        }
        Some("checks") => {
            args.finish()?;
            for c in CHECKS {
                println!(
                    "{:>2}. {}\n    PLAN: {}\n    pass: {}\n    permission: {}\n    verdict by: {}\n",
                    c.n, c.slug, c.plan_item, c.criterion, c.permission, c.verdict_by
                );
            }
            println!("{STOP_RULE}");
            Ok(ExitCode::SUCCESS)
        }
        _ => Err(USAGE.into()),
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(code) => code,
        Err(e) => {
            eprintln!("m1-report: {e}");
            ExitCode::from(2)
        }
    }
}
