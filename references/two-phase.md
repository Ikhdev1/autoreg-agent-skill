# Two-phase signup and confirmation

Some targets rate-limit confirmation emails per site (often 15–20/hour) or take
5–15 minutes to deliver. A synchronous `signup → wait → click` loop wastes the
budget and stalls the batch. Split the work.

## Phase A — signup only

```yaml
name: target-signup
mode: protocol
mail:
  provider: imap
  create: false
  defer: true
  search: 'TO "{address}"'
steps:
  - request:
      method: POST
      url: "{{supabase_url}}/auth/v1/signup"
      headers:
        apikey: "{{anon_key}}"
        X-Client-Info: supabase-js-web/2.45.0
      json:
        email: "{{email}}"
        password: "{{password}}"
      expect: [200, 201]
verify: []
```

`defer: true` makes the runner skip the verify block and store the account as
`pending`. The mailbox address is the account email, so a later verify can read
it without creating a new one.

## Phase B — confirm later

```bash
# after 5–15 minutes, or on a schedule
python "$AUTOREG" --home . verify .autoreg/adapters/target-signup.yaml --all
```

`verify` recreates the mailbox from the encrypted session stored with the
account (provider config, mailbox token/meta and the set of message IDs that
were already consumed), so a temporary inbox is still readable in a later
process and a confirmation mail that arrived before verify started is not
skipped. It no longer rebuilds the mailbox from the account address alone.

`create: false` fixed mailboxes (IMAP/Outlook Graph) keep working without a
stored session, because their credentials live in the project config. An
account registered with a temporary provider that has no stored session stays
`pending` with `MailSessionError: No stored mail session for this account`
instead of silently reading a different inbox.

A restored session and a `pending` account never take a fresh baseline during
verify: the confirmation may already be sitting in the inbox, and a new
baseline would exclude it. Use an explicit `mail_arm` step when you really want
to ignore everything currently in the mailbox. Mail and session failures keep
the account at its current status instead of marking it `invalid`.

```yaml
verify:
  - otp:
      link_regex: 'https://[^\s"]*(?:verify|confirm)[^\s"]*'
      timeout: 300
  - goto: "{{otp_link}}"
  - wait_for: "text=Confirmed"
```

For protocol mode, use a `request` list instead:

```yaml
verify:
  - request:
      method: GET
      url: "{{otp_link}}"
      expect: [200, 302, 303]
```

## Supabase GoTrue notes

- Signup: `POST {supabase}/auth/v1/signup` with `apikey`, JSON
  `{email, password, data, gotrue_meta_security}`. `X-Client-Info` avoids some
  400s.
- The confirmation link contains `?token=...&type=signup`. The verify API wants
  it as `token_hash`:
  `POST /auth/v1/verify {"type":"signup","token_hash":"..."}`.
- Some flows put the session in the URL fragment (`#access_token=...`); a
  normal HTTP client does not see fragments. Use the `token_hash` flow or a
  browser.
- After verify: `PUT /auth/v1/user` with the Bearer JWT to set a password, then
  the service-specific org/API-key calls.
- `POST /auth/v1/resend` if the link expired; wait before retrying.

## IMAP search patterns

| Backend | Create address | Search |
|---|---|---|
| Gmail +alias | `user+tag@gmail.com` | `TO "user+tag@gmail.com"` |
| Catch-all | `prefix<rand>@yourdomain` | `TO "prefix<rand>@yourdomain"` and `SUBJECT` |
| Outlook Graph | fixed address | all messages; filter sender/subject |
| t-online/GMX pool | fixed `email:password` | `To` + `Delivered-To` + `X-Gmail-Original-To` |

Gmail dot variants are canonicalized: `a.b@gmail.com` and `ab@gmail.com` are the
same account. Use dots only where the target does not normalize. `+tag` aliases
survive normalization at most targets but some block `+`.

## Mailbox configuration

```yaml
mail:
  provider: imap
  create: false
  host: imap.gmail.com
  user: base@gmail.com
  password: ${GMAIL_APP_PW}
  alias:
    template: "{user}+{tag}@gmail.com"
    tag_prefix: reg
  search: 'TO "{address}"'
  mailboxes: [INBOX, "[Gmail]/All Mail"]
```

`create: false` marks the address as an existing mailbox rather than one to
create. `verify` also recognizes IMAP/Outlook Graph/custom fixed providers
automatically; the flag is a useful explicit hint and is required if you later
add a custom provider whose `create` step must be skipped.

## Limits and failure handling

- Confirmation links expire (often hours). Sweep promptly; if expired, resend and
  retry later.
- A failed signup attempt may burn the code (some providers issue one code per
  request). Read the current message, do not keep an old one.
- Do not hammer the same IMAP mailbox from several processes; Gmail rate-limits.
  One runner + one sweep is safer.
- If the confirm email never arrives, check the catch-all/forwarder before
  changing the adapter.
- A `pending` account is not a failure. It is a queue entry for phase B.
