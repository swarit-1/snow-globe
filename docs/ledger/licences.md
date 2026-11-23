# Licence ledger

**Updated:** Mon 23 Nov 2026, from the code at commit `9d372bd` plus the working tree at about 21:40 UTC, and revised after review at about 22:40 UTC. `ledger.py build` last ran at 21:37:31 UTC (`generated.at_utc`). The hand-kept parts of licences.json were edited after it, at about 21:46 UTC and at 22:36:53 UTC; an in-memory `build()` from 22:37:08 UTC still equals the file except for that stamp.

This is the licence ledger of [PRD §15.4](../../PRD.md): every data source, code port, algorithm, model, service, platform, tool and package that the repository uses or plans. The ledger itself is [licences.json](licences.json); this page is the short reading of it. [THIRD_PARTY_NOTICES](../../THIRD_PARTY_NOTICES) is to be generated from the ledger at M5 ([PLAN.md](../../PLAN.md)). Until then the notices are kept by hand, and nothing here edits them: the differences are listed below for the lead. The data-flow inventory of PRD §15.2 is [data-flows.md](data-flows.md).

## What is in licences.json

| Key | Kept by | Holds |
|---|---|---|
| `items` | hand | 86 entries: 18 data sources, 5 code ports, 20 algorithms, 11 models, 7 services, 10 platforms, 11 tools, 2 package sets outside any lock file, the CI actions, and Snow Globe itself. |
| `packages` | `ledger.py build` | 670 entries, one per package that one of the 11 tracked lock files pins: 660 third-party, 10 Snow Globe's own. |
| `package_counts` | `ledger.py build` | Per lock file: the totals, by `ships` and by scope. |
| `licence_overrides` | hand | The reviewed reading of a package whose own metadata is not an SPDX expression: python-dateutil and scipy. |
| `disagreements` | hand, plus `ledger.py` | R1–R11 below, each `open`, `resolved` or `noted`. `ledger.py` adds `licence-differs` entries itself; it found none on this run. |
| `vocabulary` | hand | What each `ships`, `status`, `kind` and scope value means; the inclusion rule (`inclusion`); and the duty rule (`duty_rule`) with its licence-to-duty table (`duty_by_licence`). |

**Fields of an item:**
- its `id`, `kind`, `name`, and the licence or basis (`licence`), with where it was read (`evidence`) when that is not the licence text itself;
- its use (`use`) and the files that use it (`where`);
- where it ships (`ships`): `app`, `workers`, `eval-only`, `tests-only` or `not-shipped`;
- its attribution duty (`duty`): what a distribution owes, "None: not redistributed" when nothing of it is given to anyone, or "To be read with its terms" when its terms are not read yet;
- its status: `confirmed`, `to-confirm` (with `confirm_with`, who settles it and how), `planned`, `excluded` or `own`;
- the THIRD_PARTY_NOTICES section that names it (`notices`);
- on some items, a `note`.

**Fields of a package** (all written by `build`):
- `ecosystem`, `name`, `version` and `source`, with the owner's GitHub account written `<owner>`, and `own`, true for Snow Globe's own packages;
- the licence (`licence`) and where it was read (`licence_source`). For a package in `licence_overrides` (python-dateutil and scipy), `read_here` keeps what `build` read before the override;
- its use: `in` lists each lock file and scope that pins it, whether a project names it directly, where it ships from that lock, and, for npm packages, `installed_here` and any `platform`;
- `ships`, and the THIRD_PARTY_NOTICES lines that name it with their licence text (`notices`);
- its status, its generated `duty`, and, when `to-confirm`, a generated `confirm_with`.

