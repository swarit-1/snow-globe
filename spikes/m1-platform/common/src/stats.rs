//! Small statistics for the spike's timing numbers (PRD §10.4, §10.6 report p50 and p95).
//!
//! Percentiles use the nearest-rank method: the p-th percentile of n sorted values is the value
//! at rank ceil(p/100 * n). With the spike's small trial counts this is conservative (it never
//! interpolates below an observed value), and every summary also reports n, min and max.

use serde::Serialize;

/// n, min, p50, p95, max and mean of a set of values.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Summary {
    pub n: usize,
    pub min: f64,
    pub p50: f64,
    pub p95: f64,
    pub max: f64,
    pub mean: f64,
}

/// Nearest-rank percentile of already sorted values; `p` in 0..=100. Returns NaN when empty.
pub fn percentile_sorted(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let n = sorted.len();
    let rank = ((p.clamp(0.0, 100.0) / 100.0) * n as f64).ceil() as usize;
    sorted[rank.clamp(1, n) - 1]
}

/// Summarises finite values; NaN and infinities are dropped. `None` when nothing is left.
pub fn summarize(values: &[f64]) -> Option<Summary> {
    let mut v: Vec<f64> = values.iter().copied().filter(|x| x.is_finite()).collect();
    if v.is_empty() {
        return None;
    }
    v.sort_by(f64::total_cmp);
    let n = v.len();
    let mean = v.iter().sum::<f64>() / n as f64;
    Some(Summary {
        n,
        min: v[0],
        p50: percentile_sorted(&v, 50.0),
        p95: percentile_sorted(&v, 95.0),
        max: v[n - 1],
        mean,
    })
}

/// Successive differences of timestamps in nanoseconds, as milliseconds.
pub fn intervals_ms(ts_ns: &[u64]) -> Vec<f64> {
    ts_ns
        .windows(2)
        .map(|w| (w[1] as f64 - w[0] as f64) / 1e6)
        .collect()
}

/// Nanoseconds to milliseconds.
pub fn ms(ns: f64) -> f64 {
    ns / 1e6
}

/// Signed difference `later - earlier` of two host timestamps in milliseconds.
pub fn delta_ms(earlier_ns: u64, later_ns: u64) -> f64 {
    (later_ns as f64 - earlier_ns as f64) / 1e6
}

/// Running sum of squares, peak and non-zero count for audio level (PRD §15.1: samples are
/// discarded; only these aggregates survive).
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize)]
pub struct Level {
    pub samples: u64,
    pub sum_squares: f64,
    pub peak: f32,
    pub nonzero: u64,
}

impl Level {
    /// Adds a buffer of samples.
    pub fn add(&mut self, buf: &[f32]) {
        for &s in buf {
            let a = s.abs();
            self.sum_squares += f64::from(s) * f64::from(s);
            if a > self.peak {
                self.peak = a;
            }
            if s != 0.0 {
                self.nonzero += 1;
            }
        }
        self.samples += buf.len() as u64;
    }

    /// Root mean square, 0 when empty.
    pub fn rms(&self) -> f64 {
        if self.samples == 0 {
            0.0
        } else {
            (self.sum_squares / self.samples as f64).sqrt()
        }
    }

    /// RMS in dBFS, floored at -200 dB for silence.
    pub fn rms_dbfs(&self) -> f64 {
        dbfs(self.rms())
    }
}

/// Amplitude to dBFS, floored at -200 dB.
pub fn dbfs(amplitude: f64) -> f64 {
    if amplitude <= 1e-10 {
        -200.0
    } else {
        20.0 * amplitude.log10()
    }
}

/// Least-squares line through (x, y); `None` for fewer than two distinct x values.
pub fn linear_fit(xs: &[f64], ys: &[f64]) -> Option<(f64, f64)> {
    if xs.len() != ys.len() || xs.len() < 2 {
        return None;
    }
    let n = xs.len() as f64;
    let mx = xs.iter().sum::<f64>() / n;
    let my = ys.iter().sum::<f64>() / n;
    let mut sxx = 0.0;
    let mut sxy = 0.0;
    for (x, y) in xs.iter().zip(ys) {
        sxx += (x - mx) * (x - mx);
        sxy += (x - mx) * (y - my);
    }
    if sxx == 0.0 {
        return None;
    }
    let slope = sxy / sxx;
    Some((slope, my - slope * mx))
}

