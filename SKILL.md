---
name: autoreg
description: >
  Build and run automated service registrations end to end: recon a signup flow,
  generate an adapter, drive real Chromium/Gecko with a persistent identity, read
  email OTP codes, solve or inject CAPTCHA tokens, route through proxies, and
  verify accounts. Use this skill whenever the user asks to auto-register or mass
  register on a service, create accounts, automate signup/registration, bypass
  browser fingerprint checks, wire temporary mail or SMS, or turn a manual signup
  flow into a repeatable script — even if they do not say "autoreg". Also use it
  for QA/test-account provisioning and for debugging an existing registration
  adapter.
---

# autoreg

A self-contained registration pack. The engine lives next to this file; project
state (profiles, encrypted accounts, ledgers, artifacts) lives in a per-project
`.autoreg/` directory.

```text
autoreg/
├── SKILL.md
├── scripts/autoreg.py          # CLI entry point
├── scripts/autoreg_pkg/        # engine (flow, browser, mail, captcha, recon)
├── scripts/mock_site.py        # local end-to-end target
├── assets/adapters/            # templates (mock, generic, protocol)
├── references/                 # load only the file you need:
│   ├── playbook.md             # first-try method, end to end
│   ├── preflight.md            # questions and checks before code
│   ├── adapters.md             # full YAML schema + browser_fetch
│   ├── recon.md                # report → adapter
│   ├── fingerprints.md         # engines, profiles, fpcheck
│   ├── mail.md                 # providers, aliases, code extraction
│   ├── providers.md            # mail/SMS/proxy/engine selection
│   ├── sms.md                  # SMS catalog + hook/API patterns
│   ├── captcha.md              # solvers, injection, proxy binding
│   ├── cases.md                # distilled October 2026 examples
│   ├── anti-fraud.md           # one-IP rule, pacing, error classes
│   ├── two-phase.md            # signup/confirm split
│   ├── gateway.md              # bank, keys, OpenAI-compatible shim
│   └── troubleshooting.md
└── tests/                      # unit + e2e
```

## Command prefix

Use the absolute path so the skill works from any project directory.

```bash
# POSIX
AUTOREG="$HOME/.agents/skills/autoreg/scripts/autoreg.py"
python "$AUTOREG" --home . doctor

# Windows PowerShell
$AUTOREG = "$env:USERPROFILE\.agents\skills\autoreg\scripts\autoreg.py"
python $AUTOREG --home . doctor
```

`--home` accepts either the project directory or its `.autoreg` directory.
`--json` is accepted before or after the subcommand.

## Non-negotiable workflow

Do not jump straight to `run` on an unknown site. The order below is what makes
the result repeatable instead of a one-off guess.

0. **Preflight.** Read `references/preflight.md` and answer the operator
   questions before code: mail, proxy, captcha, SMS, limits, goal.

1. **Doctor.**
   ```bash
   python "$AUTOREG" --home . doctor
   ```
   Confirms engines, dependencies, home permissions, the encrypted store and
   the default mail/captcha/proxy settings. If an engine is missing, fix that
   before writing an adapter.

2. **Initialize the project.**
   ```bash
   python "$AUTOREG" --home . init
   ```
   Creates `.autoreg/`, `config.yaml`, the key file, and copies adapter
   templates into `.autoreg/adapters/`.

3. **Recon the signup page.**
   ```bash
   python "$AUTOREG" --home . recon https://target.example/signup
   ```
   Produces `report.md`, `summary.json`, `skeleton.yaml`, `requests.jsonl`,
   `page.png`, `page.html` and (Chromium) `network.har`. Read the report; do not
   invent selectors. See `references/recon.md`.

4. **Write or edit one adapter.** Start from the generated skeleton or a
   template. Keep it in `.autoreg/adapters/`. The full schema is in
   `references/adapters.md`.

5. **Run one account headed.**
   ```bash
   python "$AUTOREG" --home . run .autoreg/adapters/target.yaml \
     --count 1 --headed --var base_url=https://target.example
   ```
   Watch the flow. On failure inspect
   `.autoreg/artifacts/<run_id>/<task>/attempt-*/failure.png|html|json`.

6. **Run headless and verify.**
   ```bash
   python "$AUTOREG" --home . run .autoreg/adapters/target.yaml --count 1
   python "$AUTOREG" --home . verify .autoreg/adapters/target.yaml --account <id>
   ```