**What becomes an item** (`vocabulary.inclusion`). Every outside source of what the repository runs, ships or plans: data, code ported from elsewhere, models, services, platforms, tools, packages outside any lock file, and every algorithm, formula or method the code names in its comments, docstrings and READMEs.
- A method the code takes from a specific work (a paper, a book, an author's published constants or another implementation) gets its own item, with that work. A standard's model that the code takes with its paper or book counts too: the IAU 1976 precession (Lieske et al. 1977, through Meeus).
- The textbook techniques it names without such a source, and the public standards whose constants and algorithms it uses as given, share one item, `algo.textbook-techniques`. The standards are the WGS 84 ellipsoid, sRGB with the BT.709 and Rec. 601 luma weights, WCAG 2's contrast ratio, the CIE clear sky and SHA-256.
- File formats, protocols, datums and identifiers (JPEG, PNG, GRIB2, HTTP, JSON Schema, ISO 8601, PEP 508, NAVD 88, EPSG codes) are not items.
- "How the methods were found", below, gives the searches.

**Duties** (`vocabulary.duty_rule`). An item's duty is written by hand. A package's is written by `build`, from where it ships and its licence:
- **592 packages:** "None: not redistributed". Nothing outside the app and the workers is ever given to anyone, so no licence's conditions are triggered.
- **28 packages** ship in the workers: none while they run only on this Mac and, once AWS is applied, in the owner's account. Each entry also says what would apply if the workers were ever distributed.
- **40 crates** ship in the app from M1: the duties of their licence expressions, from `duty_by_licence`. Under OR one licence's duty is enough; under AND all apply.
- **10 packages** are Snow Globe's own; they point to `own.snow-globe`.
- `duty_by_licence` holds a short reading of each licence's conditions, for the M5 notices generator; the licence texts govern. It must hold every licence of a package that ships in the app or the workers: `build` stops on one it lacks, and a test checks that every stored duty is the rule's.

**Commands.** The tools are standard library only, need Python 3.11 or later (`tomllib`; the system's `/usr/bin/python3` is 3.9), and use no network:

```sh
python3 -I -B docs/ledger/ledger.py check        # every package of every tracked lock file is listed at its version
python3 -I -B docs/ledger/ledger.py build        # re-read the locks and the installed licences; keeps the hand-kept parts
python3 -I -B -m unittest discover -s docs/ledger -t docs/ledger   # 37 tests
```

Both commands read the lock files git tracks: committed, or staged with `git add`. An untracked lock file is another package's work in progress: neither command reads it until it is added, so a clean checkout, as in CI, gives the same answer as this Mac. A tracked lock file that is gone from disk pins nothing, and `check` reports any entry the ledger still lists for it. Both commands name either kind in a note. Today the note names `app/src-tauri/Cargo.lock`, the M1 app shell's, which `SHIPS` already places as app/Cargo.lock's crates are.

**Where `build` reads each licence.** From the package as it is installed on this Mac:
- **Python:** the wheels' METADATA in the five uv venvs, then uv's cache.
- **Rust:** `cargo metadata --offline --locked`, for both tracked Cargo workspaces.
- **npm:** each `package.json` under `node_modules/.pnpm`, for both pnpm projects.
- **Terraform:** the AWS provider's `LICENSE.txt` in the plugin cache (`TF_PLUGIN_CACHE_DIR`, else `$SNOW_GLOBE_DATA_DIR/cache/terraform-plugins`).

## The packages, in numbers

Counted by the build of 21:37:31 UTC (`package_counts`; unchanged from the build of 10:41 UTC). Each package is counted once per lock file, at its widest destination.

| Lock file | Third-party | Where they ship |
|---|---|---|
| `app/Cargo.lock` | 80 | 40 in the app; 1 build-only (autocfg); 33 tests only; 6 never built on this Mac |
| `spikes/m1-platform/Cargo.lock` | 471 | not shipped (263 built here, 208 for other platforms only) |
| `workers/common/uv.lock` | 40 | 21 workers (the runtime closure's 14, and 7 more from the `s3` extra); 18 tests only; 1 Windows-only |
| `workers/capture/uv.lock` | 40 | 21 workers; 18 tests only; 1 Windows-only |
| `workers/forecast/uv.lock` | 35 | 28 workers; 6 tests only; 1 Windows-only |
| `eval/uv.lock` | 39 | 30 eval only (the runtime closure's 16, the `oculus` group's 1, and the `forecast` group's 13 more); 8 tests only; 1 Windows-only |
| `plates/uv.lock` | 23 | 16 not shipped (the plate builder); 6 tests only; 1 Windows-only |
| `renderer/pnpm-lock.yaml` | 106 | 30 eval only (the headless runner: esbuild, playwright-core, pngjs and jpeg-js, and esbuild's 26 platform binaries); 76 tests only |
| `app/src/pnpm-lock.yaml` | 103 | not shipped: the overlay's build and test tools (its bundle holds only its own sources) |
| both `.terraform.lock.hcl` | 1 | not shipped (hashicorp/aws 6.73.0, MPL-2.0) |

The runtime closures agree with THIRD_PARTY_NOTICES 4e: common 14, capture 21, forecast 28, eval 16 (17 with `oculus`), plates 16. No locked version changed since the ledger was first built. The rebuild changed only the notices' line references of 69 packages (`9d372bd` added lines to the notices, and its 4d names certifi 2026.10.23 once more), and wrote the new `duty`, `confirm_with` and `<owner>` fields.

**Status.** 603 packages are `confirmed`: their licence was read here and agrees with THIRD_PARTY_NOTICES wherever the notices name them. 57 are `to-confirm`, and each now says who settles it (`confirm_with`): the lead, only if one is ever installed here or redistributed.
- 56 are optional npm packages for other platforms, none installed on this Mac, so their licences were not read:
  - esbuild's, Rolldown's and lightningcss's binaries for other systems;
  - the WebAssembly fallback's `@emnapi/*`, `@napi-rs/wasm-runtime`, `@tybys/wasm-util` and `tslib`.
- 1 is colorama 0.4.6, a Windows-only test dependency.
- None of the 57 is redistributed, so each one's duty is "None: not redistributed".

**What ships in the app.** The app's Rust core carries 40 crates:
- **By licence:** 23 MIT OR Apache-2.0, 7 MIT, 2 Unlicense OR MIT, 2 0BSD, 2 BSD-2-Clause OR Apache-2.0 OR MIT, 2 MIT OR Apache-2.0 OR Zlib, 1 Apache-2.0 and 1 (MIT OR Apache-2.0) AND Unicode-3.0.
- **In the notices:** THIRD_PARTY_NOTICES names 10 of them with their versions, and 7 more by name only, in its licence counts. It counts the rest.
- **At M5:** every one's `duty` says what the generated notices owe it: for the MIT, BSD and Apache-2.0 crates, their notice or licence text in a binary distribution. The same goes for the Rust standard library (`platform.rust-std`).

**What ships in the workers.** The capture and forecast workers carry the same 28 Python packages that THIRD_PARTY_NOTICES 4e lists, at the same versions and licences. Their duties stay "none while not distributed": the workers run on this Mac today, and in the owner's own AWS account once it is applied.

## Open items, and who settles them

| Item | Status | Settled by |
|---|---|---|
| NPS imagery, the AirWebCams archive and the recorded NPS fixtures | to-confirm | NPS Air Resources Division and Mount Rainier National Park, in writing. The drafts are in [docs/outreach/nps-confirmation.md](../outreach/nps-confirmation.md), not sent yet; sending them is the owner's step. Nothing derived is published until then (PRD §15.1). |
| Snow Globe's own licence | to-confirm | The owner. The repository has been public without one since 21 Nov (D-080), so it grants none. |
| Bright Star Catalogue (plates) | to-confirm | The owner (D-103, proposed): where the local copy came from, and whether its terms allow this use. |
| JPL Horizons: the six recorded responses in eval's fixtures | to-confirm | The lead: whether JPL's terms allow redistributing them. Neither JPL page states terms (THIRD_PARTY_NOTICES 1). They are committed in `f841cef`, so they become public on the next push. |
| JPL DE421 | to-confirm | Not needed while the kernel stays in the data dir (THIRD_PARTY_NOTICES 1). |
| Bruneton's transmittance parameterisation | to-confirm | The lead, online: whether the sebh reference's transmittance code, which the port follows, is Bruneton's 2017 code (BSD-3-Clause). The repository cannot say. Its port was written offline from Hillaire's paper and the reference's structure, and it holds no Bruneton code. |
| Numerical Recipes' erfcc, in `renderer/src/math/normal.ts` | to-confirm | The lead: keep it (only tests call it), or replace it with an approximation from a source with open terms. Numerical Recipes' terms were not read here. |
| Apple's on-device dictation model | to-confirm | The owner, at M1: Apple's terms for SpeechAnalyzer and its assets in a distributed app. |
| Oculus's Terraform, adapted in `infra/terraform` | to-confirm | The owner: does the Oculus licence cover its Terraform? If so, its notice belongs with the adapted files, which the public repository already carries. |
| Docker Desktop | to-confirm | The owner: Docker's subscription terms for this use. |
| The Rust standard library; the Lambda base image and its Python packages; the Linux wheels' bundled libraries; eccodeslib's and eckitlib's bundled libraries | to-confirm | The lead, at M5. Each is a TODO(M5) in THIRD_PARTY_NOTICES, or should be one. |
| Playwright's Chromium headless shell | to-confirm | The lead (R6). Tests and evaluation only. |
| Node.js; the GitHub Actions; tflint; Xcode; scikit-image 0.25.2 (one SSIM check) | to-confirm | The lead: the GitHub Actions from their repositories at M5, the others only if one is ever redistributed or used again. None is redistributed. |
| Tauri, for the M1 shell | planned | The lead, when `app/src-tauri/Cargo.lock` is committed: run `ledger.py build`, which places its crates and generates their duties, MPL-2.0's source offer among them. |
| Open-Meteo | planned | The owner, before M2 (R9). The lead proposed on 23 Nov reading NOAA's own GFS from NODD instead (ruling j, D-115). |
| Deepgram, fal, Runware, Claude Haiku, Z-Image-Turbo, FLUX.2 klein, Parakeet, Foundation Models, the M2 models, the M4 baseline | planned | The owner's go-ahead (and a key) for each vendor, then its terms are read (CLAUDE.md "Paid services"). |

## Disagreements for the lead

These are the reviewed entries in `disagreements`. THIRD_PARTY_NOTICES is not edited here.

**Open:**
1. **R2, PRD §15.4: the Hillaire row.** It offers "sebh/UnrealEngineSkyAtmosphere (MIT) or Babylon.js (Apache-2.0)".
   - The code ports sebh's only (MIT, Epic Games 2020; D-013, D-098).
2. **R3, PRD §15.4: the oculus-core row.** It says "Notice inside the WASM bundle".
   - oculus-core is linked natively into `app/core` today, and the renderer has no WASM yet.
   - The notice must ship with every build that contains it, as THIRD_PARTY_NOTICES 2 already says.
3. **R4, items in neither PRD §15.4 nor THIRD_PARTY_NOTICES:**
   - FLUX.2 klein (licence unrecorded);
   - Apple's dictation model and Foundation Models, as models;
   - the Rust standard library;
   - the GitHub Actions in CI;
   - Oculus's adapted Terraform;
   - the services' terms: AWS, GitHub, Deepgram, fal, Runware, Anthropic, Cloudflare and the Open-Meteo API;
   - scikit-image 0.25.2, used once to check the renderer's SSIM.
   - JPL Horizons, DE421 and the isolated Skyfield environment were on this list until `9d372bd` added them (notices 1 and 4d).
4. **R5, THIRD_PARTY_NOTICES 6 and 7: build tools.** Section 7 names uv's licence (MIT OR Apache-2.0). It names Terraform and Docker without theirs, section 6 names Xcode and ShellCheck without theirs, and neither names pnpm or actionlint as tools. For the record:
   - Terraform 1.16.1 is BUSL-1.1, not open source; its Additional Use Grant covers this use.
   - ShellCheck 0.11.0 is GPL-3.0-or-later.
   - pnpm 12.15.0 and actionlint 1.7.12 are MIT.
   - Docker Desktop has its own subscription terms (not read here).
   - None is redistributed.
5. **R6, THIRD_PARTY_NOTICES 5: the Chromium shell.** The notices call it BSD-3-Clause with its bundled licences.
   - The installed build's ABOUT file reads "Google Chrome … Copyright 2026 Google LLC. All rights reserved".
   - Its `LICENSE.headless_shell` holds the Chromium and third-party texts.
   - Which terms apply should be settled. It is never redistributed.
6. **R9, owner question: Open-Meteo's free API is non-commercial.** Snow Globe was presented as a startup in the AWS Activate application (D-081). If its use is commercial, the M2 Open-Meteo path needs a paid plan or another route to the CC BY 4.0 data.
   - The lead proposed on 23 Nov reading NOAA's own GFS from NODD instead (ruling j, D-115). It costs nothing and reuses the HRRR worker's byte-range code, and it waits for the owner.
   - Resolved by D-115: PRD §8.1's "discontinued 2026-11-30" for Open-Meteo's HRRR had lost its basis, because the docs page now reads "Discontinues with future RRFSv2 release" (read twice on 23 Nov by the any-place research). §8.1 is corrected.
7. **R11, methods the code names that neither THIRD_PARTY_NOTICES 2 nor PRD §15.4 lists.** Section 2 lists seven: oculus-core, lowbias32, Acklam's approximation, the NOAA solar equations, Meeus, the Almanac's Moon formulas and the Hillaire port. Under its inclusion rule the ledger names 13 more, all in use:
   - in the app's code: FNV-1a 64-bit, Henyey–Greenstein, Cornette–Shanks, Bruneton's transmittance parameterisation and Koschmieder's law;
   - in tests and tools: SplitMix64, SSIM and Numerical Recipes' erfcc;
   - in eval and plates: VEIA, the statistics of `algo.statistics` (Wilson, Owen, the block bootstraps, Holm, Cohen's kappa, the Peirce skill score and Manzato 2007), the IAU 1976 precession and IAU 1982 sidereal time, Bennett's refraction and Vincenty's formula.
   - Two of them need a decision: Numerical Recipes' erfcc and Bruneton's code (the open items above).
   - Suggestion: name them in section 2, or say there that published methods in Snow Globe's own code are listed in the ledger.

**Resolved by `9d372bd`:**
- **R1, what runs.** THIRD_PARTY_NOTICES 4b and 4c now say that capture and forecast 0.3.0 run from the snapshot of `aee6f92`, since the post-gate switch (D-090).
- **R10, the Scope line.** It now names the renderer's and the overlay's direct dependencies.

**Noted, nothing to do:**
- **R7, precision: THIRD_PARTY_NOTICES 3a.** Its "42 third-party crates are reachable" is right across all targets, and its licence counts match. On this Mac 41 are built: crunchy 0.2.4 is reached only for other targets.
- **R8, metadata: the notices are right.** For five packages, the notices' licences agree with what the packages themselves say:
  - **python-dateutil and scipy:** their licence files, read here. Their METADATA is free text, so `licence_overrides` carries the reading.
  - **eccodes, findlibs and s3transfer:** the licence name in their METADATA ("Apache License Version 2.0", "Apache License 2.0"), which `ledger.py`'s name table reads as Apache-2.0.

## How the methods were found

These ran from the repository root at 21:38 UTC on Mon 23 Nov 2026, and again at 21:48 UTC with the same output, over the same files as the data-flow greps ([data-flows.md](data-flows.md), "How the hosts were found"), with identical output in bash and zsh:

```sh
# A. author-year citations (71 distinct strings)
git ls-files -co --exclude-standard -z -- workers eval renderer app plates bench infra spikes viewpoints .github Makefile \
  | xargs -0 grep -I -h -o -E "\b[A-Z][[:alpha:]'-]+( (&|and) [A-Z][[:alpha:]'-]+| et al\.)?,? \(?(1[6-9][0-9]{2}|20[01][0-9]|202[0-5])\b" \
  | sort | uniq -c | sort -rn
# B. named methods: hyphenated eponyms, possessive formulas and laws, and the acronyms the code uses (72 distinct strings)
git ls-files -co --exclude-standard -z -- workers eval renderer app plates bench infra spikes viewpoints .github Makefile \
  | xargs -0 grep -I -h -o -E "\b[A-Z][[:alpha:]]+[-–][A-Z][[:alpha:]]+\b|\b[A-Z][[:alpha:]]+'s (formula|law|constant|step|kappa|mapping|method|algorithm|approximation)|\b(FNV-1a|SplitMix64|SSIM|VEIA|PSS|erfcc)\b" \
  | sort | uniq -c | sort -rn
```

**What they found.** Every citation and named method is an item or one of `algo.textbook-techniques`' entries:
- Standish 1992 and ELP-2000/82 describe oculus-core's own models, so they sit under `code.oculus-core`.
- Sæmundsson 1986, in `app/core/src/solar.rs` and eval's `ephemeris.py`, is the refraction formula of `algo.meeus` (eq. 16.4). The app gets it from oculus-core's `refracted_alt`.
- Kasten–Young's air mass is named only as what renderer 0.3.0 replaced (`renderer/src/light.ts`), so it is not an item.
- The rest are not methods: dates and date ranges, HTTP header names, place names, locale tags, a font name, the JSON Schema draft, Horizons' own header text ("DATA-BASED 1962"), and lengths and durations ("Lanczos 1920 px", "From 1900 s").

**A and B again at 22:29 UTC,** while the app shell and the renderer's in-cloud work were still uncommitted:
- A gave the same output.
- B found 76 distinct strings, 4 more: three HTTP header names (`Access-Control`, `Allow-Origin`, `Cache-Control`) and a place-name test value (`GRAND-TETON`), all in the shell's files.
- B's count of Henyey–Greenstein rose from 1 to 8 with the in-cloud work (`algo.henyey-greenstein`'s note).

**A wider search, by name.** Greps A and B miss a method named with neither a year nor a hyphen. So, between 22:20 and 22:29 UTC, the same files were also searched for 167 method names, eponyms and standards by name. It found:
- one method from a specific work that was not an item: Kim and Pollefeys's model-free camera response, which PRD §9.4 plans for M2 (now `algo.kim-pollefeys`);
- public standards and techniques that `algo.textbook-techniques` now lists: WCAG 2, the CIE clear sky, Rec. 601 luma, SHA-256 (the renderer has its own implementation, `renderer/src/hash/sha256.ts`), bilinear, trilinear and bicubic interpolation, and Lambertian reflectance;
- otherwise, items, the textbook techniques already listed, Rayleigh and Mie scattering (part of Hillaire's atmosphere, `code.hillaire-2020`), and things that are not items: the NAVD 88 datum, the `EPSG:4326` code, and the CRC32 checksums in recorded S3 listings.

## Keeping it current

- **When a lock file changes:** run `ledger.py build`, read its new `disagreements`, then run `ledger.py check`.
- **When a new lock file is added:** `build` refuses it until `SHIPS` in [ledger.py](ledger.py) says where its packages go. Until it is added to git, `check` and `build` only name it in a note.
- **When a licence of a distributed package is new:** add its duty to `vocabulary.duty_by_licence`; `build` stops until it is there.
- **When a data source, model, service, port or cited method is added:** add an entry to `items` by hand, with its duty.
- **When another package's work in progress is committed** (the app shell, the renderer's in-cloud work, the plate builder's specs): re-run greps A and B and the data-flow greps, and refresh what they touch: data-flows.md's row 13 and hosts table, `algo.henyey-greenstein` and `algo.textbook-techniques`.
- **The tests:** `ledger.py check` is also `RealLedger` in [test_ledger.py](test_ledger.py), and `Check` runs `check()` on a throwaway repository, so it must report a missing package and an extra entry, not only pass, and must treat a tracked lock file deleted from disk as pinning nothing. No `make` target runs them yet.
