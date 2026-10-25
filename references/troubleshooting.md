# Troubleshooting

## First three commands

```bash
python "$AUTOREG" --home . doctor
python "$AUTOREG" --home . accounts list
tail -n 50 .autoreg/runs/ledger.jsonl
```

Then open the newest attempt directory:

```text
.autoreg/artifacts/<run_id>/<task>/attempt-<n>/
  failure.json   error, URL, step, action, profile, proxy
  failure.png    page at failure
  failure.html   DOM
```

The run ledger is JSONL; `failure.json` is the fastest path to the step.

## Symptom → cause → fix

| Symptom | Cause | Fix |
|---|---|---|
| `Executable doesn't exist ... chromium_headless_shell-1228` | Playwright 1.61 expects build 1228; only 1243 is cached | Use `patchright` or `camoufox`; for `playwright`, the pack auto-falls back to the cached 1243 executable. If it still fails: `python -m playwright install chromium` |
| `CamoufoxNotInstalled ... pairs with ...` | The installed `camoufox` package pins one browser version; the cache holds another | Run `python -m camoufox fetch`, kill stale `camoufox.exe` processes first |
| `navigator.webdriver` is `true` | Stock Playwright / missing patch | Use `patchright` or `camoufox`; the init script patches it for `playwright` only |
| Fingerprint check says `hardwareConcurrency mismatch` | Patchright ignores init scripts; profile was generated with spoofed hardware | Recreate the profile with the same engine; the pack now uses native values for Patchright |
| Fingerprint check says `screen.availHeight` mismatch | Chromium ignores the prototype patch; profile had a taskbar-adjusted height | Recreate with Patchright (avail = height); use Camoufox for realistic taskbar geometry |
| `RenderError: Unknown variable {{base_url}}` | Verify/step rendered without the run's `--var`; vars were not stored | Pass `--var base_url=...`; the runner now stores safe vars in account metadata for `verify` |
| `InvalidURL ... Bad hostname` in protocol verify | A `{{var}}` was not rendered | Same as above; ensure `--var` or adapter `vars` supplies it |
| Mail timeout, inbox empty | Provider never sent, wrong address, or retention evicted it | Check `mail test`, sender/subject filters, and the provider's inbox directly |
| Mail timeout, inbox has unrelated mail | Filter too broad/narrow or baseline wrong | Tighten `sender_contains`/`subject_contains`; keep `arm_at_start`; use a fresh mailbox per account |
| Mail timeout, diagnostics show 5xx | Provider cannot read right now | Fix the Worker/provider; retrying the flow will not help |
| HTTP 4xx from mail provider | Wrong path/key/address format | Fix config; 4xx is not retried |
| `captcha sitekey not found` | Widget loads late or inside an iframe/shadow root | Run recon after settle; set `sitekey` explicitly; add a hook for shadow DOM |
| Solver returns a token but the form rejects it | Token bound to another IP/page/action, or the site reads a callback not the field | Use the same proxy for solver and browser; set `callback`; solve on the exact URL |
| Cloudflare "Checking your browser" persists | Managed challenge, not a widget | Use Camoufox + residential IP + `manual` once headed; reuse the profile |
| `Proxy probe failed` / HTTP 407 | Dead proxy or bad credentials | `python "$AUTOREG" --home . proxy check <url>`; rotate; check auth encoding |
| Timezone/locale mismatch after proxy change | Profile was created before the proxy was known | Recreate the account/profile with `align_geo: true`; never edit profile JSON by hand |
| `user data directory is already in use` | Another browser is using the profile | Close the other run/browser; one process per profile |
| `Cannot decrypt password: wrong key` | `.autoreg/key` changed or DB copied from another project | Restore the original key; do not regenerate it over an existing DB |
| `sqlite3.OperationalError: database is locked` | Too many writers / long transaction | Keep concurrency low; the store uses WAL; do not open the DB in another tool while a batch runs |
| Account stored as `unverified` | Flow succeeded, verify selector/URL wrong or session cookie not persistent | Fix `verify`; for cookies, ensure the site sets `Max-Age`/`Expires` (session cookies do not survive a browser restart) |
| Recon finds no forms | SPA renders after settle, or the form is in an iframe | Increase `--settle`, use `--headed`, inspect `page.html` and `network.har` |
| `network.har` missing | Camoufox/Firefox path or HAR write failed | Use Chromium for recon; `requests.jsonl` is still captured |
| 429 / "too many requests" | Pacing too fast | Raise `limits.min_delay`, keep `--concurrency 1`, add per-account proxy |
| Everything works once, then all fail | IP burned, profile linked, or domain flagged | New proxy, new profiles, slower pacing; do not reuse the same mailbox domain |
| `window.turnstile` is undefined under Patchright | Cloudflare detects Patchright's CDP patches | Retry with `--engine playwright`, or use Camoufox for a managed challenge |
| Captcha token is rejected despite a solver success | Token solved from a different IP, or missing `action`/sitekey binding | Pass the account proxy to the solver; set `action`; solve on the exact page URL |
| An empty turnstile is accepted | The fork does not validate it | Use `captcha.provider: empty` and skip the paid solver |
| Account stays `pending` after a run | `mail.defer: true` was set; phase B has not run | Run `verify <adapter> --all` later |
| Deferred verify cannot read mail | Provider is temp-mail (`create: false` only works for IMAP/Graph/custom fixed) | Use IMAP/catch-all or Outlook Graph for deferred flows |
| IMAP finds no mail for a Gmail alias | Search is `ALL`, or the alias was normalized | Set `search: 'TO "{address}"'`; add `[Gmail]/All Mail`; check `To`/`Delivered-To` |
| Gmail dot alias is rejected as duplicate | Gmail canonicalizes dots; the site normalizes them | Use `+tag` aliases only where allowed, or a catch-all domain |
| Confirm link returns `otp_expired` | Link expired or was already consumed | Resend, wait, re-sweep; do not reuse an old link |
| Supabase verify returns 400 | Link parameter is `token`, API field is `token_hash` | Map `token` → `token_hash`; include `type=signup` |
| Email send returns 429 | Site-wide email rate limit | Split signup/confirm phases; wait; do not hammer resend |
| `browser_fetch` fails with a CORS error | Target API does not allow the page origin | Use the same origin the site uses, or protocol mode with the correct headers |
| New-API key is masked (`sk-****`) | List endpoint returns a mask | Fetch `POST /api/token/{id}/key`; validate the full key |
| Many accounts fail with the same 401/403 | Provider-wide incident, not per-account | Pause the pool; do not disable accounts one by one |
| `solve_image_captcha` returns no indices | Vision model gave no tile numbers | Check `vision.base/key/model`, tighten the prompt, or use a hook |
| `solve_audio_captcha` cannot find the audio button | The reCAPTCHA iframe is not visible or not present | Use a real browser session, not headless, and inspect the frame |
| `fill_native: not found` | Selector wrong or the field is inside an iframe/shadow root | Use the shadow-root hook or `page.evaluate` with a recursive query |
| `mouse_click` clicks the wrong point | Coordinates are page-absolute; scroll/zoom moved them | Prefer a `target` selector; re-run recon if the layout changed |

