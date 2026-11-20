//! Pure verdicts for `m1-sck` (PLAN.md M1 1a checks 8 and 10), unit-tested without a capture.
//! Check 11's verdict lives in `m1_spike_common::consent`, so `m1-report` can redo it.

use m1_spike_common::clock::{InstantAgreement, InstantIdentity};
use m1_spike_common::jsonl::Status;
use m1_spike_common::stats::{linear_fit, median_pair_slope, summarize};
use m1_spike_common::tag::{Tag, TagError};
use serde::Serialize;
use serde_json::{Value, json};

/// What one captured frame told us.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct FrameRec {
    pub complete: bool,
    /// Presentation time converted to ns (host clock if ScreenCaptureKit uses it).
    pub pts_ns: Option<i64>,
    /// `SCStreamFrameInfo.displayTime`, mach ticks converted to ns.
    pub display_ns: Option<u64>,
    /// `CLOCK_UPTIME_RAW` when the frame reached the handler.
    pub arrival_ns: u64,
    /// `Instant::now()` at arrival, mapped onto the host clock through the process origin.
    pub instant_host_ns: u64,
    pub tag: Option<Tag>,
    pub tag_err: Option<TagError>,
    /// The pixel buffer has the configured size. ScreenCaptureKit scales into the configured
    /// size, so this is a sanity check on the buffer, not evidence of window-only capture.
    pub buffer_matches_config: bool,
    /// `SCStreamFrameInfo.contentRect` (x, y, width, height), as ScreenCaptureKit reports it.
    pub content_rect: Option<[f64; 4]>,
    /// `SCStreamFrameInfo.contentScale` and `.scaleFactor`.
    pub content_scale: Option<f64>,
    pub scale_factor: Option<f64>,
    /// `SCStreamFrameInfo.screenRect`.
    pub screen_rect: Option<[f64; 4]>,
}

/// Window-only capture facts gathered before the stream started.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Target {
    pub window_id: u32,
    pub title: String,
    pub owner_pid: Option<i32>,
    pub filter_style: String,
    pub width_px: u32,
    pub height_px: u32,
    pub point_pixel_scale: f64,
    /// `SCWindow.frame` in points (x, y, width, height).
    pub window_frame_pt: [f64; 4],
}

/// Required decode rate over complete frames (check 8).
pub const MIN_DECODE_RATE: f64 = 0.99;
/// Frames needed before either verdict counts.
pub const MIN_FRAMES: usize = 10;
/// The median presentation-to-arrival lag must lie in 0..=this many ms (check 10); frames
/// beyond it are reported as late, not failed (one scheduling stall is not a clock mismatch).
pub const MAX_LAG_MS: f64 = 250.0;
/// A frame arriving this long before its presentation time cannot be on the same clock.
pub const NEGATIVE_LAG_TOLERANCE_MS: f64 = 1.0;
/// Arrival time against presentation time must have a slope of 1 within this.
pub const SLOPE_TOLERANCE: f64 = 0.01;

/// How a frame's `contentRect` compares with the captured window (informational: Apple does
/// not document the rectangle's unit for a window capture, so this run's values settle it).
/// TODO(M1): once the owner's check 8 run shows the unit, make the match a criterion here and
/// in the M1 harness.
fn content_vs_window(r: &[f64; 4], target: &Target) -> &'static str {
    let (w, h) = (r[2], r[3]);
    let (wp, hp) = (target.window_frame_pt[2], target.window_frame_pt[3]);
    let (wx, hx) = (f64::from(target.width_px), f64::from(target.height_px));
    if (w - wp).abs() <= 1.0 && (h - hp).abs() <= 1.0 {
        "window_size_in_points"
    } else if (w - wx).abs() <= 2.0 && (h - hx).abs() <= 2.0 {
        "window_size_in_pixels"
    } else {
        "other"
    }
}

