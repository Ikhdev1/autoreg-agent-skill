# Provider catalog and selection

The point is not to try every link. Pick the cheapest provider that gives the
target what it validates: an API inbox, a real mailbox, a non-VoIP number, or a
clean exit IP.

Status notes are from the operator's October 2026 chat/repos. Treat them as
leads, not guarantees; test before a batch.

## First-pass stack

1. **Mail:** Voidash or mail.tm for a disposable inbox; Gmail `+alias`/catch-all
   when the target blocks disposable domains; IMAP/Outlook when the account must
   survive; tigrmail for QA-grade delivery (API key, no spam filter).
2. **Browser:** protocol/`curl_cffi` if no JS; Camoufox or CloakBrowser for
   fingerprint-sensitive pages; Patchright/vanilla Playwright for normal forms;
   `invisible_playwright` (`--engine invisible`) for the strongest bundled stealth.
3. **Proxy:** one sticky residential exit per account; health-check before
   creating the mailbox. Free lists only after validation against the target.
4. **Captcha:** try `empty` on New-API forks; then 2captcha/YesCaptcha/local;
   managed Cloudflare → real browser + one headed pass.
5. **SMS:** only if recon shows a phone field. Prefer a paid API with the right
   country/operator; free public numbers are for low-risk targets.

## Mail

### API-capable (first choice)

| Provider | Transport | Notes |
|---|---|---|
| `voidash` | REST | `POST /inboxes` → `address` + `session_key`; used by secondno/wispbyte/abliteration/remalt. Built in |
| `mail.tm` | REST | No key; `POST /accounts` → token; rate-limited (~1/min/IP); domain pool small |
| `mail.gw` | REST | Same API as mail.tm; built in as `mailgw` |
| `tigrmail` | REST | API key from console; `POST /inboxes`, `GET /inboxes/{id}/messages`; QA-grade, no spam filter; use `custom_http` |
| `nonecap` | REST | hCaptcha solver API; `pip install nonecap`; 100 free solves/day on the extension, paid beyond |
| `invisible_playwright_mcp` | MCP | Same stealth Firefox engine; `browser_open/navigate/snapshot/click/type/...` tools; if the agent prefers MCP, use this instead of YAML adapters |
| `cloudflare_temp_email` | REST | Self-hosted Worker; best retention/control; built in |
| `moemail` | REST/self-host | CF-based; configure via `custom_http` or a provider |
| `24email` | WebSocket | ~200 domains; message body via `/media/`; API is WS, use `custom_http`/browser |
| `emailnator` | HTTP + XSRF | Real `@gmail.com` dot aliases; 5k+ pool; flow: GET `/`, cookies, POST `/generate-email` `{"email":["dotGmail"]}`, POST `/message-list`; can be Cloudflare-sensitive |
| `smailpro` | HTTP/paid | Real `@gmail.com` mailboxes; passes disposable checks; paid |
| `anymessage` | REST/paid | Gmail/Outlook pools; cancel order on failure for refund |
| `mailsac` | REST/key | Good API; free tier limited; use `custom_http` |
| `mailinator` | REST/key | Public inboxes are widely blocked; paid domain/API |
| `dropmail.me` | GraphQL | Live inbox; parse via `custom_http`/browser |
| `1secmail` | REST | Historically API, but reported **403/dead** in Oct 2026; avoid as primary |

### Aliases and real mailboxes

| Method | Notes |
|---|---|
| Gmail `username+tag@gmail.com` | All mail lands in one inbox; IMAP App Password; Google anti-fraud ~20–30 regs/hour, rotate IP. Some sites reject `+` |
| Gmail dot trick | `a.b@gmail.com` = `ab@gmail.com`; canonicalized by Gmail, so it does not create a new identity at the provider. Use only where the target does not normalize |
| Own domain + Cloudflare Email Routing | `*@yourdomain` → one inbox; free; needs a domain and MX/Email Routing setup |
| SimpleLogin / addy.io | Alias forwarding; addy.io has a generous free tier; self-host possible |
| ImprovMX / 33mail / spamgourmet / erine.email | Forwarding/alias services; useful when disposable domains are blocked |
| IMAP providers (mail.ru, rambler.ru, yandex.ru, aol.com) | No SMS at signup (captcha instead); IMAP works; Yandex anti-spam is aggressive |
| gmx.com | IMAP, but signup may require SMS |

Our `imap` provider supports Gmail `+alias`, catch-all and recipient-header
search. `create: false` marks an existing mailbox for deferred verify.

### Web-only inboxes

These are useful as a fallback through a real browser, not as a first API:

