//! The camera facts Tier 0 needs from the viewpoint manifests (PRD Appendix A, §9.1, §15.4).
//!
//! The alias table ([`crate::places::PlaceTable`]) keeps what the parser needs. Tier 0 also needs
//! the camera's **view azimuth** (to put the Sun's azimuth in the camera's frame), its **image
//! size** (the framing check), the **attribution** and operator (the licence column of the
//! library and the caption's credit, PRD §15.4), and whether the camera has an **NPS archive**.
//! A camera whose manifest names no `archive_code` has no archive of its own, so its library is
//! our own captures only: that is Rainier's case (D-019; the `mora` archive belongs to another
//! camera, as its manifest notes say).

use std::path::Path;
use std::sync::OnceLock;

use chrono_tz::Tz;
use serde::Deserialize;

use crate::places::{PlaceTableError, embedded_manifest_texts, manifest_texts_from_dir};
use crate::solar::Site;

/// What Tier 0 knows about one camera (one manifest).
#[derive(Debug, Clone, PartialEq)]
pub struct CameraInfo {
    /// `viewpoint_id`, for example `rainier.paradise-mountain`.
    pub viewpoint_id: String,
    /// The place's display name ("Mount Rainier").
    pub place_name: String,
    /// The operator's label for the view, if any ("Mountain webcam, Paradise").
    pub view_label: Option<String>,
    /// The place's IANA zone (local dates and the caption's clock).
    pub tz: Tz,
    /// Camera position: the observer for the Sun (PRD §8.4).
    pub site: Site,
    /// Compass bearing of the view centre, degrees from north through east.
    pub view_azimuth_deg: f64,
    /// The camera's image size in pixels, `[width, height]`, if the manifest gives it.
    pub image_px: Option<(u32, u32)>,
    /// The manifest's attribution line (the licence of every frame from this camera).
    pub attribution: String,
    /// Who runs the camera ("National Park Service (Mount Rainier National Park)").
    pub operator: String,
    /// `colour`, `infrared`, `mixed` or `unknown` (the manifest's `night_mode`).
    pub night_mode: String,
    /// The NPS archive code, if the camera has an archive.
    pub archive_code: Option<String>,
}

impl CameraInfo {
    /// True when the library may hold only our own live captures from this camera: the camera has
    /// no NPS archive (D-019: Rainier's library is our own captures only).
    pub fn own_captures_only(&self) -> bool {
        self.archive_code.as_deref().is_none_or(str::is_empty)
    }

    /// The caption's credit: "Courtesy National Park Service" from "Webcam imagery courtesy
    /// National Park Service. ...", else the attribution's first sentence (PRD §5.3, §15.4).
    pub fn credit(&self) -> String {
        let first = self
            .attribution
            .split_once(". ")
            .map_or(self.attribution.as_str(), |(a, _)| a)
            .trim_end_matches('.');
        let lower = first.to_lowercase();
        match lower.find("courtesy ") {
            Some(i) => format!("Courtesy {}", &first[i + "courtesy ".len()..]),
            None => first.to_string(),
        }
    }

    /// The camera's name for captions: "Mount Rainier, Mountain webcam, Paradise", or the place
    /// name alone.
    pub fn display_name(&self) -> String {
        match &self.view_label {
            Some(label) if !label.is_empty() => format!("{}, {label}", self.place_name),
            _ => self.place_name.clone(),
        }
    }
}

#[derive(Deserialize)]
struct RawManifest {
    id: String,
    place: String,
    time_zone: String,
    camera: RawCamera,
    truth_camera: RawTruthCamera,
    attribution: String,
}

#[derive(Deserialize)]
struct RawCamera {
    lat: f64,
    lon: f64,
    elevation_m: f64,
    view_azimuth_deg: f64,
}

#[derive(Deserialize)]
struct RawTruthCamera {
    operator: String,
    #[serde(default)]
    archive_code: Option<String>,
    #[serde(default)]
    night_mode: Option<String>,
    #[serde(default)]
    view_label: Option<String>,
    #[serde(default)]
    image_px: Option<Vec<u32>>,
}

/// The cameras of every manifest, sorted by `viewpoint_id`.
#[derive(Debug, Clone, PartialEq)]
pub struct CameraTable {
    cameras: Vec<CameraInfo>,
}

