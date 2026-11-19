# Cameras and photo sources — licensing, 18–19 Nov 2026

Desk research by a research agent: terms pages, API docs and operator camera listings. No signups, keys or scraping; resolutions are from the JPEG header of **one** live frame per camera (spot check). **Not legal advice.** Feeds DECISIONS.md D-007.

Uses we need, each checked against terms: **U1** automated fetch at a low rate · **U2** store frames · **U3** derive a base plate · **U4** display frames and derivatives with attribution.

## Main finding

**No live public camera of the Golden Gate Bridge or the SF skyline has terms that clearly allow U1–U4.** The licence-clean coastal-fog camera is the **NPS air-quality webcam at Point Reyes**: "the windiest place on the Pacific Coast and the second foggiest place on the North American continent" [6].

The NPS disclaimer [4]: material "created by the National Park Service… is generally considered in the public domain. It may be distributed or copied as permitted by applicable law… a citation or acknowledgement… is appreciated." It also warns that "not all materials… are in the public domain", so **partner-run cameras on NPS pages are excluded**.

## Candidate cameras

| Camera | Owner | View | Interval | Res. | U1 | U2 | U3 | U4 |
|---|---|---|---|---|---|---|---|---|
| **Point Reyes air cam** [1][2][3] | NPS Air Resources | Point Reyes Beach, looking NNE from the Lighthouse Visitor Center | "refreshes every fifteen minutes" [3] | 3840×2160 (also 640×360) | OK (public domain [4]) | OK | OK | OK, credit NPS |
| **Grand Canyon, Yavapai Point air cam** [2][10] | NPS | South Rim looking NW | 15 min | 3840×2160 | OK | OK | OK | OK |
| **Grand Teton air cam** [2] | NPS | Teton Science Center looking W at the range | 15 min | 3840×2160 | OK | OK | OK | OK |
| **Mount Rainier "Mountain" cam** [7] | NPS | Paradise toward the peak ("Is the mountain out?") | not stated (page reloads every 60 s) | 1920×1080 | OK | OK | OK | OK |
| Glacier, Lake McDonald-1 [8] | NPS-hosted; Glacier Conservancy supports installations | Apgar, up the lake to the Divide | not stated | 1920×1080 | likely OK — confirm ownership | likely | likely | likely |
| Yosemite, Turtleback Dome air cam [9] | NPS | looking SW | 15 min | 3840×2160 | OK | OK | OK | OK |
| Acadia, McFarland Hill air cam [2] | NPS | looking NE | 15 min | 3840×2160 | OK | OK | OK | OK |
| Mount St. Helens, Johnston Ridge [11] | USGS CVO research camera | looking S at the crater | not stated | 1920×1080 | OK: "USGS-authored… data… are considered to be in the U.S. Public Domain" | OK | OK | OK, "Credit: U.S. Geological Survey" |
| Caltrans D4 CCTV: TVD32/33 Bay Bridge towers, TVE70 Bowers Vista Point (north end of the Golden Gate Bridge) [13] | Caltrans | road-oriented; may be re-aimed | 5 min + 12 hourly "previous" frames | **320×260** | OK: "There is no charge for the use of this data" | OK | OK | OK: "In general, information… is considered in the public domain" (images "neither retained nor archived") |
| ALERTCalifornia Sutro Tower 1/2 (az 327°/341°, toward the Golden Gate), Mt Tam East (az 163°) [14] | UC San Diego | **PTZ**; listing reports `az_current`, `zoom_current` | near-real-time | 1920×1080 | **unclear** | OK, non-commercial | **prohibited to share**: CC BY-NC-ND 4.0 allows you to "produce and reproduce, but not Share, Adapted Material" | OK unmodified, credit "ALERTCalifornia \| UC San Diego" |
| Lawrence Hall of Science "The View" [17] | UC Berkeley | across the bay to a distant Golden Gate | 1 min | not stated (1080p timelapse) | unclear (no terms; "© 2026 The Regents") | unclear | unclear | unclear |
| Lick Observatory HamCam1 [18] | UC Observatories | summit; can be turned, incl. W/NW | 3 min | 2592×1520 | unclear | unclear | unclear | unclear |
| SFSU FogCam [16] | SF State Academic Technology | campus, not the bay | 20 s | 640×480 | unclear | unclear | unclear | unclear |
| FAA WeatherCams [12] | FAA (> 260 owned) + > 530 hosted third-party | airports, passes | 10 min | — | **unclear**: no published API/terms; site `/api/...` returned HTTP 401 to a direct request | FAA-owned frames public domain (17 USC 105); third-party not | same | same |
| HPWREN [15] | UC San Diego (SoCal only) | fixed + PTZ | — | — | **prohibited for real time**: licence needed to "retrieve real-time data automatically… scheduled downloads" | archive > 24 h OK (CC BY-NC 4.0) | archive OK (NC, may adapt) | OK, "Data courtesy of HPWREN…" |