/// Check 8 (ScreenCaptureKit half): a desktop-independent window filter, and the tag readable
/// at the frame's top-left corner in complete frames. Frames before the first decodable tag are
/// a warm-up (the page has not drawn yet) and are counted separately. Decoding at the frame's
/// origin is the evidence that the frame starts at the panel's corner: in a display capture the
/// panel sits elsewhere in the frame. `contentRect` against the window frame is reported.
pub fn sck_tag(frames: &[FrameRec], target: &Target) -> (Status, Value) {
    let complete: Vec<&FrameRec> = frames.iter().filter(|f| f.complete).collect();
    let first_tag = complete.iter().position(|f| f.tag.is_some());
    let judged: &[&FrameRec] = match first_tag {
        Some(i) => &complete[i..],
        None => &complete[..0],
    };
    let decoded = judged.iter().filter(|f| f.tag.is_some()).count();
    let rate = if judged.is_empty() {
        0.0
    } else {
        decoded as f64 / judged.len() as f64
    };
    let buffer_mismatch = complete.iter().filter(|f| !f.buffer_matches_config).count();
    let window_style = target.filter_style.to_ascii_lowercase().contains("window");
    let mut distinct: Vec<Tag> = judged.iter().filter_map(|f| f.tag).collect();
    distinct.sort();
    distinct.dedup();
    let mut content: std::collections::BTreeMap<&str, usize> = Default::default();
    let mut content_sizes: Vec<[f64; 2]> = Vec::new();
    for f in &complete {
        let label = match &f.content_rect {
            Some(r) => {
                if !content_sizes.contains(&[r[2], r[3]]) && content_sizes.len() < 8 {
                    content_sizes.push([r[2], r[3]]);
                }
                content_vs_window(r, target)
            }
            None => "absent",
        };
        *content.entry(label).or_default() += 1;
    }
    let scales: Vec<f64> = complete.iter().filter_map(|f| f.content_scale).collect();
    let mut reasons = Vec::new();
    if complete.len() < MIN_FRAMES {
        reasons.push(format!("only {} complete frame(s)", complete.len()));
    }
    if first_tag.is_none() {
        reasons.push("no frame carried a readable tag".into());
    } else if rate < MIN_DECODE_RATE {
        reasons.push(format!(
            "tag read in {:.1} % of complete frames, need 99 %",
            rate * 100.0
        ));
    }
    if !window_style {
        reasons.push(format!(
            "filter style is {:?}, not a window",
            target.filter_style
        ));
    }
    let status = if complete.len() < MIN_FRAMES {
        Status::Incomplete
    } else {
        Status::from_bool(reasons.is_empty())
    };
    let errors = |e: TagError| judged.iter().filter(|f| f.tag_err == Some(e)).count();
    (
        status,
        json!({
            "target": target,
            "frames": frames.len(),
            "complete_frames": complete.len(),
            "warm_up_frames": first_tag.unwrap_or(complete.len()),
            "judged_frames": judged.len(),
            "decoded": decoded,
            "decode_rate": rate,
            "distinct_tags": distinct.len(),
            "errors": {"no_sync": errors(TagError::NoSync), "bad_crc": errors(TagError::BadCrc), "too_small": errors(TagError::TooSmall)},
            "buffer_size_mismatch": buffer_mismatch,
            "content_rect_vs_window": content,
            "content_rect_sizes": content_sizes,
            "content_scale": summarize(&scales),
            "reasons": reasons,
        }),
    )
}

