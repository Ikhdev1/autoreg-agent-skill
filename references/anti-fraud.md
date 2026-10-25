# Anti-fraud rules for registration runs

The target's risk engine sees the whole chain, not one page. These rules come
from the October 2026 examples and are the difference between a working flow and
a wave of burned accounts.

## 1. One egress IP per chain

Use one exit IP for: captcha solve → signup → email verification → login →
token/API-key creation. A solver token is bound to the resolver IP; a login from
a new IP is a flag.

- `proxy.strategy: per_account` (default) gives one sticky proxy per account.
- Pass the same proxy into the captcha task — 2captcha/YesCaptcha/local adapters
  receive it automatically from the runner.
- Rotating proxies are for warmup, health checks, and mail polling only.
- `proxy.required: true` makes the run fail closed when no proxy is available.

## 2. Proxy modes

| Mode | Use for | Our config |
|---|---|---|
| static list | stable per-account egress | `proxy.pool` + `per_account` |
| sticky session | residential gateway with session id | a `{sid}`/`{ttl}` template, one pick per account |
| rotating | warmup / mail / health only | `per_attempt` or external rotation |

If the provider exposes `host:port:user:pass`, the pack parses it. `#CC` country
tags and `[refresh_url]` comments are not part of the URL; strip them before
adding to the pool.

## 3. Health-check before spending resources

Create the proxy, probe its exit (`proxy check`), then create the mailbox and
solve the captcha. A dead proxy after a paid solve is money burned.

- `proxy.health_check: true` (default) probes every picked proxy in the runner.
- `identity.align_geo: true` applies the exit country/timezone/locale to the
  profile.
- If the probe fails, the account fails before any mailbox/captcha cost.

## 4. Pacing and humanization

- `limits.min_delay` (default 2s) between account starts.
- `limits.concurrency: 1` until ten single runs are clean.
- Browser `fill` with `human: true` types at 40–140ms/char; `click` supports a
  delay. Jitter all sleeps.
- Stagger worker starts; do not launch N browsers at once.
- Headless is detectable on some targets. Prefer headed or an offscreen window
  when the target shows a challenge; Camoufox `humanize` helps.

## 5. Headers and geo consistency

- Protocol mode sets `Accept-Language` from the profile locale.
- Keep `Origin`/`Referer` on the target's own origin; do not send a bare
  `python-requests` UA. `curl_cffi impersonate="chrome"` fixes TLS/JA3.
- Country of the proxy ↔ profile timezone ↔ locale ↔ `Accept-Language` must
  agree. A German IP with `Asia/Tokyo` is a stronger signal than canvas noise.

## 6. Warmup

Before a sensitive action (create API key, claim credit), do a couple of
ordinary GETs (`/me`, `/plans`) and pause. "Bot goes straight for the key" is a
common heuristic.

## 7. Error classes and reactions

| Class | Reaction |
|---|---|
| `ratelimit` / 429 | honour `Retry-After`, retry the same step |
| `proxy` | switch proxy; keep the same flow |
| `mail` | retry, new mailbox, or wait for delivery |
| `captcha` | new token, same proxy |
| `blocked` / 401/403 | stop the account; do not burn retries |
| `target` | re-run recon; selector/flow changed |
| `config` | fix adapter/config |
| `retryable` / 5xx | backoff and retry |

The runner records `kind` in `.autoreg/runs/ledger.jsonl` and stops on permanent
classes.

## 8. Domain and identity burn

- Burn a mail domain only on a deterministic signal ("domain not allowed",
  "disposable", "not eligible"). "Mail did not arrive" is not proof.
- One profile per account; never reuse a profile across accounts.
- Do not reuse a mailbox for two consecutive registrations; round-robin.
- If the target limits N accounts per IP, run batches of N on one egress IP, then
  switch the whole egress. Do not rotate inside a batch.

## 9. Soft failures

A UI message such as "registration failed" can be false: the account may exist
and the mail may arrive. Verify by a fact (mail landed, login succeeds, token
returned), not by the page text. Conversely, HTTP 200 with
`{"success": false}` is a failure.

## 10. Mass incidents

If many accounts fail the same way in a short window, pause the pool. Disabling
accounts one by one after a provider-wide 401/403 turns a transient incident into
permanent loss.

## 11. What the pack does not do

- Card/BIN generation, 3DS fabrication, payment-gateway card swapping.
- SMS provider integrations (use a hook; numbers are a separate resource).
- Guaranteed bypass of a managed challenge. Camoufox + a clean residential IP +
  one headed pass is the practical path.
