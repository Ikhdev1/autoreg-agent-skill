# CAPTCHA solving and token injection

## Providers

| `provider` | Needs | Task types |
|---|---|---|
| `none` | — | Skip (no widget, or browser passes on its own) |
| `empty` | — | Send `turnstile=""`; some New-API forks accept it |
| `static` | `token` | Tests only |
| `2captcha` | `key` | Turnstile, hCaptcha, reCAPTCHA v2 |
| `yescaptcha` | `key` | Turnstile, hCaptcha, reCAPTCHA v2 |
| `local` | `base` | Theyka-compatible `createTask`/`getTask`, or `mode: 2captcha` |
| `manual` | headed browser | Human solves; engine waits for the token |

```yaml
captcha:
  default: 2captcha
  providers:
    2captcha: {key: ${TWOCAPTCHA_KEY}}
    yescaptcha: {key: ${YESCAPTCHA_KEY}}
    local: {base: http://127.0.0.1:8889}
```

Adapter-level default:

```yaml
captcha:
  provider: 2captcha
  type: turnstile
  sitekey: "0x4AAAAAAA..."
  page_url: "{{signup_url}}"
  action: signup
  callback: onTurnstileSuccess
  timeout: 180
```

Step-level overrides merge over the adapter default:

```yaml
- captcha: {provider: manual, click: true}
```

If `type` or `sitekey` is missing, the engine inspects the DOM and iframes:

- `.cf-turnstile` / `[data-sitekey]` → turnstile;
- `.h-captcha` → hcaptcha;
- `.g-recaptcha` → recaptcha;
- iframe host and `sitekey`/`k` query parameters are used as fallback.

Always set `sitekey` explicitly when recon found it; detection is a fallback.

**Try `provider: empty` before paying a solver.** Some New-API forks accept
`turnstile=""`; the request simply carries an empty field. If it is rejected,
switch to a real solver. Do not try an empty token on a managed challenge.

**Bind the solver to the signup IP.** The runner passes the account proxy into
2captcha/YesCaptcha/local tasks. A token solved from a different IP is rejected
or silently lowers the trust score. `proxy.required: true` prevents a direct
fallback.

**Enterprise reCAPTCHA with `action`:** the server rejects a token without the
matching action. Set `action` in the spec. On a live page,
`grecaptcha.enterprise.execute(sitekey, {action})` can produce a valid token for
free; a solver that ignores the action will not.

## Injection

A returned token is written to the response field for the widget type:

| Type | Fields |
|---|---|
| turnstile | `input[name="cf-turnstile-response"]`, `textarea[...]` |
| hcaptcha | `h-captcha-response`, `g-recaptcha-response` fallback |
| recaptcha | `g-recaptcha-response`, `h-captcha-response` fallback |

The injector also:

- dispatches `input` and `change`;
- sets `window.__autoreg_captcha_token`;
- calls `callback` or the widget's `data-callback` with the token;
- creates a hidden response input if the page has none (some forms only read
  the field, some rely solely on the callback).

Many sites read the token from the field at submit time; some only react to
the callback. If the form rejects a valid token, inspect the widget's
`data-callback`/`data-success-callback` and set `callback` explicitly.

## Solver protocols

### 2captcha

```text
POST https://2captcha.com/in.php
  key, method=turnstile|hcaptcha|userrecaptcha,
  pageurl, sitekey|googlekey, json=1, action?, data?
GET  https://2captcha.com/res.php?key=&action=get&id=&json=1
  {"status":1,"request":"TOKEN"} or CAPCHA_NOT_READY
```

### YesCaptcha

```text
POST https://api.yescaptcha.com/createTask
  {"clientKey":..., "task":{"type":"TurnstileTaskProxyless",
   "websiteURL":..., "websiteKey":..., "action":...}}
POST https://api.yescaptcha.com/getTaskResult
  {"clientKey":..., "taskId":...}
  status=processing|ready; ready -> solution.token
```