## Artifact inspection

```bash
# last 20 ledger events
python - <<'PY'
import json
for line in open('.autoreg/runs/ledger.jsonl', encoding='utf-8').read().splitlines()[-20:]:
    e=json.loads(line); print(e.get('status'), e.get('service'), e.get('email'), e.get('error'))
PY
```

`failure.json` fields: `error`, `url`, `step`, `action`, `email`, `profile`,
`proxy`. The step number matches the adapter's `steps` order.

## Debug ladder

1. Same adapter, `--count 1 --headed`: watch the actual page.
2. Add `- action: screenshot` before the failing step.
3. Replace a selector with a broader one (`input[type=email]`) to confirm the
   page state, then narrow.
4. Switch engine:
   - Patchright failed on a challenge → Camoufox;
   - Camoufox failed on a normal form → Patchright.
5. Run `profile fpcheck` for the account's profile.
6. Run `mail test --provider ... --wait` with the same filters.
7. If protocol mode fails with a WAF/TLS error, switch to browser mode; if
   browser mode fails on a plain JSON endpoint, switch to protocol.

## Cleanup after a bad batch

- `accounts delete <id> --yes` removes the DB row (profiles/artifacts stay).
- `profile delete <id> --yes` removes the profile JSON and browser data.
- Do not delete the whole `.autoreg/` unless you intend to lose the encryption
  key and every stored account.
- Keep one failing artifact directory when reporting a bug; it contains everything
  needed to reproduce without rerunning the target.

## Regression check

After changing engine code:

```bash
python "$AUTOREG" --home /tmp/autoreg-selftest selftest --engine patchright
python "$AUTOREG" --home /tmp/autoreg-selftest-cf selftest --engine camoufox
python "$AUTOREG" --home /tmp/autoreg-selftest-pw selftest --engine playwright
```

The selftest covers signup, OTP, captcha injection, verification, fingerprint
compare and encrypted storage against `mock_site.py`.
