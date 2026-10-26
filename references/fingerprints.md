# Identities, fingerprints and engine limits

The pack never invents a new identity per request. One account gets one profile;
the profile is stored as JSON and the browser's persistent `user_data_dir`
keeps cookies and storage across runs.

## Engine matrix (verified 2026-10-24)

| Engine | Rendering | Fingerprint control | `add_init_script` | Best use |
|---|---|---|---|---|
| `camoufox` | Firefox 156 / Gecko | Full generated fingerprint: UA, platform, screen, WebGL, fonts, voices, media devices, canvas/audio noise seeds, WebRTC | Not needed | Turnstile, Cloudflare, fingerprint-sensitive |
| `patchright` | Chromium 153 | Real native values + Playwright context options: locale, timezone, screen size, DPR, proxy, persistent profile | **Ignored by design** | General JS signup, no heavy bot defense |
| `invisible` | Firefox / invisible_playwright | Full fingerprint engine (C++), humanized input, `profile_dir` persistence | Not needed | The strongest bundled option; used by `--engine auto` for stealth |
| `playwright` | Chromium (cached build) | Context options + init-script overrides (platform, hardware, screen avail, WebGL) | Works | When you explicitly need an override; easiest to detect |

`--engine auto` picks Camoufox only when the adapter sets `stealth: true`;
otherwise it picks Patchright. `invisible` is available explicitly.

## 2026 field notes from the source farms

These are observations from the October 2026 examples, not universal laws;
verify on the target.

- The main detection axis moved from static JS values to **automation-protocol
  fingerprints** (how CDP/Juggler is opened). A browser with perfect JS patches
  can still be caught before the first script runs.
- In the source benchmark (7 tools × 31 Cloudflare targets, 651 verdicts):
  **nodriver 28 OK / 0 blocked**, CloakBrowser 26, `curl_cffi` 26, Patchright
  25, Camoufox 25, vanilla Playwright 24 (= rebrowser-patches). Patchright can
  fail Cloudflare Turnstile because of its CDP patches; vanilla Playwright often
  loads the Turnstile widget where Patchright does not. Camoufox is the strong
  Gecko option; CloakBrowser is a 562 MB optional Chromium binary; nodriver is a
  separate async API and is not bundled here.
- New in October 2026: **invisible_playwright** (`pip install invisible-playwright`,
  Firefox fork, C++ fingerprint engine, humanized input, `profile_dir` persistence,
  5/5 detection suites). It is now a first-class engine: `--engine invisible`.
- **`invisible_playwright_mcp`** is an MCP server on the same engine: tools
  `browser_open`, `browser_navigate`, `browser_snapshot`, `browser_click`,
  `browser_click_at`, `browser_type`, `browser_take_screenshot`,
  `browser_evaluate`, `browser_read_text`, `browser_read_html`. If your agent
  prefers MCP over YAML adapters, register it and use those calls instead.
- **zendriver** (async nodriver fork) is installed and detected, but its async CDP
  API does not map cleanly onto this pack's persistent-context flow. Use it only
  if you are willing to drive it directly.
- This pack does not bundle nodriver. Practical mapping:
  - Turnstile widget fails to render under Patchright → retry with
    `--engine playwright`, or use Camoufox for a managed challenge.
  - Cloudflare managed challenge → Camoufox + residential IP, one headed pass.
  - Microsoft press-and-hold / JS challenges → Patchright or vanilla Playwright
    with a headed window.
- Headless is detectable on some targets. A headed window positioned offscreen
  (`--window-position=3200,3200`) is a common compromise, but the pack exposes
  only `--headed`; use it for the first pass and reuse the profile.
- The captcha solver and the signup must share the **same egress IP**. The
  engine now passes the account proxy into 2captcha/YesCaptcha/local tasks.
- Do not chase a perfect fingerprint on a datacenter IP. IP reputation and ASN
  dominate; use a clean residential/sticky proxy.

## What is actually controlled

### Camoufox

The generated fingerprint is persisted as a JSON-safe `camoufox_fp`. On reload,
the launcher reuses that identity instead of generating another one. fpgen-backed
Camoufox accepts the saved dict directly; BrowserForge-backed builds need the
`Fingerprint` dataclass reconstructed with its nested screen, navigator and
optional video-card dataclasses. The live `camoufox_fp_obj` cache is process-local
and excluded from profile files and public CLI output. Backend capabilities,
not the package version string, determine the representation. It pins:

- navigator UA/platform/languages/hardware concurrency/device memory;
- screen and window dimensions;
- WebGL vendor/renderer and parameters;
- fonts, voices, media devices, battery, DRM, codecs;
- canvas/audio noise seeds;
- WebRTC candidates (with proxy).

Locale and timezone are passed to the context. `block_webrtc` is enabled when a
proxy is in use. `humanize` moves the cursor like a person.