/// Check 10: `Instant` is the host clock, and frame presentation times are on it too: no frame
/// arrives before its presentation time, the median lag is 0-250 ms, and arrival against
/// presentation time has a slope of 1. Frames later than 250 ms are counted, not failed.
pub fn clock(
    frames: &[FrameRec],
    identity: &InstantIdentity,
    agreement: Option<&InstantAgreement>,
) -> (Status, Value) {
    let complete: Vec<&FrameRec> = frames
        .iter()
        .filter(|f| f.complete && f.pts_ns.is_some())
        .collect();
    let lags: Vec<f64> = complete
        .iter()
        .filter_map(|f| {
            f.pts_ns
                .map(|p| (f.instant_host_ns as f64 - p as f64) / 1e6)
        })
        .collect();
    let lag = summarize(&lags);
    let negative = lags
        .iter()
        .filter(|l| **l < -NEGATIVE_LAG_TOLERANCE_MS)
        .count();
    let late = lags.iter().filter(|l| **l > MAX_LAG_MS).count();
    let display_minus_pts: Vec<f64> = complete
        .iter()
        .filter_map(|f| match (f.display_ns, f.pts_ns) {
            (Some(d), Some(p)) => Some((d as f64 - p as f64) / 1e6),
            _ => None,
        })
        .collect();
    let xs: Vec<f64> = complete
        .iter()
        .filter_map(|f| f.pts_ns.map(|p| p as f64))
        .collect();
    let ys: Vec<f64> = complete.iter().map(|f| f.instant_host_ns as f64).collect();
    // The verdict uses the outlier-proof slope; the least-squares one is reported beside it.
    let slope = median_pair_slope(&xs, &ys);
    let lsq_slope = linear_fit(&xs, &ys).map(|(m, _)| m);
    let arrival_vs_instant: Vec<f64> = frames
        .iter()
        .map(|f| (f.instant_host_ns as f64 - f.arrival_ns as f64) / 1e3)
        .collect();
    let mut reasons = Vec::new();
    let identity_ok = identity.verdict == "uptime_raw" || identity.verdict == "indistinguishable";
    if !identity_ok {
        reasons.push(format!(
            "Instant is not CLOCK_UPTIME_RAW: {}",
            identity.verdict
        ));
    }
    match agreement {
        Some(a) if a.drift_ns.unsigned_abs() > a.max_bracket_ns + 1_000 => reasons.push(format!(
            "Instant drifted {} ns from the host clock over {:.0} ms",
            a.drift_ns, a.span_ms
        )),
        None => reasons.push("no Instant/host paired reads".into()),
        _ => {}
    }
    if negative > 0 {
        reasons.push(format!(
            "{negative} of {} frame(s) arrived more than {NEGATIVE_LAG_TOLERANCE_MS} ms before \
             their presentation time (a different clock)",
            lags.len()
        ));
    }
    if let Some(l) = &lag
        && !(0.0..=MAX_LAG_MS).contains(&l.p50)
    {
        reasons.push(format!(
            "median presentation-to-arrival lag is {:.1} ms, outside 0-{MAX_LAG_MS} ms",
            l.p50
        ));
    }
    match slope {
        Some(m) if (m - 1.0).abs() > SLOPE_TOLERANCE => reasons.push(format!(
            "arrival against presentation time has slope {m:.4}, not 1 (a different clock rate)"
        )),
        None if complete.len() >= MIN_FRAMES => {
            reasons.push("no slope: the presentation times do not vary".into());
        }
        _ => {}
    }
    let status = if complete.len() < MIN_FRAMES {
        reasons.push(format!(
            "only {} complete frame(s) with a timestamp",
            complete.len()
        ));
        Status::Incomplete
    } else {
        Status::from_bool(reasons.is_empty())
    };
    (
        status,
        json!({
            "instant_identity": identity,
            "instant_agreement": agreement,
            "frames": complete.len(),
            "pts_to_arrival_ms": lag,
            "negative_lag_frames": negative,
            "late_frames": late,
            "late_fraction": if lags.is_empty() { 0.0 } else { late as f64 / lags.len() as f64 },
            "display_time_minus_pts_ms": summarize(&display_minus_pts),
            "arrival_slope_vs_pts": slope,
            "arrival_slope_vs_pts_least_squares": lsq_slope,
            "instant_minus_uptime_at_arrival_us": summarize(&arrival_vs_instant),
            "identity_note": if identity.verdict == "indistinguishable" {
                "the machine has not slept since boot, so CLOCK_UPTIME_RAW and CLOCK_MONOTONIC_RAW are equal; rerun after a sleep for a conclusive identity"
            } else { "" },
            "reasons": reasons,
        }),
    )
}

