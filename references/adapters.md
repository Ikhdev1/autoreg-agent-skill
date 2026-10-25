# Adapter schema

An adapter is one YAML file. `autoreg adapters validate <path>` checks it
without touching the network.

## Top level

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Service id used in the store, ledger and artifacts |
| `description` | no | Human note |
| `mode` | no | `browser` (default) or `protocol` |
| `stealth` | no | `true` prefers Camoufox when `--engine auto` |
| `engine` | no | `auto`, `camoufox`, `patchright`, `playwright`, `invisible` (CLI wins) |
| `attempts` | no | Total attempts per account (default from config `limits.attempts`) |
| `vars` | no | Adapter variables; may reference CLI vars |
| `identity` | no | `profile` (fixed id), `seed`, `os`, `locale`, `timezone`, `languages` |
| `proxy` | no | Per-adapter proxy override; normally use config/CLI |
| `mail` | if OTP | Mail provider spec and code filters |
| `captcha` | no | Default captcha spec, merged into each `captcha` step |
| `steps` | yes | Ordered action list |
| `verify` | no | Mapping or step list |
| `hooks` | no | Path to a Python file with named hook functions |
| `protocol` | protocol mode | `impersonate`, `headers` |

## Variables

Runtime variables: `email`, `password`, `username`, `first_name`, `last_name`,
`full_name`, `service`, `run_id`, `task`, `profile_id`, `proxy`.

After an `otp` step: `otp`, `otp_link`, `otp_sender`, `otp_subject`.
Protocol mode also sets `last_status`, `last_json`, `last_text`,
`last_headers`, `last_response`, and `cookies` after the flow.

Use `{{name}}`. A whole-string placeholder keeps the value's type; a substring
placeholder stringifies it. Missing variables raise `RenderError` — they are
never silently replaced with an empty string.

## Step syntax

Compact form: exactly one action key.

```yaml
- goto: "{{signup_url}}"
- wait_for: "input[type=email]"
- fill: {target: "input[type=email]", value: "{{email}}"}
- click: "#submit"
```

Explicit form, needed for `optional`, per-step `timeout`, `retry` or `as`:

```yaml
- action: fill
  target: "input[name*=name i]"
  value: "{{full_name}}"
  optional: true
  timeout: 10
  retry: {times: 2, delay: 1}
```

`optional: true` logs and continues when the step fails. Do not use it to hide
the submit or the success assertion.

Use `engine: invisible` for the strongest bundled anti-detect path
(`pip install invisible-playwright`); it uses a C++ Firefox fingerprint engine
with humanized input and a persistent profile directory.

## Actions (browser mode)

| Action | Parameters | Notes |
|---|---|---|
| `goto` | `value`/`url`, `wait_until` | default `domcontentloaded` |
| `wait_for` | `target`/`value`, `state`, `timeout` | state `visible` default |
| `wait_url` | `value`/`url`, `timeout` | glob or regex accepted by Playwright |
| `fill` | `target`, `value`, `human`, `delay` | clears first; `human` clicks and types |
| `type` | `target`, `text`, `delay` | append-style typing |
| `click` | `target`, `button`, `click_count` | auto-waits |
| `press` | `target`, `key` | e.g. `Enter` |
| `check` / `uncheck` | `target` | checkboxes |
| `select` | `target`, `value` (string or list) | native `<select>` |
| `mouse_move` | `x`/`y` or `target`, `steps`, `jitter` | Bézier mouse path over the trusted CDP input |
| `mouse_click` | `x`/`y` or `target` | human mouse click (used by some strict targets) |
| `mouse_drag` | `x`/`y` or `target` + `to_x`/`to_y`/`to` | human drag for sliders |
| `press_hold` | `x`/`y` or `target`, `duration_ms`, `tremor_px` | press-and-hold with tremor |
| `fill_native` | `target`, `value` | React-friendly native setter + input/change |
| `fill_code` | `target` optional | fills single-cell OTP boxes; falls back to one input |
| `browser_fetch` | `method`, `url`, `headers`, `json`/`body`, `expect`, `save`, `credentials`, `timeout` | fetch inside the page; use when direct HTTP is CF-blocked |
| `solve_image_captcha` | `target`, `prompt`, `tile_selector`, `max_index` | screenshot grid → VLM → click tiles |
| `click_by_vision` | `target`, `description` | screenshot element/page → VLM point → trusted click |
| `solve_audio_captcha` | — | reCAPTCHA audio → STT → fill → verify |
| `solve_slider` | — | hook required (gap detect + drag) |
| `otp` | `target`, `timeout`, `code_regex`, `link_regex`, `sender_contains`, `subject_contains`, `follow_link` | fetches and optionally fills |
| `captcha` | merged captcha spec, `click`, `timeout` | solves and injects |
| `sleep` | `value`/`seconds` | seconds |
| `eval` | `js`, `args`, `as` | result stored when `as` is set |
| `extract` | `as` plus `js` / `selector+attr` / `regex+from` | see below |
| `assert` | selector, text, url, JS or variable assertion | see below |
| `screenshot` | `value`/`name`, `full_page` | saved in attempt artifacts |
| `log` | `value`/`message` | stderr, redacted |
| `set` | mapping, or `key`+`value` | sets variables |
| `mail_arm` | — | snapshots current mailbox IDs |
| `hook` | `name` | calls a Python hook |