`yopmail.com` (8d TTL), `temp-mail.org`, `temp-mail.io`, `dropmail.me`,
`emailfake.com`, `emailnator.com`, `mohmal.com`, `tempail.com`, `getnada.com`,
`inboxkitten.com`, `mailinator.com`, `burnermail.io`, `fakemail.net`,
`tempmail.plus`, `mail.td`, `mailpoof.com`, `trash-mail.com`, `temp-mails.com`,
`emaildrop.io`, `temporarymail.com`, `generator.email`, `mailsac.com`,
`tempr.email`, `atomicmail.io`, `emailondeck.com`, `crazymailing.com`,
`tempmail100.com`, `tempmail.ninja`, `boomlify.com`, `tempmail.dev`,
`internxt.com/temporary-email`, AdGuard Temp Mail, `tempmailo.com`,
`incognitomail.com`, `tempmail365.com`, `mail7.io`, `spamgourmet.com`,
`byom.de/trashmails`, `altmails.com`, `mailnesia.com`, `dispostable.com`,
`one-off.email`, `getnada.com`, `33mail.com`, `erine.email`.

If a target rejects a disposable domain deterministically, switch to
Gmail/catch-all; do not burn attempts on more disposable names.

## SMS

Full catalog and API notes: `references/sms.md`.

| Tier | Services | Use |
|---|---|---|
| Paid/API | HeroSMS, 5SIM, SMSPool, PVACodes, PVAPins, NumberOTP, SMS-Man, Tiger SMS, OnlineSIM | Automation; choose country/operator; cancel on failure |
| Non-VoIP US | Textverified, SMSPinVerify, FoxSIMs, Buru | Targets that filter VoIP |
| Rental | Groovl, SMSSir, Virtty, ASMS, SMS7, Freeje | Multiple codes / long-lived numbers |
| Free public | 2nd-no, sms24.me, quackr, receive-sms-free, temp-number, goinsms, sms-online, 7sim, and the rest of the 21-service list in `sms.md` | Low-risk targets; numbers are public, codes may be stale |
| Telegram bots | @ReceiveSMSRobot, @KupitNomerBot, @receive_robot, @timeofsms_bot | Manual fallback |

Rules: one number per account, never reuse; pass the target's country/operator;
if the target rejects VoIP, use a non-VoIP service; cancel/refund on every
failure branch.

## Proxy

### Sources

- Free lists: ProxyScrape, proxifly, TheSpeedX, monosans, Geonode, IPWay trials,
  Webshare free, Oracle Free Tier, WARP→SOCKS5. Validate before use.
- Residential: IPWay, ProxyShard, Decodo, Nodemaven, ProxyLane, RapidProxy,
  Bright Data. Prefer sticky sessions per account.
- Datacenter proxies fail by IP reputation, not by browser patch. For
  fingerprint-sensitive targets, use residential.

### Tooling

| Tool | Role |
|---|---|
| Proxy Workbench | Validate a list against your actual target, rank, export TXT/CSV/JSON/Clash/sing-box |
| mubeng | HTTP pool with live rotation |
| GoProxy | HTTP/HTTPS/SOCKS5/SS server, balancing |
| Hysteria | Censorship-resistant SOCKS5/HTTP tunnel |
| SpoofDPI / graftcp / vopono | DPI bypass / per-process proxy / VPN namespace |
| headscale / Firezone | Self-hosted mesh/zero-trust VPN |
| MyIP | IP/leak/blacklist diagnostics |

Our engine has `proxy check`, `proxy.pool`, `per_account`, `health_check`,
`required`, and `align_geo`. Free lists are acceptable only after a target-
specific check; do not put unvalidated proxies into a batch.

## Browser engines (Oct 2026 benchmark)

From the operator's 7-tool × 31 Cloudflare-target run:

| Engine | Result | Notes |
|---|---|---|
| invisible_playwright | 5/5 detection suites | Firefox fork, C++ fingerprint engine, humanized input, persistent profile. First-class in this pack |
| nodriver | 28 OK / 0 blocked | System Chrome, direct CDP; async API; detected but not integrated (persistent-context flow is Playwright-shaped) |
| CloakBrowser | 26 OK | Patched Chromium; 562 MB binary download; optional |
| curl_cffi | 26 OK | No browser; TLS/JA3 impersonation |
| Patchright | 25 OK | Playwright drop-in; can fail Turnstile |
| Camoufox | 25 OK | Gecko fingerprint; our stealth default |
| vanilla Playwright | 24 OK | Useful when Patchright breaks a widget |

Practical: use `curl_cffi` for no-JS, Camoufox for fingerprint-sensitive,
Patchright/Playwright for normal forms; install CloakBrowser/nodriver only if the
target resists the built-ins. See `references/fingerprints.md`.

## Status notes

- `pointgpt-farm`: chat update 22.10.2026 says the autoreg stopped working
  ("авторег не ворк уже"). Treat the repo as a pattern source, not a live tool.
- `1secmail`: reported 403/dead in October 2026.
- `tempcoda`: dead.
- Gmail `+alias`: ~20–30 signups/hour per Google anti-fraud; rotate IP.
- Disposable domains burn fast. A catch-all domain or a real mailbox is more
  durable than chasing new temp-mail names.
- Free public SMS numbers are public; codes may be consumed by someone else.
  Never use them for Telegram/OpenAI (the pools are banned).