impl CameraTable {
    /// The cameras of the manifests compiled into the crate.
    pub fn embedded() -> &'static CameraTable {
        static TABLE: OnceLock<CameraTable> = OnceLock::new();
        TABLE.get_or_init(|| {
            CameraTable::from_manifest_texts(embedded_manifest_texts().iter().copied())
                .expect("the embedded viewpoint manifests are valid (checked by tests)")
        })
    }

    /// The cameras of every `*.json` manifest in a directory.
    pub fn from_dir(dir: &Path) -> Result<CameraTable, PlaceTableError> {
        let texts = manifest_texts_from_dir(dir)?;
        CameraTable::from_manifest_texts(texts.iter().map(|(a, b)| (a.as_str(), b.as_str())))
    }

    /// Build the table from `(file stem, JSON text)` pairs.
    pub fn from_manifest_texts<'a>(
        manifests: impl IntoIterator<Item = (&'a str, &'a str)>,
    ) -> Result<CameraTable, PlaceTableError> {
        let mut cameras = Vec::new();
        for (stem, text) in manifests {
            let raw: RawManifest =
                serde_json::from_str(text).map_err(|source| PlaceTableError::Json {
                    name: stem.to_string(),
                    source,
                })?;
            let invalid = |message: String| PlaceTableError::Invalid {
                name: stem.to_string(),
                message,
            };
            if raw.id != stem {
                return Err(invalid(format!(
                    "id {} does not match the file name",
                    raw.id
                )));
            }
            let tz: Tz = raw
                .time_zone
                .parse()
                .map_err(|_| invalid(format!("unknown IANA zone {}", raw.time_zone)))?;
            if !raw.camera.view_azimuth_deg.is_finite() {
                return Err(invalid("view_azimuth_deg is not a number".to_string()));
            }
            let image_px = match raw.truth_camera.image_px.as_deref() {
                None => None,
                Some([w, h]) if *w > 0 && *h > 0 => Some((*w, *h)),
                Some(other) => {
                    return Err(invalid(format!(
                        "truth_camera.image_px {other:?} is not [width, height]"
                    )));
                }
            };
            cameras.push(CameraInfo {
                viewpoint_id: raw.id,
                place_name: raw.place,
                view_label: raw.truth_camera.view_label,
                tz,
                site: Site {
                    lat_deg: raw.camera.lat,
                    lon_deg: raw.camera.lon,
                    elevation_m: raw.camera.elevation_m,
                },
                view_azimuth_deg: raw.camera.view_azimuth_deg.rem_euclid(360.0),
                image_px,
                attribution: raw.attribution,
                operator: raw.truth_camera.operator,
                night_mode: raw
                    .truth_camera
                    .night_mode
                    .unwrap_or_else(|| "unknown".to_string()),
                archive_code: raw.truth_camera.archive_code.filter(|c| !c.is_empty()),
            });
        }
        if cameras.is_empty() {
            return Err(PlaceTableError::Empty);
        }
        cameras.sort_by(|a, b| a.viewpoint_id.cmp(&b.viewpoint_id));
        Ok(CameraTable { cameras })
    }

    /// Every camera, sorted by `viewpoint_id`.
    pub fn cameras(&self) -> &[CameraInfo] {
        &self.cameras
    }

    /// The camera of a viewpoint.
    pub fn get(&self, viewpoint_id: &str) -> Option<&CameraInfo> {
        self.cameras
            .binary_search_by(|c| c.viewpoint_id.as_str().cmp(viewpoint_id))
            .ok()
            .map(|i| &self.cameras[i])
    }

    /// The fields Tier 0 derives anything from, as one stable string: a change to any of them
    /// invalidates the library cache ([`crate::tier0::library`]).
    pub fn fingerprint(&self) -> String {
        let mut out = String::new();
        for c in &self.cameras {
            out.push_str(&format!(
                "{}|{}|{:.6}|{:.6}|{:.1}|{:.3}|{:?}|{}|{}|{:?}\n",
                c.viewpoint_id,
                c.tz.name(),
                c.site.lat_deg,
                c.site.lon_deg,
                c.site.elevation_m,
                c.view_azimuth_deg,
                c.image_px,
                c.attribution,
                c.operator,
                c.archive_code,
            ));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_embedded_cameras_match_the_alias_table() {
        let cams = CameraTable::embedded();
        let places = crate::places::PlaceTable::embedded();
        assert_eq!(cams.cameras().len(), places.viewpoints().len());
        for v in places.viewpoints() {
            let c = cams.get(&v.id).expect("every viewpoint has a camera");
            assert_eq!(c.site, v.site, "{}", v.id);
            assert_eq!(c.tz, v.tz, "{}", v.id);
            assert!((0.0..360.0).contains(&c.view_azimuth_deg));
            let (w, h) = c.image_px.expect("image_px");
            assert_eq!(
                w * 9,
                h * 16,
                "{}: every candidate camera is 16:9 (PRD §5.1)",
                v.id
            );
            assert!(c.attribution.contains("National Park Service"));
        }
    }

    #[test]
    fn only_rainier_is_own_captures_only() {
        // D-019: the summit camera has no archive, so its library is our own captures.
        let own: Vec<&str> = CameraTable::embedded()
            .cameras()
            .iter()
            .filter(|c| c.own_captures_only())
            .map(|c| c.viewpoint_id.as_str())
            .collect();
        assert_eq!(own, ["rainier.paradise-mountain"]);
    }

    #[test]
    fn credit_follows_the_attribution() {
        let c = CameraTable::embedded()
            .get("point-reyes.beach")
            .expect("camera");
        assert_eq!(c.credit(), "Courtesy National Park Service");
        let mut other = c.clone();
        other.attribution = "Image by Someone. More.".to_string();
        assert_eq!(other.credit(), "Image by Someone");
    }
}