### Targets

A target is a Playwright selector string or a mapping:

```yaml
target: "#email"
target: "text=Continue"
target: {role: button, name: "Create account", exact: true}
target: {label: "Password"}
target: {placeholder: "Enter code"}
target: {test_id: "submit"}
target: {css: "form#signup input[name=email]"}
target: {xpath: "//button[contains(., 'Next')]"}
target: {css: ".item", nth: 2}
target: {css: ".item", last: true}
```

By default the first match is used. Add `strict: true` only when you want a
strict-mode error for multiple matches. `nth`/`last` are applied before the
default first.

### In-page fetch

Some targets return Cloudflare 403 to direct HTTP but accept the same JSON-RPC
from their own page. `browser_fetch` runs `fetch()` with the page's cookies,
origin and sessionStorage:

```yaml
- eval: "() => sessionStorage.getItem('AUTH_TOKEN')"
  as: auth_token
- browser_fetch:
    method: POST
    url: "https://target.example/rpc"
    headers: {x-auth-token: "{{auth_token}}"}
    json: {id: 310, query: {}}
    expect: 200
    save: rpc
- extract: {from: rpc, path: result.token, as: token}
```

It stores `last_status`, `last_json`, `last_text`, `last_headers` and
`last_response`, so protocol-style `extract`/`assert` work unchanged.
`credentials: include` is the default; cross-origin calls still obey CORS.

### OTP

```yaml
- otp:
    target: "input[autocomplete=one-time-code]"
    timeout: 180
    code_regex: '(?<!\d)(\d{6})(?!\d)'
    sender_contains: "no-reply@example.com"
    subject_contains: "verification"
```

The baseline is taken at flow start (`mail.arm_at_start`, default true). If the
flow reuses a long-lived mailbox, keep that default. Put `mail_arm` immediately
before the action that triggers the email if the target sends mail on page load.

If no `target` is given, the code is only stored in `{{otp}}`. If `link_regex`
matches and `follow_link` is true, the browser navigates to the link.

### Extract

```yaml
- extract: {as: token, js: "() => localStorage.getItem('token')"}
- extract: {as: href, selector: "a.confirm", attr: "href"}
- extract: {as: csrf, selector: "input[name=_csrf]", attr: "value"}
- extract: {as: code, from: last_text, regex: "code=(\\w+)"}
- extract: {as: token, from: last_json, path: data.access_token}
- extract: {as: token, from: rpc, path: result.token, default: ""}
```

`path` accepts dot-separated keys and list indices. `from` defaults to
`last_json`; it can also be a saved variable or a literal object.

`from` for regex extraction: `page` (HTML), a variable name, or a literal.
`group` defaults to 1 when the regex has groups, otherwise 0.

### Assert

```yaml
- assert: "text=Welcome"                 # selector visibility, compact form
- assert: {selector: "#dashboard", visible: true}
- assert: {text: "Welcome", contains: true}
- assert: {url_contains: "/dashboard"}
- assert: {js: "() => document.cookie.includes('session')"}
- assert: {var: token, exists: true}
```

### Captcha

```yaml
captcha:               # adapter default, merged into every captcha step
  provider: 2captcha
  type: turnstile
  sitekey: "0x4AAA..."
  page_url: "{{signup_url}}"
  action: signup
```

```yaml
- captcha:
    provider: yescaptcha
- captcha: {provider: manual, click: true}
```

Provider `none` skips solving. `manual` optionally clicks the widget and waits
for the token field to be populated. `static` uses `token` (tests).

