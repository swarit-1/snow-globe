# Draft: NPS confirmation request (M0.5)

**For you to send.** Fill the brackets and send it from the project's own contact address, not a personal one (PRD §15.5). I haven't sent anything.

**The name:** the project is Snow Globe since 21 Nov 2026 (D-080). The capture loop sent the old User-Agent, `Lookout/0.1 (…)`, until the lead's switch after the M0.5 gate, at 00:02 UTC on 23 Nov 2026, and `SnowGlobe/0.1 (…)` since (D-090), so the email names both.

Send it to two places:
1. **NPS Air Resources Division (air-quality webcams).** Use the contact form linked from the archive site: <https://www.nps.gov/airwebcams/Contact>.
2. **Mount Rainier National Park,** about the Paradise "mountain" camera. Use the park's contact page (<https://www.nps.gov/mora/contacts.htm>; check the link before sending).

---

**Subject:** Permission check: using NPS webcam images in a non-commercial forecast-verification project

Hello,

I'm building **Snow Globe**, a non-commercial, open-source portfolio project. It renders what a scenic viewpoint will look like at a future hour, using NOAA's public forecast, and then checks those renderings against what the park webcams actually recorded. NPS webcams are the ground truth. Before I publish anything, I'd like to confirm that my use is appropriate.

**Cameras:**
- Air-quality webcams:
  - Point Reyes (View of Point Reyes Beach);
  - Grand Canyon (Yavapai Point);
  - Grand Teton (Teton Science Center);
  - Yosemite (Turtleback Dome);
  - Acadia (McFarland Hill);
  - Great Smoky Mountains (Look Rock);
  - Big Bend (Panther Junction area).
- Mount Rainier: the Paradise camera at `https://www.nps.gov/webcams-mora/mountain.jpg`.

**How I collect images:**
- Live images are checked at most every 5 minutes, and an image is downloaded only when it has changed. That is no more than 4 images per camera per hour.
- Archive images (AirWebCams) are fetched one request at a time, at least 5 seconds apart, and at most 1,000 requests a day. The first pass is about 4,300 frames over five days (about 0.25 GB), and about 23,700 frames (about 1.4 GB) by mid-December.
- Every request names the project in its User-Agent: `SnowGlobe/0.1 (webcam research capture; fetch-on-change)`. Requests sent before the project's rename used its earlier name, `Lookout/0.1 (webcam research capture; fetch-on-change)`.

**What I'd like to confirm:**
1. Are the images from these cameras NPS works in the public domain, per the NPS website disclaimer? Or do any carry other terms? The air-quality images name "National Park Service" as artist and copyright holder in their EXIF data. The Rainier Paradise image carries no ownership metadata, so I'd also like to know who operates that camera and what terms apply.
2. Is the automated collection described above acceptable? Is there a preferred way to get archive images in bulk? The archive images I have seen are 999 pixels wide; are full-resolution originals kept anywhere?
3. May I publish derived images in the project's repository, README and a short demo video? These are relit or simulated views, and crops of camera frames no wider than 1280 px with people and vehicles blurred. Each would carry the credit *"Webcam imagery courtesy National Park Service. Derived images are not NPS products."* Nothing would suggest NPS endorsement.
4. **For Mount Rainier National Park, about the Paradise "mountain" camera:**
   - Where is it mounted: which building, and roughly which way does it face? I have assumed the Henry M. Jackson Memorial Visitor Center, but the webcam page says only "Paradise".
   - How often does it update? From my checks on 19 Nov 2026, the newest image was never more than about 2 minutes old when I looked, and usually under a minute. The camera's own time stamp falls within a few seconds of a whole minute. So I believe it updates about once a minute. Is that right, and is it the same day and night?
   - Are past images from this camera kept anywhere? I couldn't find a public archive.

If any of this isn't appropriate, I'll change the plan, and I'll remove any image on request within 7 days.

Thank you,
[Your name]
Snow Globe project, [project contact address]
[Repository URL]

---

**After the reply:** paste it into `docs/outreach/replies/` and tell me. Each manifest's `confirmation` and `publish_cleared` fields will be updated from it, and the PRD §15.1 publication gate opens or stays closed.
