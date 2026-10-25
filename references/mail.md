# Mail providers and code extraction

## Provider overview

| `provider` | Creates a mailbox? | Auth | Best for |
|---|---|---|---|
| `mock` | yes, locally | none | Tests, mock site |
| `voidash` | yes | session key (returned) | Fast disposable inbox used by several October farms |
| `cloudflare_temp_email` | yes | admin key (optional) | Self-hosted CF Worker temp mail |
| `mailtm` | yes | auto account + bearer | Free public temp mail |
| `mailgw` | yes | auto account + bearer | Second public pool (same API as mail.tm) |
| `imap` | fixed or alias | login or XOAUTH2 | Gmail aliases, catch-all, Outlook/Yandex/custom domain |
| `outlook_graph` | no, fixed address | refresh token | Outlook mailboxes via Graph |
| `custom_http` | configurable | headers | Any temp-mail HTTP API |

`mail.provider` is required when an adapter has an `otp` step. Configure
credentials in `.autoreg/config.yaml` with `${ENV}` references, not in the
adapter.

```yaml
mail:
  default: cloudflare_temp_email
  providers:
    cloudflare_temp_email:
      base: ${TEMPMAIL_BASE}
      admin_key: ${TEMPMAIL_ADMIN_KEY}
      domain: ${TEMPMAIL_DOMAIN}
    mailtm: {}
    mailgw: {}
    voidash: {domain: voidash.bond}
    outlook_graph:
      email: ${OUTLOOK_EMAIL}
      client_id: ${OUTLOOK_CLIENT_ID}
      refresh_token: ${OUTLOOK_REFRESH_TOKEN}
    imap:
      host: ${IMAP_HOST}
      port: 993
      user: ${IMAP_USER}
      password: ${IMAP_PASSWORD}
```

## Selecting and checking

```bash
python "$AUTOREG" --home . mail test --provider imap
python "$AUTOREG" --home . mail test --provider cloudflare_temp_email --wait \
  --timeout 120 --regex '(?<!\d)(\d{6})(?!\d)'
```

`mail test` creates a mailbox (or resolves the fixed address), prints it, and
optionally waits for a matching message.

## Cloudflare temp email

The default contract is the one used by the operator's Worker:

```text
POST /api/mailboxes        Bearer admin_key      -> {"emails": ["addr"]}
GET  /api/inbox?email=...  (optional bearer)     -> {"messages": [...]}
GET  /health
```

Message fields: `id`, `from_address`/`from`, `to_address`/`to`, `subject`,
`body`/`text`/`raw_text`, `received_at` (seconds or milliseconds).

If your Worker follows a different contract, override it:

```yaml
cloudflare_temp_email:
  base: ${TEMPMAIL_BASE}
  admin_key: ${TEMPMAIL_ADMIN_KEY}
  create_path: /api/new_address
  list_path: /api/mails
  list_query: {address: "{address}"}
  messages_path: results
  address_path: address
  token_path: jwt
  auth_header: Authorization
  auth_scheme: "Bearer "
  map:
    id: id
    sender: from
    subject: subject
    body: raw
    received_at: created_at
```

`{address}` (single braces) is replaced in paths, query and bodies. `map`
supports dot paths.

## Voidash

`base` defaults to `https://api.voidash.com/api/v1`, `domain` to
`voidash.bond`.

```text
POST /inboxes {"domain": "voidash.bond"} -> {"address": "...", "session_key": "..."}
GET  /messages        Authorization: Bearer <session_key>
GET  /messages/{id}   full body when the list returns a preview
```

The provider stores the session key in the mailbox and maps both list and detail
responses. The key is also saved with the account (encrypted), so a deferred
confirm can read the same inbox from a later process.

## mail.tm

`base` defaults to `https://api.mail.tm`. Without `address`/`password` the
provider asks `/domains`, creates a random account, and logs in. It fetches the
full body of each new message once and caches it. Public rate limits apply;
2-second polling is safe.

## IMAP

```yaml
imap:
  host: imap.example.com
  port: 993
  ssl: true
  user: bot@example.com
  password: ${IMAP_PASSWORD}
  address: bot@example.com      # optional; defaults to user
  mailbox: INBOX
```

For XOAUTH2 use `access_token` instead of `password`. The provider selects the
mailbox read-only and fetches the last 40 messages per poll; the baseline-ID
filter prevents old mail from being consumed.

### Gmail +alias and catch-all