## Protocol mode

```yaml
mode: protocol
protocol:
  impersonate: chrome          # curl_cffi profile; "none" disables
  headers:
    Accept: application/json
```

| Action | Parameters |
|---|---|
| `request` | `method`, `url`, `headers`, `params`, `json`, `data`, `expect`, `allow_redirects`, `save`, `retries`, `timeout` |
| `otp` | same filters as browser mode; no fill |
| `extract` | `from`, `path` or `regex`, `group`, `as`, `default`, `required` |
| `assert` | `status`, `json_path`+`equals`/`exists`, `text_contains`, `var` |
| `set`, `log`, `sleep`, `mail_arm`, `hook` | same as browser |

`expect` accepts an int or a list. Without it, 2xx/3xx pass. `save: name` stores
the parsed JSON (or text) under `{{name}}`.

```yaml
- request:
    method: POST
    url: "https://example.com/api/signup"
    json: {email: "{{email}}", password: "{{password}}"}
    expect: [200, 201]
- otp: {timeout: 180}
- request:
    method: POST
    url: "https://example.com/api/verify"
    json: {email: "{{email}}", code: "{{otp}}"}
    expect: 200
- extract: {from: last_json, path: data.token, as: token}
```

Do not set `retries` on a non-idempotent signup POST.

## Verify

```yaml
verify:
  url: "{{base_url}}/welcome"
  selector: "text=Sign out"
  text: "Welcome"
  url_contains: "/welcome"
  js: "() => !!localStorage.getItem('token')"
  timeout: 20
```

A list of steps is also accepted and runs in the same browser context:

```yaml
verify:
  - goto: "{{base_url}}/settings"
  - wait_for: "text=Profile"
```

Protocol verify:

```yaml
verify:
  request:
    method: GET
    url: "{{base_url}}/api/me"
    headers: {Authorization: "Bearer {{token}}"}
    expect: 200
```

Verification failure does not discard the account; it is stored as
`unverified` with `verify_error` in its metadata. An account from a
`mail.defer: true` flow is stored as `pending`; `verify` moves it to
`verified` or `invalid`.

## Hooks

Point `hooks` at a Python file next to the adapter:

```yaml
hooks: hooks.py
steps:
  - action: hook
    name: solve_pow
```

```python
def solve_pow(ctx):
    challenge = ctx.vars["challenge"]
    # ctx.page, ctx.http, ctx.vars, ctx.log()
    return {"pow": compute(challenge)}
```

A hook receives `(ctx)` or `(ctx, page)` depending on its signature. A returned
dict is merged into the variables. Hooks are arbitrary code: review them before
running an adapter from an untrusted source.

## Mail section

```yaml
mail:
  provider: cloudflare_temp_email
  code_regex: '(?<!\d)(\d{6})(?!\d)'
  link_regex: 'https://[^\s"]+/(?:confirm|verify)[^\s"]*'
  sender_contains: ""
  subject_contains: "verification"
  timeout: 180
  interval: 2
  arm_at_start: true
  # provider-specific keys follow; see references/mail.md
```

`provider` may also be an inline mapping. `mail` is required when any `otp`
step exists.

For two-phase targets add `defer: true` and configure a mailbox that can be
re-read later (IMAP/catch-all/Outlook Graph):

```yaml
mail:
  provider: imap
  create: false
  defer: true
  search: 'TO "{address}"'
  alias:
    template: "{user}+{tag}@gmail.com"
    tag_prefix: reg
```

The runner stores the account as `pending` and skips the verify block. Later,
`autoreg verify <adapter> --all` opens the stored profile, builds the same mail
provider, sets the mailbox to the stored address, arms it, and runs the verify
steps. `create: false` states that the account address is an existing mailbox;
`verify` also recognizes IMAP/Graph providers without it.

## Identity section

```yaml
identity:
  profile: p_existing123        # reuse a stored profile (advanced)
  seed: "stable-seed"           # otherwise defaults to the account email
  os: windows                   # windows|macos|linux
  locale: en-US
  timezone: Europe/Berlin
  languages: [en-US, en]
```

Do not set `profile` to share one identity across accounts. Fixed profiles are
for re-running the same account.

## Interpolation and secrets

- `${ENV}` expansion happens in `config.yaml` only.
- `{{var}}` expansion happens in adapter values at runtime.
- Never put a password or API key in the adapter. The engine generates the
  password and stores it encrypted.