**Other Bay Area leads, none usable:** Exploratorium lists sensors, no webcam [20]; no official Golden Gate Bridge District or Port of SF webcam found; NPS Golden Gate NRA only links third-party cameras [19]; Parks Conservancy's Crissy Field PTZ cams are "temporarily unavailable" and its Mt Tam image comes from ABC7/KGO [19]; NOAA buoy 46026 BuoyCAM (2880×300 panorama, public domain [28]) moves with the buoy, so it is only a secondary fog check; ALERTWildfire has no terms on its site (ALERTCalifornia's apply in California).

**NPS caveats:** a "summer only" notice appears word for word on every park's archive page [5], but live frames were updating at 03:35 UTC on 19 Nov, so it looks like stale boilerplate. Partner cameras on NPS pages are not public domain: Yosemite's Half Dome, El Capitan, Falls and High Sierra cams belong to Yosemite Conservancy ("No additional use is authorized") [9]; Grand Canyon's Kolb Studio and Bright Angel cams are served from pixelcaster.com [10].

## Aggregators — all fail U2–U4

- **Windy** [21]: "The User is not allowed to publish any graphics displaying the Webcam images", "must not further redistribute the Content", no "image URL usage other than provided by the API"; free tier "Image size is limited"; tokens expire after 10 min (docs) or 15 min (pricing page); Professional €9,990/yr.
- **Skyline Webcams** [22]: prohibits anyone "to record, download, copy… or republish the Content" and "to extract the Content… by automated means (scraping)".
- **EarthCam** [23]: embedding needs a licence; social sharing only with the watermark and "Courtesy of EarthCam".
- **Webcamtaxi:** terms page returned HTTP 403; mostly embeds third-party streams. Unusable.

## Photo sources (Tier 0 library, base plates)

- **Wikimedia Commons** [25]: licence per file; ShareAlike licences mean "if you modify the work, your modifications must also be similarly freely licensed". API `extmetadata` gives `DateTimeOriginal`, `GPSLatitude/Longitude`, `LicenseShortName`, `Artist`, `AttributionRequired`; `iiprop=metadata` returns raw EXIF. Heading rarely recorded. U2–U4 OK with attribution; **a plate relit from a CC BY-SA file must itself be CC BY-SA.**
- **Flickr** [26]: API terms (2018) forbid caching user photos "other than for reasonable periods" and require "This product uses the Flickr API but is not endorsed or certified by SmugMug, Inc." API keys reportedly now Pro-only (~$82/yr, secondary sources). U2 doubtful.
- **Mapillary** [27]: CC BY-SA 4.0; API gives `captured_at`, `computed_geometry`, `computed_compass_angle`, `computed_rotation`, `camera_parameters`; self-hosted images need the Mapillary logo and a link back; unapproved data mining is banned.

## Recommendations (agent's)

**First three viewpoints, all NPS public domain, one credit line ("Courtesy National Park Service"), stable direct JPEG URLs:**
1. **Point Reyes air cam:** the marine-fog case. 4K every 15 min, plus hourly temperature, humidity and wind at the same site.
2. **Mount Rainier "Mountain" cam (Paradise):** orographic cloud caps, heavy snow and rain, "is the mountain out?".
3. **Grand Canyon, Yavapai Point air cam:** dry clear days, summer thunderstorms, winter snow and haze; 4K with a visibility reading.

Base plates: hand-pick clear-day and night frames from the NPS archive ("Show Nighttime Images" toggle, high-res downloads) [5]. The NPS "clear/hazy" reference images are only 550×422.

If a Golden Gate view is required: Caltrans is public domain but 320×260 and may be re-aimed → scoring only, not a plate.

**Ask permission (best-looking cameras with unclear terms):**
- Lawrence Hall of Science "The View" (best Golden Gate candidate): phone 510-642-5132, no email listed [17].
- ALERTCalifornia Sutro Tower / Mt Tam East: an exception to the ND clause and permission for automated fetch, via alertcalifornia.org contact [14].
- Lick Observatory HamCam: hamcam@ucolick.org [18].
- SFSU FogCam: via at.sfsu.edu [16].
- FAA WeatherCams (automated access, clear-day images): 9-AJO-WCAM-IT@faa.gov or WCAMExp@faa.gov [12]; the site's code requests a per-camera clear-day reference from an `/images/clearday` endpoint.
- HPWREN real-time waiver: fback@hpwren.ucsd.edu ("may waive it for academic research and teaching") [15].
- Yosemite Conservancy (Half Dome / El Capitan): usage form form.jotform.com/201336316228044 [9].

## Sources