7. **Only then scale.** Keep `--concurrency 1` until ten consecutive single
   runs are clean. The default `limits.min_delay` is 2 seconds; do not remove it
   to "go faster".

## Choosing the execution mode

| Target | Mode / engine | Why |
|---|---|---|
| Plain JSON/HTTP signup, no JS challenge | `mode: protocol` | Fast, cheap, browser-like TLS via `curl_cffi` |
| JS form, no bot defense | `mode: browser`, `patchright` | Real Chromium, persistent profile |
| Turnstile / fingerprint-sensitive / Cloudflare | `mode: browser`, `invisible` or `camoufox`, residential proxy | C++ fingerprint engine / Gecko-level spoofing |
| Need a custom init-script override | `mode: browser`, `playwright` | Only stock Playwright applies `add_init_script` in this pack |
| Interactive challenge you must pass by hand | `browser`, `--headed`, `captcha.provider: manual` | Human solves once, profile keeps the clearance cookie |

`--engine auto` resolves to Camoufox when the adapter sets `stealth: true`,
otherwise to Patchright. `--engine invisible` uses `invisible_playwright`
(Firefox fork, C++ fingerprint engine, humanized input). Protocol runs still
create a profile because the same account may need browser verification later.

Fingerprint rules, limits and the L1–L8 ladder: `references/fingerprints.md`.

## Service classes

The full method is in `references/playbook.md`. The October 2026 case gallery
is the fastest way to pick a shape. Read `references/cases.md` for the full
patterns; the short version:

| Class | Start with |
|---|---|
| Supabase / GoTrue (email + confirm link) | `assets/adapters/supabase-email-password.yaml`; `references/two-phase.md` |
| JSON-RPC/API behind Cloudflare | browser mode + `browser_fetch`; `assets/adapters/jsonrpc-browser.yaml` |
| Gmail `+alias` or catch-all domain | `imap` provider with `alias`/`catch_all`; `assets/adapters/gmail-alias-imap.yaml` |
| Signup and confirm split by email rate limit | `mail.defer: true` + `pending`; `assets/adapters/catchall-two-phase.yaml` |
| New-API fork / API-key extraction | protocol mode; `references/gateway.md` |
| Phone/SMS required | `hook` + `references/sms.md`; one number per account |
| Rate limit per exit IP | `proxy.strategy: per_account`, `proxy.health_check: true`; `references/anti-fraud.md` |
| Turnstile widget fails under Patchright | `--engine playwright` or Camoufox; `references/captcha.md` |

## Adapter in one screen

```yaml
name: target-signup
mode: browser
stealth: true
vars:
  signup_url: "{{base_url}}/signup"
mail:
  provider: cloudflare_temp_email
  code_regex: '(?<!\d)(\d{6})(?!\d)'
  subject_contains: "verification"
captcha:
  provider: 2captcha
  type: turnstile
steps:
  - goto: "{{signup_url}}"
  - wait_for: "input[type=email]"
  - fill: {target: "input[type=email]", value: "{{email}}"}
  - fill: {target: "input[type=password]", value: "{{password}}"}
  - click: "button[type=submit]"
  - otp: {target: "input[autocomplete=one-time-code]"}
  - captcha: {provider: 2captcha}
  - click: "button[type=submit]"
  - wait_for: "text=Welcome"
verify:
  url: "{{base_url}}/welcome"
  selector: "text=Sign out"
```

Variables available to every adapter: `{{email}}`, `{{password}}`,
`{{username}}`, `{{first_name}}`, `{{last_name}}`, `{{full_name}}`,
`{{service}}`, `{{run_id}}`, `{{profile_id}}`, plus `vars` and `--var`.
`{{otp}}` and `{{otp_link}}` appear after an `otp` step.

## Mail, captcha, proxy

- **Mail** providers: `mock`, `voidash`, `cloudflare_temp_email`, `mailtm`,
  `mailgw`, `imap`, `outlook_graph`, `custom_http`. IMAP supports Gmail `+alias` and
  catch-all domains (`alias`, `catch_all`, `search`, `mailboxes`). `mail.defer:
  true` stores a `pending` account for later confirmation. Configure credentials
  in `.autoreg/config.yaml` using `${ENV_VAR}`. Details and API contracts:
  `references/mail.md`.