```yaml
imap:
  host: imap.gmail.com
  user: base@gmail.com
  password: ${GMAIL_APP_PW}
  alias:
    template: "{user}+{tag}@gmail.com"
    tag_prefix: reg
  search: 'TO "{address}"'
  mailboxes: [INBOX, "[Gmail]/All Mail"]

# catch-all domain forwarded to one IMAP inbox
imap_catchall:
  host: imap.example.com
  user: inbox@example.com
  password: ${IMAP_PASSWORD}
  catch_all: true
  alias_domain: mail.example.com
  alias_prefix: pk
  search: 'TO "{address}"'
  recipient_headers: [To, Delivered-To, X-Original-To, Cc]
```

`create()` returns a unique alias and `messages()` searches the server for that
recipient, then double-checks the recipient headers. Gmail dot variants are
canonicalized (same inbox); `+tags` are not. A fixed mailbox (no `alias`, no
`catch_all`) behaves as before.

`mail.create: false` in an adapter means "do not create a new mailbox; the
account email is the existing one". It is required for deferred verification.

## Outlook Graph

```yaml
outlook_graph:
  email: bot@outlook.com
  client_id: ${OUTLOOK_CLIENT_ID}
  refresh_token: ${OUTLOOK_REFRESH_TOKEN}
  tenant: common
```

The provider refreshes the access token, calls
`GET /v1/me/messages?$top=25&$select=id,from,subject,receivedDateTime,body`,
and parses the HTML body. It is the reliable route for Outlook mailboxes;
basic-auth IMAP is generally disabled.

## Custom HTTP

A declarative provider, useful when a new temp-mail API appears:

```yaml
custom_http:
  base: https://mail.example.com
  headers:
    Authorization: "Bearer ${MAIL_KEY}"
  create:
    method: POST
    path: /api/mailboxes
    json: {domain: "example.com"}
    address_path: data.address
    token_path: data.token
  list:
    method: GET
    path: /api/inbox
    query: {email: "{address}"}
    messages_path: data.messages
    map:
      id: id
      sender: from.address
      subject: subject
      body: body
      received_at: created_at
      to: to.address
```

## Code extraction

Order:

1. A code alone in an HTML tag (`<td>7F3K9Q</td>`) — strongest signal.
2. If `code_regex` is set, use it (capture group 1 when present).
3. A labeled code: `code|otp|verification|passcode|pin|token` followed by a
   4–12 character alphanumeric token that contains a digit.
4. A standalone 6-digit number.

Before matching: HTML is stripped, Unicode is NFKC-normalised, ISO timestamps
and dates are removed, a 6-digit code split as `123 456` is joined, digits from
the recipient address are ignored, and all-zero / year-like values are rejected.
There is no bare 4–8 digit fallback: it matches years and order IDs.

Alphanumeric and hex codes work through the labeled/tag paths; an 8-hex code
appears in the source cases (`5fde27d1`). A non-labeled 8-hex value is not
accepted on purpose — it collides with hashes and IDs.

For a target-specific format, set `code_regex` explicitly. A failed registration
may burn the code (some providers issue one code per request), so always read the
message produced after the trigger.

Use `sender_contains` and `subject_contains` together when a mailbox carries
marketing mail. Filters are case-insensitive substrings, not regexes.

## Reliability rules

- **New mail is identified by message ID**, never by comparing local and
  server timestamps. The runner snapshots mailbox IDs at flow start.
- **5xx is retried; 4xx fails immediately.** A 5xx means the provider cannot
  read right now; a 4xx means the request/credentials are wrong.
- **Temp-mail retention is short.** Read the mail in the same run; do not rely
  on a public inbox keeping a code for later.
- **Polling interval** defaults to 2 seconds. Lower values mostly add load, not
  speed, unless the provider is local.
- **Timeout diagnostics** list the last senders/subjects, so "no mail" and
  "filter is wrong" are distinguishable.
- **One mailbox per account.** Reusing an address across accounts links them
  and makes the ID baseline ambiguous.
- **Resend once.** After 30–60 seconds of silence, resend once, then keep
  polling. Many providers invalidate the previous code on resend.
- **Two-phase flows.** If the target rate-limits confirmation email, set
  `mail.defer: true`; the account is stored as `pending` and `autoreg verify`
  later reads the existing mailbox. See `references/two-phase.md`.

## SMS

There is no built-in SMS provider. If a target requires a phone, add a hook
that calls your SMS API and returns the code, then use it as a normal variable:

```yaml
steps:
  - action: hook
    name: request_sms
  - fill: {target: "input[name=phone_code]", value: "{{sms_code}}"}
```

Do not fake a successful phone verification; the account will fail later and
the failure will look like a flow bug.
