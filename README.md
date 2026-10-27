# Autoreg — Account Registration Automation for AI Agents

**English** | [Русский](README.ru.md)

**An agent skill and Python CLI for repeatable signup and email verification workflows.**

Autoreg turns a service's registration flow into a reusable YAML adapter. It combines browser and HTTP execution, email OTP retrieval, persistent account profiles, proxy routing, verification, and encrypted credential storage in one project-oriented workflow.

**Release:** 1.0.2 · **Maintainer:** [@ikhdev](https://t.me/ikhdev)

[Download the latest release](https://github.com/Ikhdev1/autoreg-agent-skill/releases/latest) · [Agent instructions](SKILL.md) · [Adapter reference](references/adapters.md)

## Features

- **Registration flow discovery:** inspect forms, controls, requests, and protection signals; generate a report and a starter YAML adapter.
- **Browser and HTTP execution:** run interactive flows with Patchright, Playwright, Camoufox, or optional `invisible_playwright`; use `curl_cffi` for protocol-based workflows.
- **Email verification:** retrieve codes and confirmation links through Voidash, mail.tm, mail.gw, IMAP, Outlook Graph, Cloudflare Workers, or a configurable HTTP provider.
- **Persistent account context:** associate each account with its browser profile, mailbox, and proxy assignment; reuse that context across retries.
- **Deferred confirmation:** save an account as `pending`, then restore its encrypted mailbox session in a separate `verify` process without excluding messages that arrived in the meantime.
- **CAPTCHA integrations:** connect 2captcha, YesCaptcha, local services, or manual handling. Vision and speech-to-text workflows use separately configured model endpoints.
- **Interaction tools:** selector-based actions, native-style input, segmented OTP fields, CDP mouse controls, and screenshot-based clicks.
- **Encrypted storage:** protect passwords, proxy credentials, and mailbox sessions with Fernet in SQLite; export account records as JSON, CSV, or JSONL.
- **Operational diagnostics:** environment checks, execution records, failure captures, fingerprint consistency checks, and a local end-to-end test server.

The repository includes **8 adapter templates** and **14 reference guides**. Adapters describe individual services; the execution engine remains shared.

## Installation

Requires **Python 3.13** and a browser installed for the selected engine. Docker is not required.

### Install from Git

```bash
git clone https://github.com/Ikhdev1/autoreg-agent-skill.git "$HOME/.agents/skills/autoreg"
python -m pip install -r "$HOME/.agents/skills/autoreg/requirements.txt"
python -m patchright install chromium
```

### Install from the release ZIP

Download `autoreg-agent-skill-1.0.2.zip` from the [v1.0.2 release](https://github.com/Ikhdev1/autoreg-agent-skill/releases/tag/v1.0.2). The archive contains a top-level `autoreg/` directory. Extract it into the skill directory used by your agent, such as `~/.agents/skills/`.

Then install the Python dependencies and the selected browser as shown above. Release assets include `SHA256SUMS.txt` for archive verification.

For Windows PowerShell:

```powershell
Expand-Archive .\autoreg-agent-skill-1.0.2.zip -DestinationPath "$env:USERPROFILE\.agents\skills"
python -m pip install -r "$env:USERPROFILE\.agents\skills\autoreg\requirements.txt"
python -m patchright install chromium
```

`SKILL.md` is the entry point for agents that support local skills. For other clients, configure their skill search path or load that file directly. Start a new agent session after installation.

### Optional engines

`invisible_playwright` is installed separately:

```bash
python -m pip install invisible-playwright
```

Select it explicitly with `--engine invisible`. Standard `auto` selection prefers Patchright; adapters with `stealth: true` prefer Camoufox when available. Camoufox requires its own browser download:

```bash
python -m camoufox fetch
```

## Quick Start

Run these commands from the project directory that will hold the registration state:

```bash
AUTOREG="$HOME/.agents/skills/autoreg/scripts/autoreg.py"

python "$AUTOREG" --home . init
python "$AUTOREG" --home . doctor
python "$AUTOREG" --home . recon https://target.example/signup
```

Review the generated report and adapter skeleton, then configure the required mail, proxy, CAPTCHA, or model providers. Keep the completed adapter in `.autoreg/adapters/`.

```bash
python "$AUTOREG" --home . run .autoreg/adapters/target.yaml --count 1 --headed
python "$AUTOREG" --home . verify .autoreg/adapters/target.yaml --account 1
```

Windows PowerShell command prefix:

```powershell
$AUTOREG = "$env:USERPROFILE\.agents\skills\autoreg\scripts\autoreg.py"
python $AUTOREG --home . doctor
```

`--home` accepts a project directory or its `.autoreg/` directory. `--json` provides machine-readable output and can appear before or after the subcommand. Progress messages go to stderr.

## Commands

| Command | Purpose |
|---|---|
| `init` | Create project state, default configuration, encryption key, and adapter templates. |
| `doctor` | Check dependencies, engines, configuration, and storage. |
| `recon URL` | Capture a registration page and generate a report and adapter skeleton. |
| `run ADAPTER` | Execute an adapter with configurable account count, concurrency, engine, proxy, and runtime variables. |
| `verify ADAPTER` | Recheck one account or a selected group using its stored context. |
| `accounts list/show/export/delete` | Inspect, export, or remove stored account records. |
| `profile new/list/show/delete/fpcheck` | Manage browser identities and check fingerprint consistency. |
| `adapters validate PATH` | Validate a specific adapter; use `--all` to check available templates. |
| `mail test --provider NAME` | Check mailbox access; add `--wait` to retrieve a code. |
| `proxy check URL` | Inspect a proxy's exit IP and geographic information. |
| `selftest` | Exercise registration, OTP, verification, and storage against the local mock service. |

Run a command with `--help` for its full option list.

## Project State and Deferred Verification

The skill code and project data are separate. Runtime state is stored in `<project>/.autoreg/`, including the database, encryption key, profiles, execution records, and captures.

Setting `mail.defer: true` skips immediate verification and stores the account as `pending`. A later `verify` process restores the original mailbox configuration, token, metadata, and message-ID baseline from encrypted storage. It does not create a replacement inbox.

Restored sessions and pending accounts do not automatically advance the mailbox baseline. An explicit `mail_arm` step is available when the workflow needs a new baseline before requesting another message. Mailbox persistence does not extend the provider's retention period or token lifetime.

See [Two-phase signup and confirmation](references/two-phase.md) and [Mail providers](references/mail.md).

## Repository Layout

```text
autoreg/
  SKILL.md                  Agent-facing workflow and usage instructions
  assets/adapters/          Eight reusable adapter templates
  references/               Adapter, mail, proxy, CAPTCHA, and workflow guides
  scripts/autoreg.py        CLI entry point
  scripts/autoreg_pkg/      Execution engine and provider integrations
  scripts/mock_site.py      Local test service
  tests/                    Unit and browser/HTTP integration tests
```

## Verification

The verified test baseline is **44 unit tests and 10 end-to-end tests**. End-to-end coverage uses the bundled local mock service and includes browser registration, protocol flows, deferred verification with a token-protected inbox, recon, input actions, and vision integration fixtures.

```bash
cd ~/.agents/skills/autoreg
python -m unittest discover -s tests -p "test_core.py" -v
python -m unittest discover -s tests -p "test_e2e.py" -v
```

Run a complete local CLI smoke check:

```bash
python scripts/autoreg.py --home /tmp/autoreg-selftest selftest --engine patchright
```

The included tests do not establish compatibility with every third-party service. New targets require an adapter, appropriate provider access, and an observed registration and verification run. SMS integration is currently hook-based; no SMS provider is bundled. Vision and speech-to-text models, provider subscriptions, and browser binaries are not included in the ZIP.

## Credential and Data Handling

- Store provider credentials in environment variables or a local `.env` file, not in committed adapters.
- The Fernet key in `.autoreg/key` decrypts the account database. Keep it with a protected backup; do not commit either file.
- Account exports exclude mailbox credentials. Password export requires the explicit `accounts export --secrets` option.
- Automatic OTP and extraction logs report presence rather than raw codes, email subjects, or extracted credential values. Explicit `mail test --wait` output returns the requested code.
- Browser profiles, HAR files, HTML captures, and screenshots are local runtime data. Database encryption does not encrypt those files.
- The repository excludes local runtime state, credentials, and caches. Shell launchers retain LF line endings across Windows checkouts.
- Adapters can execute Python hooks; review external adapters before running them.

## Maintainer

Created and maintained by **[@ikhdev](https://t.me/ikhdev)**.
