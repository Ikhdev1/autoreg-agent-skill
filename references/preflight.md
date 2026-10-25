# Preflight checklist

Run this before recon and before writing an adapter. Missing resources are the
usual reason a first attempt fails.

## Operator questions

Ask these once, in one message, when the answer is not already obvious:

1. **Target URL and goal.** Which service, and what do you want from the account
   (trial, credit, API key, points, an account to reuse later)?
2. **Mail.** Temp inbox, own domain/catch-all, Gmail/Outlook/IMAP pool, or a
   paid provider (Voidash/Anymessage/SmailPro)?
3. **Proxy.** Static list, sticky residential template, rotating, or none?
   Use none only for tests.
4. **Captcha.** Do you have a solver key (2captcha/YesCaptcha), or should we try
   `empty`/a local solver first?
5. **SMS.** Only if the form asks for a phone. Which provider and country?
6. **Invite/referral.** Code, pool, or link if the target requires one.
7. **Limits.** Known per-IP/per-account limits, and the acceptable batch size.

If the target requires payment, stop. This pack does not generate cards or
bypass payment.

## Environment checks

```bash
python "$AUTOREG" --home . doctor
python "$AUTOREG" --home . adapters validate --all
```

`doctor` must be green: engines available, store writable, providers configured.
If `engine:camoufox`/`engine:patchright` fails, fix it before writing an adapter.

## Config checklist

```yaml
mail:
  default: voidash        # or cloudflare_temp_email / imap / ...
  providers: {...}
captcha:
  default: empty          # try empty first; use 2captcha/YesCaptcha if needed
  providers: {...}
proxy:
  default: direct
  strategy: per_account
  health_check: true
  required: false
limits:
  concurrency: 1
  min_delay: 2.0
```

Decide the mail mode before the first run:

- **Disposable:** Voidash/mail.tm/mail.gw (fast, not durable).
- **Durable:** Gmail `+alias`, catch-all domain, IMAP pool, Outlook Graph.
- **Two-phase:** `mail.defer: true` + `pending` if the site rate-limits mail.

## Readiness criteria

Do not start a batch until all of these are true:

- `doctor` is green.
- A recon report exists for the signup URL.
- One adapter is validated.
- One account runs headed successfully, then one runs headless.
- `verify` marks the account `verified` (or `pending` by design).
- `fpcheck` returns no mismatches for the chosen engine.
- Proxy health-check passes for the first picked proxy.
- The ledger and artifacts directory are being written.

## Stop conditions

- The target asks for payment, KYC, or a document.
- The target needs a phone and you have no SMS provider.
- The target shows a managed Cloudflare challenge and you have no clean
  residential IP or no way to pass it headed once.
- Ten consecutive single runs fail without a fix; re-run recon.