Generate it once and keep the profile. Regenerating a fingerprint for the same
account breaks continuity.
`run`, browser `verify`, and `profile fpcheck` save the generated identity,
including when verification or the fingerprint check fails after generation.
This also pins profiles created with `profile new` or older profiles with no
saved fingerprint on their first Camoufox launch.

Keep the same Camoufox backend for an existing profile. A saved fingerprint that
cannot be reconstructed by the installed BrowserForge schema raises a
configuration error; it is not silently replaced with a new identity.

### Patchright

Patchright removes the obvious automation leaks (`navigator.webdriver`,
CDP artefacts, automation flags) and is a real Chromium. It does **not** apply
Playwright init scripts. Therefore the profile is built from native values:

- `hardware.concurrency` = host CPU count;
- `screen.availHeight` = `screen.height` (what headless Chromium reports);
- `webgl` = null (native, not overridden);
- `deviceMemory` is compared leniently because headless may report `null`.

The pack still controls locale, timezone, screen width/height, DPR, proxy and
persistent storage. That is enough for ordinary signup pages, not for a
fingerprint vendor that probes GPU/canvas deeply.

### Playwright

Applies `init_script(profile)`: platform, languages, hardware concurrency,
device memory, screen width/height/avail/color depth, WebGL vendor/renderer,
and `navigator.webdriver = false`. It also sets locale, timezone, screen, DPR
and proxy through the context.

Use it only when an override is required. Stock Playwright is easier to detect
than Patchright and much easier than Camoufox.

## Profile fields

```json
{
  "id": "p_...", "seed": "...", "os": "windows",
  "locale": "en-US", "languages": ["en-US", "en"],
  "timezone": "Europe/Berlin",
  "screen": {"width": 1920, "height": 1080,
             "avail_width": 1920, "avail_height": 1040,
             "color_depth": 24, "pixel_ratio": 1.0},
  "viewport": {"width": 1920, "height": 955},
  "hardware": {"concurrency": 8, "memory": 8},
  "webgl": null,
  "platform": "Win32",
  "ua": "native",
  "geo": {"country_code": "DE", "timezone": "Europe/Berlin", "ip": "..."}
}
```

Generation is deterministic from `seed` for the profile's own fields. Camoufox
adds `camoufox_fp`.

## Consistency rules

1. **Timezone and locale must match the exit IP.** Set
   `identity.align_geo: true` (default). The proxy's exit geo is looked up and
   applied before the profile is created. A German IP with `Asia/Tokyo` is a
   stronger signal than any canvas noise.
2. **OS must match the UA.** Chromium refuses cross-OS claims without a
   matching UA; use Camoufox for cross-OS identities. Windows profile + Win32
   platform + Windows UA is consistent.
3. **Viewport must fit the screen.** The generator enforces
   `viewport <= screen`; do not hand-edit it into an impossible state.
4. **Do not add canvas/audio noise casually.** Noise that does not match the
   real GPU/audio stack is itself detectable. Camoufox handles noise coherently;
   Chromium defaults leave it native.
5. **One profile per account, forever.** Reusing a profile links accounts.
   A fresh account = fresh seed = fresh `user_data_dir`.
6. **Do not switch engines for the same profile.** Chromium and Firefox data
   directories are not interchangeable.

## Fingerprint check

```bash
python "$AUTOREG" --home . profile fpcheck <profile_id> --engine camoufox
```

The command launches the identity, reads live values (UA, platform, locale,
timezone, screen, DPR, hardware, WebGL, `webdriver`) and compares them to the
stored profile. It returns non-zero on mismatch.

Expected behavior:

- Camoufox: hardware/device-memory/WebGL are skipped (its own fingerprint wins);
  screen width/height, locale, timezone, platform and `webdriver` must match.
- Patchright: hardware concurrency, screen (including avail), DPR, locale,
  timezone, platform and `webdriver` must match native values.
- Playwright: all compared fields must match the init-script overrides.

If `fpcheck` fails, delete the profile directory for that engine and recreate
the profile. Never patch a stored profile JSON by hand to silence the check.

## Browser access ladder (context)

| Level | Tool | When |
|---|---|---|
| L1 | curl / HTTP | Static, no JS, no session |
| L3 | Playwright / Patchright | JS, standard sessions |
| L5 | Camoufox | Fingerprint-sensitive, bot defense |
| L7–L8 | Persistent anti-detect profiles | Long-lived identities |

The pack maps L3 to `patchright`, L5 to `camoufox`, and persistent identity to
the profile directory + proxy lease.

## Known limits

- A solver token cannot pass a Cloudflare **managed challenge**. Use a real
  browser, pass it once headed, then reuse the profile.
- Residential IP quality matters more than any JS patch. Datacenter IPs fail
  even with perfect fingerprints.
- Deep GPU/canvas/audio probes can still catch Chromium overrides. Camoufox is
  the answer for those targets; do not over-promise on Patchright.
- IP reputation, ASN and request pacing are outside the browser. Use sticky
  proxies per account and keep `limits.min_delay`.
- `profile fpcheck` validates internal consistency, not whether a third party
  considers the browser human.