- **Captcha** providers: `none`, `empty`, `static`, `2captcha`, `yescaptcha`,
  `local`, `manual`. A solver returns a token; the engine injects it into
  `cf-turnstile-response`, `h-captcha-response` or `g-recaptcha-response` and
  calls the widget callback. The solver task carries the account proxy, so the
  token is bound to the same exit IP. Try `empty` first on New-API forks. A
  solver token cannot pass a Cloudflare *managed challenge* — use Camoufox with
  a real profile for that. Details: `references/captcha.md`.
- **Proxy**: `direct`, a single URL, or a pool in config. `per_account` is
  sticky by task; `align_geo` rewrites the profile timezone/locale to the exit
  IP. Credentials are encrypted in the account store.
- **Human input**: trusted CDP mouse (`mouse_move`/`mouse_click`/`mouse_drag`/
  `press_hold`), React-friendly `fill_native`, OTP boxes via `fill_code`, plus
  `solve_image_captcha` (grid → VLM → click), `solve_audio_captcha`
  (reCAPTCHA audio → STT), and `click_by_vision` (screenshot → VLM point →
  trusted click). Mouse coordinates are page-absolute.
- **SMS**: the engine has an action hook, not a built-in provider. If the target
  needs a phone, add an HTTP/SMS provider in a `hooks.py` and call it with
  `- action: hook, name: ...`. Provider catalog, API shapes and a working
  HeroSMS hook: `references/sms.md`. Do not fake a successful phone step.

## Verification and artifacts

- Every attempt writes `.autoreg/runs/ledger.jsonl` and a DB row in
  `.autoreg/accounts.db`.
- Passwords and proxy credentials are Fernet-encrypted; `accounts list` never
  prints them. `accounts export --secrets` is explicit and should be avoided.
- `verify --all` re-opens the stored profile, checks the adapter's `verify`
  block, and marks accounts `verified`/`invalid`.
- `profile fpcheck <id> --engine <engine>` launches the identity and compares
  the browser's live values to the stored profile. A non-empty `mismatches`
  means the identity is inconsistent — fix it before running batches.

## Failure protocol

1. Read stderr: the engine logs the step number and action.
2. Read `failure.json` in the attempt directory (URL, step, error, profile,
   proxy). Screenshot and HTML are next to it.
3. Classify before changing code:
   - selector/flow → re-run recon headed;
   - mail timeout → check sender/subject filters, baseline IDs, provider 5xx;
   - captcha → check sitekey, widget type, callback, solver balance;
   - proxy → `proxy check <url>`, exit geo vs profile timezone;
   - fingerprint challenge → switch to Camoufox + residential IP;
   - `RenderError` → a variable was not passed or not stored.
4. `references/troubleshooting.md` has the full symptom → cause → fix table.

## Requirements

- Python 3.13+, `playwright`, `patchright`, `camoufox`, `curl_cffi`, `PyYAML`,
  `cryptography`, `requests`.
- `invisible_playwright` for `--engine invisible` (`pip install invisible-playwright`).
- Optional: `nodriver` (28/31 in the source Cloudflare benchmark), `cloakbrowser`
  (562 MB binary), `zendriver` (async nodriver fork; detected, not integrated).

## Rules that prevent most failures

- One account = one persistent profile. Never share a profile between accounts
  or reuse a profile for a different service.
- Take selectors from recon, not from memory. If a page has several forms,
  scope with `#form-id input[...]` or a locator mapping.
- Prefer `wait_for` over `sleep`. `sleep` is only for deliberate pacing.
- Keep signup POSTs non-retryable (`retries: 0`) unless the target is
  idempotent; a retried registration can create duplicates.
- Do not put passwords, API keys or tokens in adapter YAML. Use `${ENV}` in
  config and let the store encrypt account secrets.
- Do not raise concurrency to hide a flaky flow. Fix the flake.
- The bundled `mock_site.py` is the regression bed. After editing engine code:
  ```bash
  python .agents/skills/autoreg/scripts/autoreg.py --home /tmp/autoreg-selftest \
    selftest --engine patchright
  ```
  A pass means signup, OTP, captcha injection, verification, fingerprint check
  and encrypted storage all still work.

## Authorized use

This pack automates account creation. Use it only on services where you are
authorized to create accounts, and respect the target's terms, rate limits and
local law. The operator is responsible for what the accounts are used for.
