# autoreg — agent skill v1.7.0

На коленке собранный, но проверенный пак для агента: разобрать регистрацию на
новом сервисе, собрать YAML-адаптер, пройти форму через браузер или HTTP,
получить OTP из почты и сохранить проверенный аккаунт в шифрованной базе.
Это skill с CLI-движком, а не набор готовых аккаунтов или ключей.

**Автор и соцсети:** [@ikhdev](https://t.me/ikhdev).

## Что внутри

- Camoufox, Patchright, Playwright и опциональный `invisible_playwright`;
  `curl_cffi` для protocol-режима. `auto` обычно выбирает Patchright,
  `stealth: true` — Camoufox; `invisible` включается явно.
- Почта: Voidash, mail.tm/mail.gw, IMAP, Outlook Graph, Gmail-алиасы,
  catch-all, Cloudflare Worker и настраиваемый HTTP-провайдер.
- OTP, интеграции 2captcha/YesCaptcha/local/manual, vision/STT-пайплайны,
  клики по скриншоту и CDP-мышь. Ключи и модели подключаются отдельно.
- Один профиль, ящик и прокси на аккаунт; recon-отчёт и генерация адаптера.
- SQLite + Fernet: пароли, прокси и доступ к почте; экспорт JSON/CSV/JSONL.
- Отложенная проверка `pending → verify`: доступ к временному ящику и
  использованные ID писем переживают перезапуск процесса.
- 8 адаптеров-шаблонов и справочники для агента.

Новый сервис требует своего адаптера и доступных провайдеров. Встроенные e2e
проверяют локальный mock-сайт; гарантии регистрации любого сайта с первого
запуска нет. SMS пока подключается только через hook.

## Изменения версий

v1.1 added the October 2026 case patterns: in-page `browser_fetch`, IMAP
aliases/catch-all, two-phase `pending` verification, solver proxy binding,
error classes, and the case/anti-fraud/gateway references.

v1.2 adds the provider layer: the `voidash` mailbox, `mailgw`, and the
`providers.md` / `sms.md` catalogs (temp mail, SMS, proxies, engine benchmark),
so an agent can choose a working provider before writing an adapter.

v1.3 adds the method layer: `references/playbook.md` (first-try process),
`references/preflight.md` (operator questions and readiness checks), and recon
skeletons now detect the send-code control and put OTP before submit.

v1.4 adds the input layer: trusted CDP mouse (`mouse_move`/`mouse_click`/
`mouse_drag`/`press_hold`), React-friendly `fill_native`, OTP boxes via
`fill_code`, `solve_image_captcha` (grid → vision → click), and
`solve_audio_captcha` (reCAPTCHA audio → STT). It also fixes Camoufox detection
when the installed package and cached browser version differ.

v1.5 adds `click_by_vision`: screenshot an element or page, ask the VLM for a
point, and click it with the trusted CDP mouse.

v1.6 adds `invisible_playwright` as an optional first-class engine
(`--engine invisible`), with persistent profiles and humanized input.
`zendriver`/`nodriver` are not integrated engines.

v1.7 encrypts mailbox sessions with each account, restores them in a later
`verify` process, keeps message-ID baselines stable and renders protocol
verification steps lazily so `{{otp}}` is available after its OTP step.

The pack is self-contained under `~/.agents/skills/autoreg/`. Project state is
kept in `<project>/.autoreg/`.

## Installation

```bash
git clone https://github.com/Ikhdev1/autoreg.git "$HOME/.agents/skills/autoreg"
python -m pip install -r "$HOME/.agents/skills/autoreg/requirements.txt"
python -m patchright install chromium
```

`SKILL.md` is the entry point for an agent that supports local skills. For
other clients, add this directory to their skill search path or load
`SKILL.md` directly. Start a new agent session after installing the skill.

Optional engines are installed separately; to use `--engine invisible`, run
`python -m pip install invisible-playwright`. Store provider keys in a local
`.env` or environment variables, never in an adapter committed to Git.

## Quick start

```bash
AUTOREG="$HOME/.agents/skills/autoreg/scripts/autoreg.py"

python "$AUTOREG" --home . doctor
python "$AUTOREG" --home . init
python "$AUTOREG" --home . recon https://target.example/signup
# edit .autoreg/adapters/target.yaml using the generated report
python "$AUTOREG" --home . run .autoreg/adapters/target.yaml --count 1 --headed
python "$AUTOREG" --home . verify .autoreg/adapters/target.yaml --account 1
```

Windows PowerShell:

```powershell
$AUTOREG = "$env:USERPROFILE\.agents\skills\autoreg\scripts\autoreg.py"
python $AUTOREG --home . doctor
```

`--json` can appear before or after the subcommand.

## Commands

| Command | Purpose |
|---|---|
| `init` | Create `.autoreg/`, config, key, adapter templates |
| `doctor` | Check engines, deps, store, permissions, config; `--probe` checks the direct IP |
| `recon URL` | Capture a signup page, network, forms, protection; emit report + skeleton |
| `run ADAPTER` | Execute 1..N accounts; `--dry-run`, `--headed`, `--engine`, `--proxy`, `--var`, `--email`, `--password`, `--no-verify`, `--stop-on-error` |
| `verify ADAPTER` | Re-check one account or `--all`; updates `verified`/`invalid` |
| `accounts list/show/export/delete` | Inspect or export the encrypted store |
| `profile new/list/show/delete/fpcheck` | Manage identities |
| `adapters validate PATH\|--all` | Schema check without network |
| `mail test --provider X [--wait]` | Create a mailbox and optionally wait for a code |
| `proxy check URL` | Exit IP, country, timezone, ASN |
| `selftest` | Full mock-site regression (signup, OTP, captcha, verify, fpcheck) |

## Layout

```text
autoreg/
├── SKILL.md                     agent-facing workflow
├── references/                  adapters, recon, fingerprints, mail, captcha,
│                                providers, sms, cases, anti-fraud, two-phase,
│                                gateway, troubleshooting
├── assets/adapters/             mock, generic email/password/OTP, protocol OTP,
│                                Supabase, JSON-RPC, Gmail alias, catch-all two-phase
├── scripts/
│   ├── autoreg.py               CLI launcher
│   ├── mock_site.py             local signup target
│   └── autoreg_pkg/
│       ├── cli.py               argparse commands
│       ├── runner.py            per-account orchestration, retries, proxy/mail/profile
│       ├── flow.py              browser step engine + verify
│       ├── protocol.py          curl_cffi step engine + verify
│       ├── browser.py           Camoufox / Patchright / Playwright persistent sessions
│       ├── identity.py          deterministic profiles, Camoufox fingerprints, fpcheck
│       ├── mouse.py             trusted CDP mouse helpers
│       ├── vision.py            OpenAI-compatible vision/STT clients
│       ├── mail.py              providers + code extraction
│       ├── captcha.py           2captcha / YesCaptcha / local / manual + injection
│       ├── recon.py             HAR, forms, protection detection, skeleton
│       ├── store.py             SQLite + Fernet, ledger
│       ├── verify.py            stored-account verification
│       └── selftest.py          end-to-end harness
└── tests/                       unit + e2e
```

## Design decisions

- **One account = one profile + one mailbox + one proxy lease.** Reuse links
  accounts and destroys the value of the identity.
- **Recon before code.** Selectors and endpoints come from the live page, not
  from guesses. The skeleton is a draft with TODOs.
- **In-page fetch.** `browser_fetch` runs `fetch()` in the logged-in page for
  JSON-RPC/APIs that return Cloudflare 403 to direct HTTP.
- **One egress IP per chain.** A solver token is bound to the resolver IP; the
  runner passes the account proxy into 2captcha/YesCaptcha/local tasks.
- **IMAP aliases and catch-all.** `imap` can generate Gmail `+tags` or unique
  catch-all addresses and search by recipient (`TO`, `Delivered-To`, ...).
- **Two-phase.** `mail.defer: true` stores `pending`; `verify` later reads the
  existing mailbox and runs the confirm steps.
- **Error classes.** The ledger records `mail/proxy/captcha/ratelimit/blocked/
  target/config`; permanent classes stop instead of burning retries.
- **Browser is the default; protocol is an optimization.** Protocol mode is
  fast but cannot execute JS or pass a managed challenge.
- **Camoufox for fingerprint-sensitive targets; Patchright for normal Chromium;
  Playwright for init-script overrides.** Patchright ignores
  `add_init_script` by design (verified), so its profile describes native
  hardware values instead of lying.
- **New mail is identified by message ID**, never by timestamp comparison.
  Mail 5xx is retried; 4xx fails fast.
- **Passwords, proxy credentials and mailbox sessions are Fernet-encrypted**
  in SQLite. Public account output and exports exclude mailbox credentials.
- **Progress goes to stderr**, so `--json` stdout stays machine-readable.
- **Retries reuse the profile, mailbox and proxy**; a late failure often means
  the account already exists.

## Tests

```bash
cd ~/.agents/skills/autoreg
python -m unittest discover -s tests -p "test_core.py" -v
python -m unittest discover -s tests -p "test_e2e.py" -v
```

The e2e test starts `mock_site.py`, runs a real browser signup, verifies the
account, checks the fingerprint, runs recon, exercises `browser_fetch`, tests
deferred `pending` verification, runs protocol mode, and exercises
`browser_fetch`, `fill_native`, `solve_image_captcha`, `mouse_click` and
`click_by_vision` against the mock grid, plus deferred confirmation with a
token-protected inbox in both modes. Verified release baseline:
**44 unit tests, 10 e2e tests**.

Full smoke on all three engines:

```bash
python scripts/autoreg.py --home /tmp/autoreg-selftest selftest --engine patchright
python scripts/autoreg.py --home /tmp/autoreg-selftest-cf selftest --engine camoufox
python scripts/autoreg.py --home /tmp/autoreg-selftest-pw selftest --engine playwright
```

## Requirements

Python 3.13 and packages from `requirements.txt`; install a browser for the
selected engine. Optional engines and external provider/model credentials are
not bundled. The pack does not require Docker.

## Security

- `.autoreg/key` decrypts `accounts.db`. Back it up with the DB; never commit
  either.
- Export with secrets only on explicit request (`accounts export --secrets`).
- Automatic OTP/extract logs report presence, not the code, email subject or
  extracted credential value. Explicit `mail test --wait` output still returns
  the requested code.
- Recon HAR/HTML/screenshots and failure captures contain raw page content;
  keep `.autoreg/` local. Encryption of the account database does not encrypt
  browser profiles or capture files.
- `.gitattributes` keeps shell launchers LF on Windows, even with
  `core.autocrlf=true`.
- Adapters can execute Python hooks. Review third-party adapters before running.
- Use only on services where account creation is authorized; respect terms,
  rate limits and local law.
