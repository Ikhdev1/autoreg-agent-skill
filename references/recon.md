# Recon: from a signup URL to an adapter

```bash
python "$AUTOREG" --home . recon https://target.example/signup --settle 3
```

Optional flags: `--engine patchright|camoufox|playwright`, `--headed`,
`--proxy`, `--out <dir>`, `--settle <seconds>`.

Recon opens the page with a fresh identity, records network traffic, waits for
the page to settle, extracts forms/controls and writes a report plus a skeleton.

## Output

| File | Use |
|---|---|
| `report.md` | Start here: protections, form tables, candidate endpoints, next steps |
| `summary.json` | Machine-readable forms, controls, endpoints, cookies, console |
| `skeleton.yaml` | Runnable adapter draft; every guess is marked TODO |
| `requests.jsonl` | Method, URL, type, post body (redacted), headers (auth/cookie masked) |
| `responses.jsonl` | Status and content type per request |
| `page.png` | Full-page screenshot |
| `page.html` | DOM after settle |
| `network.har` | Chromium only; embeds response bodies |
| `console.json` | First 100 console messages |
| `profile/` | Recon identity; not for production accounts |

## Reading the report

1. **Bot protection.** Names such as `cloudflare_turnstile`, `hcaptcha`,
   `recaptcha`, `datadome`, `perimeterx`, `kasada`, `akamai`, `imperva` come
   from request URLs, script/iframe sources and the DOM. Absence of a signature
   is not proof: many sites load the challenge only after the first POST.
2. **Forms.** Each form lists tag/type/name/id/placeholder/visibility. Use the
   most specific stable selector: `#id`, then `[name=...]`, then a scoped CSS
   path. Avoid text selectors that change with localization.
3. **Guessed controls.** The report highlights email/password/code/name/submit.
   Treat these as hints, not facts.
4. **Candidate endpoints.** Sorted by a heuristic score (hint keywords, POST
   body, XHR/fetch). Use these for protocol mode. Open `requests.jsonl` to see
   the actual body shape.
5. **Cookies.** Names only; values are not reported. A pre-existing `cf_clearance`
   or session cookie means the flow depends on a prior page visit.

## Building the adapter

1. Copy `skeleton.yaml` or a template from `.autoreg/adapters/`.
2. Replace `name`, `vars.signup_url`, and every `TODO`.
3. Choose the mode:
   - `protocol` if the signup is a JSON/form POST with no JS challenge;
   - `browser` otherwise.
4. Choose the engine:
   - no bot defense → `patchright` (real Chromium, persistent profile);
   - Turnstile/fingerprint-sensitive → `stealth: true` (Camoufox);
   - need init-script overrides → `playwright`.
5. Fill `mail` with the provider and filters. A code regex is not optional: the
   default is a bare 6-digit number, which is wrong for alphanumeric codes.
6. Add `verify` with a success signal that survives a reload: a dashboard
   selector, a token in storage, or an API response.
7. Run one account headed. Fix selectors against the real page, then run
   headless.

## Protection-specific notes

| Finding | Practical response |
|---|---|
| `cloudflare_turnstile` | Solve with 2captcha/YesCaptcha/local, or run Camoufox + residential proxy + `manual` once |
| `cloudflare_challenge` | A token solver cannot pass it; use a real browser profile, pass the challenge headed, reuse the profile |
| `hcaptcha` / `recaptcha` | Same solver flow; inject the response field and call the callback |
| `datadome` / `perimeterx` / `kasada` | High-risk; expect device telemetry. Prefer Camoufox, clean residential IP, one account per profile, slow pacing |
| `akamai` / `imperva` | Often TLS/HTTP2 fingerprint checks; protocol mode with `curl_cffi` may fail, browser is safer |
| None detected | Still assume a server-side risk engine; keep pacing and per-account IPs |

## Limits

- Recon captures one page state. Multi-step flows (email first, then password)
  need a second recon after the first step, or use `--headed` and walk the flow
  manually while watching the requests.
- The skeleton's success selector is a placeholder. Replace it.
- `network.har` can contain personal data; treat the recon directory as
  sensitive and do not commit it.
- Recon reuses a per-run profile directory only for that run; do not point a
  production account at it.
