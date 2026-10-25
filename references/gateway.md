# Pool, bank and gateway

A registration is only useful if the account's credential can be consumed. This
page covers the account bank and the OpenAI/Anthropic-compatible gateway. It does
**not** cover cards, BINs, 3DS, or payment-gateway swapping.

## Account bank

The store is SQLite; export one row per account:

```bash
python "$AUTOREG" --home . accounts export accounts.jsonl
python "$AUTOREG" --home . accounts export accounts.csv
python "$AUTOREG" --home . accounts export accounts.json --secrets   # explicit
```

`accounts.jsonl` fields: `id`, `service`, `email`, `status`, `profile_id`,
`proxy`, `engine`, timestamps, `meta`, `has_password`, `has_proxy_credentials`.
With `--secrets`, `password` is included; treat that file as a credential.

A practical bank row for a farm:

```json
{"email":"a@example.com","password":"...","token":"...","expires":1790000000,
 "balance":12,"api_key":"sk-...","stage":"banked","proxy":"http://..."}
```

Keep the raw password and the session token separate. A refresh token or API key
is often more durable than a password.

## Extracting the full key

- New-API forks: `POST /api/token/` (trailing slash) with
  `{name, expired_time:-1, unlimited_quota:true, group:<first unlocked>}`,
  then `POST /api/token/{id}/key` for the full value. A masked `sk-****` from a
  list endpoint is not usable.
- Fork-specific user headers: `New-Api-User` or `X-ModelFlare-User`.
- Validate the key with a cheap endpoint (`/v1/models` or a one-token chat)
  before banking it.
- `HTTP 200` with `{"success": false}` is a failure; parse the body.

## OpenAI-compatible shim

Pattern from `pointgpt-farm` and `router-farm`:

```text
client -> /v1/chat/completions -> pool picker -> upstream account
                              -> on 401/402/429/5xx: next account
                              -> /health: pool, balances, cooldowns
```

Rules:

- Pick a live account; mark `dead` on 401/403, `cooldown` on 429/402.
- Keep a `tried` set so one request never loops on the same account.
- Pass through streaming; do not buffer the whole response.
- Reload the bank on file mtime or `/admin/reload`.
- Keep a gateway key (a dummy) for clients; the shim swaps it for a live account
  key.
- Pin a stable model mapping; unknown model names are a common 404.

## New-API / One-API / 9Router

- Channel type 1 (OpenAI) with `base_url` pointing at the shim; model mapping
  from public names to upstream names.
- Add models to `abilities` for every user group that should see them.
- `group: 'default'` is deprecated on forks; read the first unlocked group from
  `/api/user/self/groups`.
- Long generations can trip a watchdog that disables channels; exclude your
  gateway channel by name if the fork has `ch-watchdog`.
- 9Router: add connections to an existing node, remove connections whose balance
  is below a threshold during the balance refresh.
- Any2API/Sub2API: push accounts/tokens after registration; verify the push
  succeeded, not just the HTTP status.

## Referrals and OAuth

`router-farm` registers a GitHub account, then performs OAuth on each router with
the operator's `aff` code, creates a PAT and an API key, and pushes the key to
9Router. The generic pattern:

1. Persistent profile per account (GitHub session survives).
2. Email → password → username → IMAP code.
3. OAuth `state` carrying the referral; one retry with a fresh state if the
   router redirects to `/sign-in`.
4. PAT + API key; fetch the full key; push to the gateway.
5. One GitHub account can be used across several routers.

## Balance and health

- Refresh balances on a schedule; a 402/empty balance is not a dead account.
- Cooldown on 429/402; do not disable on a single 401 if the whole pool is
  failing (a provider-wide incident).
- Expose `/health` with pool size, live/dead/cooldown counts, last error, and the
  next retry time.
- Keep a `MassErrorBreaker`: N failures in a window pause the pool instead of
  deleting accounts.

## Security

- The bank contains passwords, tokens and proxy credentials. Store it outside the
  repo, encrypt at rest if it leaves the machine.
- Do not log full keys or OTPs. The pack redacts them in logs; exports with
  `--secrets` are explicit.
- Rotate/refresh tokens before expiry; keep the refresh token, not just the
  short-lived access token.