1 https://www.nps.gov/features/ard/webcams/index.html · 2 https://www.nps.gov/featurecontent/ard/webcams/json/porejson.txt (also grcajson.txt, grtejson.txt, morajson.txt, acadjson.txt, yosejson.txt) · 3 https://www.nps.gov/pore/learn/photosmultimedia/webcams.htm · 4 https://www.nps.gov/aboutus/disclaimer.htm · 5 https://www.nps.gov/AirWebCams/pore · 6 https://www.nps.gov/pore/learn/historyculture/stories_maritime_lighthouse.htm · 7 https://www.nps.gov/mora/learn/photosmultimedia/webcams.htm · 8 https://www.nps.gov/glac/learn/photosmultimedia/webcams.htm · 9 https://www.nps.gov/yose/learn/photosmultimedia/webcams.htm; https://form.jotform.com/201336316228044 · 10 https://www.nps.gov/grca/learn/photosmultimedia/webcams.htm · 11 https://www.usgs.gov/volcanoes/mount-st.-helens/multimedia/webcams; https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits · 12 https://www.faa.gov/about/office_org/headquarters_offices/ato/service_units/systemops/fs/alaskan/weather_cams; https://weathercams.faa.gov; https://www.law.cornell.edu/uscode/text/17/105 · 13 https://cwwp2.dot.ca.gov/documentation/cctv/cctv.htm; https://dot.ca.gov/conditions-of-use; https://cwwp2.dot.ca.gov/data/d4/cctv/cctvStatusD04.json · 14 https://alertcalifornia.org/terms/; https://alertcalifornia.org/images-and-video/; https://cameras.alertcalifornia.org/public-camera-data/all_cameras-v3.json; https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode.en · 15 https://hpwren.ucsd.edu/cc.html · 16 https://fogcam.org/ · 17 https://lawrencehallofscience.org/play/view/ · 18 https://mthamilton.ucolick.org/hamcam/; https://www.ucolick.org/main/science/hamcam.html; https://mthamilton.ucolick.org/hamcam/infopage/ · 19 https://www.nps.gov/goga/learn/photosmultimedia/webcams.htm; https://www.parksconservancy.org/parks/park-web-cams · 20 https://www.exploratorium.edu/environmental-field-station/current-conditions · 21 https://api.windy.com/webcams/terms; https://account.windy.com/agreements/windy-webcams-api-terms-of-use; https://api.windy.com/webcams/pricing; https://api.windy.com/webcams/docs · 22 https://www.skylinewebcams.com/en/terms-of-use.html · 23 https://www.earthcam.com/faq.php · 25 https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia; https://www.mediawiki.org/wiki/Extension:CommonsMetadata · 26 https://www.flickr.com/help/terms/api; https://www.cantoni.org/2025/12/12/downsizing-flickr-library-free-tier/ (secondary); https://toolradar.com/tools/flickr/pricing (secondary) · 27 https://www.mapillary.com/terms; https://www.mapillary.com/developer/api-documentation · 28 https://www.ndbc.noaa.gov/buoycams.php; https://www.weather.gov/disclaimer · (no [24]: Webcamtaxi terms returned HTTP 403)

## Addendum — spot checks by the author, 2026-11-19 04:20 UTC (one frame each)

| Frame | Resolution | Last-Modified (UTC) | EXIF |
|---|---|---|---|
| Point Reyes ARD air cam `featurecontent/ard/webcams/images/porelarge.jpg` | 3840×2160, 1.86 MB | 04:15:24 | `Artist: National Park Service`, `Copyright: National Park Service`, `ImageDescription: National Park Service Webcam Image`, `Software: ARS_Digicam`, `DateTime 2026:11:18 20:00:00` |
| Mount Rainier ARD air cam `…/images/moralarge.jpg` ("View from Paradise, Looking West-Southwest") | **512×288** | 04:15:20 | same NPS fields as Point Reyes |
| Mount Rainier park cam `nps.gov/webcams-mora/mountain.jpg` | 1920×1080, 577 KB | 04:21:14 | `Make: AXIS`, `Model: P1387`, `DateTime 2026:11:18 20:21:03`; **no artist/copyright fields** |

- The ARD air-cam EXIF names NPS as artist and copyright holder; `ARS_Digicam` is the capture software. This supports NPS authorship, though written confirmation is still worth requesting.
- The Rainier ARD air cam looks WSW from Paradise (not at the summit) and its "large" image is only 512×288, so the **park's `mountain.jpg` (1080p) is the Rainier candidate**. Its ownership must be confirmed with Mount Rainier NP (it carries no ownership metadata), and its update cadence is not quarter-hourly (20:21 local) — measure it.
- NPS JSON `imagedate` and HTTP `Last-Modified` agree to within minutes for the ARD cams, so staleness detection can use both.
