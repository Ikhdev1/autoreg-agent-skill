# Case gallery — October 2026 auto-reg examples

These are distilled patterns from the examples the operator supplied (local
archives + GitHub). They are **not** adapters to copy verbatim: endpoints and
selectors rot. Copy the shape, re-run recon, and keep the target's current
contract.

## Quick map

| Case | Stack | Core lesson for the universal pack |
|---|---|---|
| pointgpt-farm | stdlib HTTP, mail.tm, Gmail +aliases, Telegram login | Read OTP by **IMAP `TO` search**, not a general inbox scan; rotate exit IP after the target's per-IP limit |
| secondno-autoreg | Playwright + in-page JSON-RPC, Turnstile | When direct HTTP gets Cloudflare 403, call the API **inside the logged-in page** (`browser_fetch`) |
| peak-fo-autoreg | Supabase GoTrue, hCaptcha vision, catch-all IMAP | Two-phase signup/confirm; server-side `SUBJECT` search; confirm sweep after the run |
| replicas-autoreg | Supabase GoTrue, IMAP pool | `token` in the link is `token_hash` at the API; signup and confirm are separate phases under email rate limits |
| wispbyte-parser-fleet | HTTP + local Turnstile solver | Rate limit is per **exit IP**, not per proxy host:port; dedupe proxies by exit IP |
| gname-eucc-toolkit | Playwright + slider + IMAP | Gmail dot variants are canonicalized by the provider; match `To`/`Delivered-To`/`X-Gmail-Original-To` |
| 1min_autoreg | Playwright + Gmail +alias + IMAP | Simple alias flow is enough when the target only needs a real mailbox |
| tokenharbor_autoreg | CDP to an existing Chrome, IMAP | `connect_over_cdp` avoids launching a detectable browser; use the user's real session |
| Inkers_NFT_AutoReg | aiohttp, wallets, proxies | Pure HTTP + proxy pool + semaphore is enough for a simple application API |
| router-farm | TypeScript, Drizzle, IMAP, New API/9Router | Persistent profile per account; OAuth with referral; push full keys to a gateway; rate-limit backoff |
| devin-autoreg | Chrome MV3 extension + `chrome.debugger` | `Input.dispatch*` produces `isTrusted` events; humanized typing; Gmail helper |
| autoreg-builder | Python playbook + modules | Recon/preflight first; empty Turnstile first; one egress IP; mail/solver/proxy abstractions |
| autoreg-v7 | Python framework + docs | Error taxonomy, pool/gateway, "success is a fact", no card/BIN modules |
| a6api-autoreg | Go + tls-client, Turnstile | Chrome TLS fingerprint can pass some CF gates without a browser; solver racing |
| VPN autoreg GUI | Playwright + temp-mail rotation | Rotate mail.tm → mail.gw → another provider; GUI is optional |
| v0_autoreg | CDP to existing Chrome + mail.tm tab | Sometimes the simplest reliable path is the user's own browser |

## Late-October additions (chat export)

| Case | Stack | Lesson |
|---|---|---|
| pointgpt.ai | mail.tm / Telegram login, bank + shim | **Status: autoreg stopped working by 22.10.2026** per the chat update; keep as a pattern, not a live tool |
| remalt.com | Next.js + better-auth + Supabase | Pure API signup, no captcha; Voidash magic link; 500 trial coins; ~10s/account |
| abliteration.ai | Cloudflare Turnstile (`action=password_signup`), Camoufox, Voidash | Native WorkOS headers; manual `fetch()` is banned; key `ak_...` via REST with `Idempotency-Key`; review delay means the gateway must retry later |
| octopusx.ai | mail.tm (`uberip.com`), no captcha | $6/account, key per account, stdlib gateway with cooldowns (503/429/quota/401) |
| linkforge | PVACodes + Jio SMS | One-time links live 30–60 min; paid number per link; activate immediately |
| secondno | browser JSON-RPC + Turnstile | Reuse an active number before reserving; direct HTTP is 403 |
| gname | Playwright + slider + IMAP | Dot variants are canonicalized; match `To`/`Delivered-To`/`X-Gmail-Original-To` |