/// The parts of `codesign -dvvv` output that Screen Recording consent depends on (PRD §18.1):
/// Identifier, CDHash and the Signature kind. The signing certificate's subject
/// (`Authority=Apple Development: <name> (<team>)`) and the TeamIdentifier name a person, so
/// only whether a developer certificate signed the binary and the certificate's kind (the text
/// before the colon) are kept.
pub fn codesign_summary(text: &str) -> Value {
    let values = |key: &str| -> Vec<String> {
        text.lines()
            .filter_map(|l| l.strip_prefix(&format!("{key}=")))
            .map(str::to_string)
            .collect()
    };
    let authorities = values("Authority");
    let team = values("TeamIdentifier");
    // `Signature=adhoc` for ad-hoc code, `Signature size=4797` for certificate-signed code.
    let signature: Vec<String> = text
        .lines()
        .filter_map(|l| {
            l.strip_prefix("Signature=")
                .or_else(|| l.strip_prefix("Signature "))
        })
        .map(str::to_string)
        .collect();
    let leaf_kind = authorities
        .first()
        .map(|a| a.split(':').next().unwrap_or("").trim().to_string());
    json!({
        "identifier": values("Identifier"),
        "cdhash": values("CDHash"),
        "signature": signature,
        "developer_signed": !authorities.is_empty(),
        "certificate_kind": leaf_kind,
        "team_id_present": team.iter().any(|t| t != "not set"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use m1_spike_common::clock::classify_identity;

    fn frame(k: u64, tag: Option<Tag>, lag_ms: f64) -> FrameRec {
        let pts = 1_000_000_000 + k * 16_666_667;
        FrameRec {
            complete: true,
            pts_ns: Some(pts as i64),
            display_ns: Some(pts + 1_000),
            arrival_ns: pts + (lag_ms * 1e6) as u64,
            instant_host_ns: pts + (lag_ms * 1e6) as u64 + 50,
            tag,
            tag_err: if tag.is_none() {
                Some(TagError::NoSync)
            } else {
                None
            },
            buffer_matches_config: true,
            content_rect: Some([0.0, 0.0, 480.0, 300.0]),
            content_scale: Some(1.0),
            scale_factor: Some(2.0),
            screen_rect: None,
        }
    }

    fn target() -> Target {
        Target {
            window_id: 7,
            title: "Lookout M1 spike panel".into(),
            owner_pid: Some(42),
            filter_style: "Ok(Window)".into(),
            width_px: 960,
            height_px: 600,
            point_pixel_scale: 2.0,
            window_frame_pt: [1000.0, 60.0, 480.0, 300.0],
        }
    }

    #[test]
    fn tag_check_ignores_warm_up_frames_and_needs_99_percent() {
        let mut frames: Vec<FrameRec> = (0..3).map(|k| frame(k, None, 8.0)).collect();
        frames.extend((3..203).map(|k| {
            frame(
                k,
                Some(Tag {
                    gen_: (k / 15) as u16,
                    tier: 0,
                }),
                8.0,
            )
        }));
        let (s, v) = sck_tag(&frames, &target());
        assert_eq!(s, Status::Pass, "{v}");
        assert_eq!(v["warm_up_frames"], 3);
        assert_eq!(v["content_rect_vs_window"]["window_size_in_points"], 203);
        // Three misreads in 200 judged frames is 98.5 %: fail.
        for k in [50, 90, 150] {
            frames[k].tag = None;
            frames[k].tag_err = Some(TagError::BadCrc);
        }
        assert_eq!(sck_tag(&frames, &target()).0, Status::Fail);
    }

    #[test]
    fn tag_check_needs_a_window_filter_and_enough_frames() {
        let frames: Vec<FrameRec> = (0..20)
            .map(|k| frame(k, Some(Tag { gen_: 1, tier: 1 }), 8.0))
            .collect();
        let mut t = target();
        t.filter_style = "Ok(Display)".into();
        assert_eq!(sck_tag(&frames, &t).0, Status::Fail);
        assert_eq!(sck_tag(&frames[..5], &target()).0, Status::Incomplete);
    }

    /// The reviewer's case: the buffer-size check could never fail, so it is reported, and the
    /// window geometry ScreenCaptureKit reports per frame is compared with the window instead
    /// (informational until a real run shows the rectangle's unit).
    #[test]
    fn content_rect_is_compared_with_the_window_and_reported() {
        let mut frames: Vec<FrameRec> = (0..20)
            .map(|k| frame(k, Some(Tag { gen_: 1, tier: 1 }), 8.0))
            .collect();
        for f in &mut frames[..5] {
            f.content_rect = Some([0.0, 0.0, 960.0, 600.0]);
        }
        frames[5].content_rect = Some([0.0, 0.0, 1512.0, 982.0]);
        frames[6].content_rect = None;
        let (s, v) = sck_tag(&frames, &target());
        assert_eq!(s, Status::Pass, "{v}");
        let c = &v["content_rect_vs_window"];
        assert_eq!(
            (
                &c["window_size_in_pixels"],
                &c["other"],
                &c["absent"],
                &c["window_size_in_points"]
            ),
            (&json!(5), &json!(1), &json!(1), &json!(13))
        );
        assert_eq!(v["content_rect_sizes"].as_array().map(Vec::len), Some(3));
    }

    fn identity_and_agreement() -> (InstantIdentity, InstantAgreement) {
        (
            classify_identity(Some(5_000_000_000), 5_000_000_100, 9_000_000_000_000),
            InstantAgreement {
                samples: 100,
                span_ms: 20_000.0,
                offset_min_ns: -100,
                offset_max_ns: 150,
                drift_ns: 80,
                max_bracket_ns: 400,
            },
        )
    }

    #[test]
    fn clock_check_accepts_host_time_and_rejects_another_epoch() {
        let (id, agreement) = identity_and_agreement();
        let frames: Vec<FrameRec> = (0..60).map(|k| frame(k, None, 12.0)).collect();
        let (s, v) = clock(&frames, &id, Some(&agreement));
        assert_eq!(s, Status::Pass, "{v}");
        let slope = v["arrival_slope_vs_pts"].as_f64().expect("slope");
        assert!((slope - 1.0).abs() < 1e-6);
        // Presentation times on a clock 10 s ahead of the host clock.
        let shifted: Vec<FrameRec> = frames
            .iter()
            .map(|f| FrameRec {
                pts_ns: f.pts_ns.map(|p| p + 10_000_000_000),
                ..f.clone()
            })
            .collect();
        let (s, v) = clock(&shifted, &id, Some(&agreement));
        assert_eq!(s, Status::Fail);
        assert_eq!(v["negative_lag_frames"], 60);
        // ...or 10 s behind it.
        let behind: Vec<FrameRec> = frames
            .iter()
            .map(|f| FrameRec {
                pts_ns: f.pts_ns.map(|p| p - 10_000_000_000),
                ..f.clone()
            })
            .collect();
        let (s, v) = clock(&behind, &id, Some(&agreement));
        assert_eq!(s, Status::Fail);
        assert!(v["reasons"].to_string().contains("median"), "{v}");
        let bad_id = classify_identity(Some(9_000_000_000_000), 5_000_000_100, 9_000_000_000_000);
        assert_eq!(clock(&frames, &bad_id, Some(&agreement)).0, Status::Fail);
    }

    /// The reviewer's case: one frame stalled past 250 ms tripped the stop rule although the
    /// clocks agree. It is now counted as late and reported. The stall is put last in a short
    /// capture (20 frames), where it would also drag a least-squares slope off 1.
    #[test]
    fn one_late_frame_is_reported_not_failed() {
        let (id, agreement) = identity_and_agreement();
        let mut frames: Vec<FrameRec> = (0..20).map(|k| frame(k, None, 12.0)).collect();
        frames[19] = frame(19, None, 400.0);
        let (s, v) = clock(&frames, &id, Some(&agreement));
        assert_eq!(s, Status::Pass, "{v}");
        assert_eq!(v["late_frames"], 1);
        assert_eq!(v["negative_lag_frames"], 0);
        let lsq = v["arrival_slope_vs_pts_least_squares"]
            .as_f64()
            .expect("lsq slope");
        assert!((lsq - 1.0).abs() > SLOPE_TOLERANCE, "{lsq}");
        let robust = v["arrival_slope_vs_pts"].as_f64().expect("slope");
        assert!((robust - 1.0).abs() < 1e-6, "{robust}");
    }

    #[test]
    fn a_clock_running_at_another_rate_fails_on_the_slope() {
        let (id, agreement) = identity_and_agreement();
        // Presentation times in mach ticks read as ns (24 MHz vs 1 GHz): slope 41.67.
        let frames: Vec<FrameRec> = (0..60)
            .map(|k| {
                let mut f = frame(k, None, 12.0);
                f.pts_ns = f.pts_ns.map(|p| p * 3 / 125);
                f
            })
            .collect();
        let (s, v) = clock(&frames, &id, Some(&agreement));
        assert_eq!(s, Status::Fail);
        assert!(v["reasons"].to_string().contains("slope"), "{v}");
    }

    /// The reviewer's case: `Authority=Apple Development: <name> (<team>)` and the team id went
    /// into the run file. Only the certificate kind and booleans are kept.
    #[test]
    fn codesign_summary_keeps_no_name_or_team_id() {
        let text = "Executable=/Users/someone/lookout-data/cache/spike-m1/M1SckProbe.app/Contents/MacOS/m1-sck\n\
            Identifier=dev.lookout.spike-m1.sck\n\
            Format=app bundle with Mach-O thin (arm64)\n\
            CodeDirectory v=20500 size=4797 flags=0x10000(runtime) hashes=139+7 location=embedded\n\
            CDHash=0123456789abcdef0123456789abcdef01234567\n\
            Signature size=4797\n\
            Authority=Apple Development: Jane Example (ABCDE12345)\n\
            Authority=Apple Worldwide Developer Relations Certification Authority\n\
            Authority=Apple Root CA\n\
            TeamIdentifier=ABCDE12345\n";
        let v = codesign_summary(text);
        let s = v.to_string();
        assert!(
            !s.contains("Jane") && !s.contains("ABCDE12345") && !s.contains("someone"),
            "{s}"
        );
        assert_eq!(v["certificate_kind"], "Apple Development");
        assert_eq!(v["developer_signed"], true);
        assert_eq!(v["team_id_present"], true);
        assert_eq!(v["identifier"][0], "dev.lookout.spike-m1.sck");
        assert_eq!(v["signature"][0], "size=4797");
        // The fields of the release m1-sck built on 19 Nov 2026 (linker-signed, ad hoc).
        let adhoc = codesign_summary(
            "Identifier=m1_sck-134dd38d304dc6fd\n\
             CodeDirectory v=20400 size=15760 flags=0x20002(adhoc,linker-signed) hashes=489+0 location=embedded\n\
             CDHash=ff00\nSignature=adhoc\nTeamIdentifier=not set\n",
        );
        assert_eq!(adhoc["developer_signed"], false);
        assert_eq!(adhoc["team_id_present"], false);
        assert_eq!(adhoc["certificate_kind"], Value::Null);
        assert_eq!(adhoc["signature"][0], "adhoc");
    }
}