### Local solver

Default contract (Theyka-compatible):

```text
POST /createTask  {"type":"TurnstileTaskProxyless",
                   "websiteURL":..., "websiteKey":...} -> {"taskId": ...}
GET  /getTask?taskId=... -> {"status":"processing"}
                         -> {"status":"ready","solution":{"token":"..."}}
```

Configure `create_path`, `get_path` or `mode: 2captcha` if the solver speaks a
different dialect. A local solver still needs a real browser engine; run it as
a separate service.

## Manual mode

```yaml
- captcha: {provider: manual, click: true, timeout: 180}
```

With a headed browser the engine clicks the widget (or you click it), then waits
until the response field or `window.__autoreg_captcha_token` is populated. Use
this once to pass a Cloudflare managed challenge; the persistent profile keeps
the `cf_clearance` cookie for later headless runs, until it expires.

## hCaptcha images, reCAPTCHA audio, sliders

These are partially built in; the vision/audio backends are your config.

- `solve_image_captcha` screenshots the grid, sends it to a vision provider
  (`vision.providers.openai_compat` or any OpenAI-compatible endpoint), parses
  tile numbers, and clicks them with the trusted mouse. Set `prompt`,
  `tile_selector`, `max_index`. It uses `page.mouse` input, so the clicks are
  `isTrusted`.
- `solve_audio_captcha` switches reCAPTCHA to audio, downloads it, sends it to
  an STT provider (`stt.providers.openai_compat`), fills the answer and clicks
  verify. Best-effort; the challenge markup changes.
- `solve_slider` needs a hook: implement gap detection and drag in `hooks.py`.

The vision/STT endpoints are configured in `.autoreg/config.yaml`; the pack
ships no built-in model.

- **hCaptcha image grid:** the challenge lives in a shadow root. A recursive
  `shadowRoot` query is needed; tile images can be extracted from `img.src`,
  computed `backgroundImage`, or a canvas hash. Cache by image hash and click
  with 80–200ms human delays. A VLM can classify tiles if the prompt is
  perceptual (e.g. "does the object protrude past the red rectangle?"), not
  metric.
- **reCAPTCHA audio fallback:** switch to the audio challenge, download the
  audio, send it to a local STT/Whisper endpoint, fill `#audio-response`, click
  `#recaptcha-verify-button`. This is a free path when image solving fails.
- **Sliders:** detect the gap with a local vision model (ONNX) and drive a
  human-like drag; read the slider's CSS position in a loop instead of trusting
  a computed distance.
- **Cloudflare `cf_clearance` (managed challenge):** a widget token is not
  enough. Use a real browser profile, pass the challenge once headed, reuse the
  profile/cookie. If using a solver that returns `cf_clearance`, send the same
  UA and the same exit IP; the cookie is bound to both.

The source playbooks include standalone JS for hCaptcha shadow DOM, reCAPTCHA
audio/iframe, and browser slider drags. Treat them as references, not as a
guarantee: challenge markup changes.

## Limits

- A token solves the widget, not a **managed challenge**. If the page shows an
  interstitial ("Checking your browser"), a token API will not help. Use a real
  browser profile (Camoufox), a residential IP, and pass it once headed.
- Turnstile tokens are bound to the sitekey, page URL and often the visitor's
  IP. Solving on a different IP than the browser uses fails.
- Do not poll faster than the provider's interval. Default 5 seconds.
- Keep a solver balance check in `doctor`/runbook; an exhausted balance looks
  like a flow failure.
- The response field may be inside a shadow root or iframe. The injector covers
  the main document; add a hook for exotic widgets.

## Detection before solving

Recon reports protection names. At runtime:

```yaml
- action: eval
  js: "() => ({t: !!document.querySelector('.cf-turnstile'), s: document.querySelector('[data-sitekey]')?.getAttribute('data-sitekey')})"
  as: captcha_probe
```

Use the values to branch manually, or let `captcha` detect as a fallback.
