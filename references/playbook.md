# Autoreg playbook — first try, not first guess

The goal is to make the first real run succeed by doing the thinking before the
code. Follow the order; do not skip recon and preflight.

## The process

```text
0. Preflight   → what do we have (mail/proxy/captcha/sms), what does the target want
1. Doctor      → environment green
2. Recon       → live contract: fields, endpoints, captcha, protection
3. Classify    → protocol / browser / cloudflare / two-phase
4. Engine      → curl_cffi / patchright / camoufox / playwright
5. Resources   → mail + proxy + captcha + sms
6. Adapter     → one YAML, validated
7. Single run  → headed, then headless
8. Verify      → verified / pending / invalid
9. Scale       → count up, concurrency 1, keep min_delay
10. Bank       → export, gateway, reuse
```

## 0. Preflight

Read `references/preflight.md` and answer the operator questions. Do not write
an adapter until the answers exist.

## 1. Doctor

```bash
python "$AUTOREG" --home . doctor
```

All checks must pass. Fix missing engines/dependencies first.

## 2. Recon

```bash
python "$AUTOREG" --home . recon https://target.example/signup
```

Read `report.md` and `summary.json`. You need to know:

- Is there a captcha widget, and which type?
- Is the signup a form POST, a JSON API, or a multi-step flow?
- What is the success signal (redirect, text, dashboard, API response)?
- What is the email flow (OTP code, magic link, confirm link, invite)?
- Is the target behind Cloudflare, DataDome, PerimeterX, Kasada, Akamai?

## 3. Classify the target

| Target shape | Mode | Engine |
|---|---|---|
| Plain JSON/form POST, no JS challenge | protocol | — |
| JS form, no challenge | browser | patchright (or camoufox) |
| Turnstile widget | browser | camoufox; playwright if patchright fails |
| Cloudflare managed challenge | browser | camoufox + residential IP |
| DataDome / PerimeterX / Kasada | browser | camoufox; be ready for manual/headed |
| Phone/SMS/KYC | browser + hook | stop if you have no SMS provider |
| Payment/card required | out of scope | stop |

If direct HTTP gets Cloudflare 403 but the page works in a browser, use
`browser_fetch` for the API steps.

## 4. Pick the mail mode

| Need | Provider |
|---|---|
| Disposable, fastest | `voidash`, `mailtm`, `mailgw` |
| Disposable-blocked target | `imap` alias/catch-all, `outlook_graph`, `anymessage` |
| Long-lived account | Gmail/IMAP/Outlook, not temp mail |
| Email rate limit | `mail.defer: true` + `pending` |
| Own domain | `cloudflare_temp_email` or IMAP catch-all |

Use `references/providers.md` for the current catalog and status.

## 5. Pick the proxy

- `proxy.strategy: per_account` — one exit IP per account.
- `proxy.health_check: true` — probe before mailbox/captcha.
- `proxy.required: true` if the target bans datacenter IPs.
- Use residential/sticky for anything protected; free lists only after a
  target-specific validation.

## 6. Write the adapter

Start from a template:

- `generic-email-otp.yaml` — browser + OTP
- `supabase-email-password.yaml` — Supabase/GoTrue
- `jsonrpc-browser.yaml` — browser login + in-page RPC
- `gmail-alias-imap.yaml` — Gmail alias + IMAP
- `catchall-two-phase.yaml` — signup now, confirm later

Rules:

- Selectors from recon, not guesses.
- Keep the success assertion explicit; do not accept a soft-fail text.
- Use `wait_for` instead of `sleep` where possible.
- Keep one profile per account.

## 7. Single run

```bash
python "$AUTOREG" --home . run <adapter> --count 1 --headed
python "$AUTOREG" --home . run <adapter> --count 1
```

On failure read `.autoreg/artifacts/.../failure.png|html|json` and the ledger
entry. Use `references/troubleshooting.md`.

## 8. Verify

```bash
python "$AUTOREG" --home . verify <adapter> --account <id>
```

A `verified` account is usable. A `pending` account needs the confirm phase.
An `unverified` account may still exist — check before deleting.

## 9. Scale

- Raise `--count` slowly; keep `--concurrency 1` and `limits.min_delay`.
- Watch the ledger for a mass incident (same error across accounts).
- Stop the batch on `blocked`/`target`/`config` classes.

## 10. Bank and use

```bash
python "$AUTOREG" --home . accounts export accounts.jsonl
```

Export includes profile/proxy metadata. Use `references/gateway.md` to wire the
pool into an OpenAI-compatible shim, New-API, or 9Router.

## Failure classes

The runner records `kind` in the ledger:

| Class | Meaning | Do |
|---|---|---|
| `mail` | no code, provider 5xx | retry / new mailbox |
| `proxy` | dead/407/geo mismatch | new proxy, same flow |
| `captcha` | solver error | re-solve, same proxy |
| `ratelimit` | 429 | wait, retry same step |
| `blocked` | 401/403/ban | stop the account |
| `target` | selector/flow mismatch | recon again |
| `config` | bad adapter/provider | fix config |
| `retryable` | timeout/5xx | backoff and retry |
| `mail_burned` | domain deterministically rejected | burn domain, new mailbox |

## Success is a fact

Do not call a run successful because the page said so. Check one of:

- the email arrived,
- the login returns 200,
- the full key/token exists,
- the account state is `verified`/`pending` by design.

## When to stop

- Managed challenge you cannot pass headed.
- Phone/KYC required and no provider.
- Payment required.
- Ten consecutive single-run failures with no fix.

## First-try rule

If you cannot answer every question in `references/preflight.md` and produce a
recon report, do not write the adapter. A few minutes of preflight saves a whole
batch.