/// A slope that one outlier cannot move: the points are sorted by x, each point in the first
/// half is paired with the point half the run later, and the median of those pairs' slopes is
/// returned. `None` for fewer than two pairs with distinct x.
pub fn median_pair_slope(xs: &[f64], ys: &[f64]) -> Option<f64> {
    if xs.len() != ys.len() || xs.len() < 2 {
        return None;
    }
    let mut pts: Vec<(f64, f64)> = xs.iter().copied().zip(ys.iter().copied()).collect();
    pts.sort_by(|a, b| a.0.total_cmp(&b.0));
    let half = pts.len() / 2;
    let slopes: Vec<f64> = (0..half)
        .filter_map(|i| {
            let (a, b) = (pts[i], pts[i + half]);
            let dx = b.0 - a.0;
            (dx != 0.0).then(|| (b.1 - a.1) / dx)
        })
        .collect();
    summarize(&slopes).filter(|s| s.n >= 2).map(|s| s.p50)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn median_pair_slope_ignores_one_outlier() {
        let xs: Vec<f64> = (0..20).map(f64::from).collect();
        let mut ys: Vec<f64> = xs.iter().map(|x| 3.0 * x + 1.0).collect();
        assert_eq!(median_pair_slope(&xs, &ys), Some(3.0));
        // One wild point at the end moves the least-squares slope but not this one.
        ys[19] += 400.0;
        let (lsq, _) = linear_fit(&xs, &ys).expect("fit");
        assert!((lsq - 3.0).abs() > 1.0, "{lsq}");
        assert_eq!(median_pair_slope(&xs, &ys), Some(3.0));
        assert_eq!(median_pair_slope(&[1.0, 2.0], &[1.0, 2.0]), None);
        assert_eq!(
            median_pair_slope(&[1.0; 6], &[1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
            None
        );
    }

    #[test]
    fn nearest_rank_percentiles() {
        let v: Vec<f64> = (1..=20).map(f64::from).collect();
        assert_eq!(percentile_sorted(&v, 50.0), 10.0);
        assert_eq!(percentile_sorted(&v, 95.0), 19.0);
        assert_eq!(percentile_sorted(&v, 100.0), 20.0);
        assert_eq!(percentile_sorted(&v, 0.0), 1.0);
        assert_eq!(percentile_sorted(&[7.0], 95.0), 7.0);
        assert!(percentile_sorted(&[], 50.0).is_nan());
    }

    #[test]
    fn summary_drops_non_finite_values() {
        let s = summarize(&[3.0, f64::NAN, 1.0, 2.0, f64::INFINITY]).expect("summary");
        assert_eq!((s.n, s.min, s.p50, s.max), (3, 1.0, 2.0, 3.0));
        assert!((s.mean - 2.0).abs() < 1e-12);
        assert!(summarize(&[f64::NAN]).is_none());
    }

    #[test]
    fn intervals_and_deltas() {
        assert_eq!(intervals_ms(&[0, 10_000_000, 30_000_000]), vec![10.0, 20.0]);
        assert_eq!(delta_ms(5_000_000, 2_000_000), -3.0);
    }

    #[test]
    fn level_of_a_full_scale_square_wave_is_zero_dbfs() {
        let mut l = Level::default();
        l.add(&[1.0, -1.0, 1.0, -1.0]);
        assert!((l.rms() - 1.0).abs() < 1e-12);
        assert!(l.rms_dbfs().abs() < 1e-9);
        assert_eq!((l.nonzero, l.peak), (4, 1.0));
        let mut silent = Level::default();
        silent.add(&[0.0; 64]);
        assert_eq!(silent.rms_dbfs(), -200.0);
        assert_eq!(silent.nonzero, 0);
        let mut half = Level::default();
        half.add(&[0.5, -0.5]);
        assert!((half.rms_dbfs() - (-6.0206)).abs() < 1e-3);
    }

    #[test]
    fn fits_a_line() {
        let xs = [0.0, 1.0, 2.0, 3.0];
        let ys = [1.0, 3.0, 5.0, 7.0];
        let (m, b) = linear_fit(&xs, &ys).expect("fit");
        assert!((m - 2.0).abs() < 1e-12 && (b - 1.0).abs() < 1e-12);
        assert!(linear_fit(&[1.0, 1.0], &[2.0, 3.0]).is_none());
    }
}