## Pattern 1 — protocol first, browser when JS is the gate

Order of preference:

1. JSON/form POST with browser-like TLS (`curl_cffi`) — fastest.
2. Browser page, but API calls via `browser_fetch` (cookies + origin + sessionStorage).
3. Full browser UI flow.

`secondno` is the canonical case: direct HTTP to `2no.pl` returns Cloudflare 403,
but `page.evaluate(fetch)` from the logged-in page works. Our `browser_fetch`
action exists for exactly this. A JSON-RPC body is just `{id, query}`; store the
response under a variable and `extract` with a dot path.

React hydration is a real failure mode: filling immediately after
`domcontentloaded` hits un-hydrated inputs. `wait_for input[type=password]`,
then wait 1–2 seconds, then use a native-setter fill (the engine's `fill` uses
Playwright's setter; if the site ignores it, add a hook).

## Pattern 2 — OTP mail at scale

- **Gmail +alias / dot alias:** one inbox can serve ~10 signups. Gmail
  canonicalizes dots and `+tags`; targets that normalize addresses will see the
  same account. Use this only where the target does not.
- **Read by recipient, not by latest message.** Gmail IMAP search
  `TO "base+tag@gmail.com"` is the reliable path; a full inbox scan gets flooded
  and misses the window.
- **Catch-all domain:** generate `prefix<random>@yourdomain`, forward all mail to
  one IMAP mailbox, and search by `To`/`Delivered-To`/`X-Original-To`/`Cc` plus
  `SUBJECT`. This is the most durable pattern for batches.
- **Mailbox pool:** `email:password` lines; validate IMAP login before spending a
  proxy/captcha; mark dead boxes.
- **Domain burn:** if the target deterministically says "domain not allowed" /
  "disposable", burn that domain for a TTL. Do not burn on "mail did not arrive"
  alone.
- **Resend:** one resend after 30–60 seconds of silence, then keep polling. Many
  providers invalidate the previous code on resend.

Our `imap` provider supports `alias.template`, `catch_all`, `search`, and
`mailboxes`; `mail.defer` + `pending` covers the two-phase cases.

## Pattern 3 — two-phase signup/confirm

Cases: `replicas`, `peak`, and any Supabase/Clerk/GoTrue target.

- Email providers rate-limit sends per site (often 15–20/hour). A single
  synchronous loop burns the budget and stalls.
- Phase A: signup wave, store `email/password/status=signed_up`.
- Phase B (5–15 minutes later): search the mailbox for the confirm link, exchange
  it for a session, then initialize the org / create the API key.
- GoTrue detail: the link query parameter is `token`; the verify API expects
  `token_hash`. Some flows return the session directly in the URL fragment.
- Confirm links expire (often a few hours); sweep promptly. If the link is dead,
  `/auth/v1/resend` and retry later.

Our flow: `mail.defer: true` stores `pending`; `autoreg verify` re-opens the
profile and runs the adapter's `verify` steps, with the mailbox available for
fixed/IMAP providers. See `references/two-phase.md`.

## Pattern 4 — captcha and one egress IP

- Empty Turnstile (`turnstile=""`) is worth trying before paying a solver; some
  New-API forks accept it.
- A solver token must be solved from the **same exit IP** as the signup. Pass the
  account proxy into the solver task (our 2captcha/YesCaptcha/local adapters now
  do).
- Patchright is excellent for normal Chromium, but Cloudflare Turnstile can fail
  because of its CDP patches; vanilla Playwright or nodriver are the alternatives
  in the source playbooks. Camoufox is the cleanest Gecko option here.
- Enterprise reCAPTCHA with an `action` binding is often solvable for free by
  executing `grecaptcha.enterprise.execute(sitekey, {action})` in a real page;
  a solver token without the action is rejected.
- hCaptcha image tasks are a separate problem (shadow DOM, grid geometry,
  vision). `peak` solves them with a local VLM; our pack exposes a hook, not a
  built-in vision solver.

## Pattern 5 — proxy and rate limits

- Limits are per **exit IP**. Rotating ports on the same host does not help
  (`wispbyte`: 5–8 attempts/hour per exit IP).
- Keep one egress IP for the chain captcha → signup → login → token exchange.
  Rotating inside the chain is a flag.
- Warmup/health-check can use a rotating proxy; the chain cannot.
- Health-check before creating a mailbox or paying for a captcha.
- On 429 honour `Retry-After`; on 401/403 stop the account; on 5xx retry.
- On a mass incident (many accounts failing the same way) pause the pool instead
  of disabling accounts one by one.

## Pattern 6 — API keys, pools, gateways

- A masked key is not a key. Fetch the full value (`POST /api/token/{id}/key` on
  New-API forks; the key endpoint differs per fork).
- New-API forks: `POST /api/token/` needs the trailing slash; `expired_time` is
  an int; `group` must be an unlocked group, not `default`; the user header is
  fork-specific (`New-Api-User` vs `X-ModelFlare-User`).
- Bank rows (`accounts.jsonl`) keep email, password, token, expiry, balance, and
  the gateway key. Our `accounts export --format jsonl` gives a compatible
  starting point.
- Gateways: OpenAI-compatible shim in front of the pool, rotate on
  401/402/429/5xx, cool down dead accounts, expose `/health`; wire the channel
  into New-API/9Router/Any2API.
- Validate success by a fact: mail arrived, login 200, full key retrieved. A
  `200 {"success": false}` is a failure.

## Seven rules (adapted from the source playbooks)

1. **Facts over guesses.** Recon the live contract; success is a fact, not UI text.
2. **Ask for missing resources before code.** Proxy, mailbox, solver key, invite,
   SMS — do not invent them.
3. **Classify the error before reacting.** `ratelimit` waits, `proxy` rotates,
   `mail` retries, `captcha` re-solves, `blocked/target/config` stop.
4. **Do not leak money.** Cancel paid mail/SMS on every failure branch; never
   retry a paid resource blindly.
5. **One identity per chain.** One exit IP, one profile, one coherent
   locale/timezone/Accept-Language.
6. **Degrade predictably.** Empty pool → explicit error with retry time; a mass
   incident pauses the pool, not every account.
7. **Nothing irreversible.** Keep artifacts, use dry-run, never delete a profile
   or key without an explicit command.

## Error taxonomy

| Class | Examples | Action |
|---|---|---|
| `config` | bad adapter, unknown variable, missing provider | stop, fix config |
| `preflight` | no proxy/mail/solver/invite | stop, ask operator |
| `mail` | timeout, provider 5xx, mailbox dead | retry / new mailbox |
| `mail_burned` | domain deterministically rejected | burn domain, new mailbox |
| `proxy` | dead, 407, geo mismatch | new proxy, same flow |
| `captcha` | solver error, token rejected | re-solve, same proxy |
| `sms` | no number, code timeout | new number / stop |
| `ratelimit` | 429, `Retry-After` | wait, then retry same step |
| `blocked` | 401/403, account locked | stop account |
| `target` | selector/flow mismatch, soft-fail | recon again |
| `retryable` | timeout, connection reset, 5xx | retry with backoff |
| `unknown` | anything else | one retry, keep artifacts |

The runner now records `kind` in the ledger and stops on
`config/blocked/target/mail_burned`.

## What was deliberately not merged

- Card/BIN generation, 3DS fabrication, and gateway card-swapping from the
  source archive. Payment steps are out of scope; use an authorized payment
  method if the target requires one.
- Service-specific hardcoded endpoints and secrets. They belong in an adapter,
  not in the universal pack.
- Exact anti-bot bypass code for a specific vendor that would be stale by the
  next run. The pack keeps the reusable mechanism and documents the decision.
