"""autoreg command line."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .browser import fpcheck, system_report
from .config import ensure_home, load_settings, resolve_home
from .flow import load_adapter
from .identity import make_profile, public_profile, validate_profile
from .proxy import parse_proxy, probe
from .runner import load_profile, run_batch, save_profile
from .store import Store
from .util import ConfigError, friendly_exc, now_iso, short_id

SKILL_DIR = Path(__file__).resolve().parents[2]


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(data, list):
        for item in data:
            print(item)
    elif isinstance(data, dict):
        for key, value in data.items():
            print(f"{key}: {value}")
    else:
        print(data)


def _adapter_path(settings: Any, value: str) -> Path:
    p = Path(value)
    candidates = [p, settings.paths.adapters / value,
                  SKILL_DIR / "assets" / "adapters" / value]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise ConfigError(f"Adapter not found: {value} (looked in {', '.join(str(c) for c in candidates)})")


def _copy_templates(home: Path) -> list[str]:
    source = SKILL_DIR / "assets" / "adapters"
    target = home / "adapters"
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    if source.is_dir():
        for item in sorted(source.glob("*.yaml")):
            dest = target / item.name
            if not dest.exists():
                shutil.copy2(item, dest)
                copied.append(item.name)
    return copied


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    home = resolve_home(args.home)
    settings = ensure_home(home, create_config=True, force=args.force)
    copied = _copy_templates(home)
    print(f"home: {home}")
    print(f"config: {settings.paths.config}")
    if copied:
        print("templates: " + ", ".join(copied))
    print("\nNext:")
    print(f"  1. autoreg --home {home.parent} doctor")
    print(f"  2. autoreg --home {home.parent} recon https://target.example/signup")
    print(f"  3. edit {home / 'adapters' / 'generic-email-otp.yaml'}")
    print(f"  4. autoreg --home {home.parent} run <adapter> --count 1 --headed")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings(args.home)
    settings.paths.ensure()
    report: dict[str, Any] = {
        "version": __version__,
        "home": str(settings.home),
        "config": str(settings.paths.config),
        "config_exists": settings.paths.config.is_file(),
        "system": system_report(),
        "settings": {
            "engine": settings.get("engine"),
            "mail_default": settings.get("mail.default"),
            "captcha_default": settings.get("captcha.default"),
            "proxy_default": settings.get("proxy.default"),
            "concurrency": settings.get("limits.concurrency"),
        },
        "checks": [],
    }
    checks: list[dict[str, Any]] = report["checks"]

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("python", sys.version_info >= (3, 10), sys.version.split()[0])
    for module in ("yaml", "cryptography", "requests"):
        try:
            __import__(module)
            check(f"dep:{module}", True, "installed")
        except Exception as exc:
            check(f"dep:{module}", False, friendly_exc(exc))
    try:
        __import__("curl_cffi")
        check("dep:curl_cffi", True, "installed (protocol mode)")
    except Exception:
        check("dep:curl_cffi", False, "missing (protocol mode unavailable)")
    for engine, info in report["system"]["engines"].items():
        check(f"engine:{engine}", bool(info["available"]), info["detail"])
    try:
        settings.paths.home.mkdir(parents=True, exist_ok=True)
        probe_file = settings.paths.home / ".doctor-write-test"
        probe_file.write_text(now_iso(), encoding="utf-8")
        probe_file.unlink()
        check("home:writable", True, str(settings.paths.home))
    except Exception as exc:
        check("home:writable", False, friendly_exc(exc))
    try:
        store = Store(settings)
        count = store.count()
        store.close()
        check("store", True, f"{settings.paths.db} ({count} accounts)")
    except Exception as exc:
        check("store", False, friendly_exc(exc))
    if settings.get("mail.default") == "mock":
        check("mail:default", True, "mock (local tests only)")
    else:
        check("mail:default", bool(settings.get("mail.default")),
              str(settings.get("mail.default")))
    if args.probe:
        try:
            geo = probe(None)
            report["direct_geo"] = geo
            check("network:direct", True, f"{geo.get('ip')} {geo.get('country_code')}")
        except Exception as exc:
            check("network:direct", False, friendly_exc(exc))
    if args.json:
        _emit(report, True)
    else:
        print(f"autoreg {__version__}  home={settings.home}")
        print(f"config: {'yes' if report['config_exists'] else 'NO (run init)'}")
        for item in checks:
            mark = "ok " if item["ok"] else "FAIL"
            print(f"[{mark}] {item['name']}: {item['detail']}")
        if report.get("direct_geo"):
            print(f"direct geo: {report['direct_geo'].get('ip')} {report['direct_geo'].get('country_code')}")
    return 0 if all(c["ok"] for c in checks if c["name"].startswith(("python", "home:", "store"))) else 1


def cmd_profile(args: argparse.Namespace) -> int:
    settings = load_settings(args.home)
    settings.paths.ensure()
    sub = args.profile_cmd
    if sub == "new":
        geo = None
        proxy = parse_proxy(args.proxy) if args.proxy else None
        if proxy:
            geo = probe(proxy)
        seed = args.seed or "profile-" + short_id(6)
        profile = make_profile(seed, os_name=args.os, geo=geo,
                               engine=args.engine or "auto",
                               canvas_noise=args.canvas_noise)
        errors = validate_profile(profile)
        if errors:
            _emit({"ok": False, "errors": errors}, args.json)
            return 1
        path = save_profile(settings, profile)
        data = public_profile(profile)
        data["path"] = str(path)
        _emit(data, args.json)
        return 0
    if sub == "list":
        items = []
        for path in sorted(settings.paths.profiles.glob("*.json")):
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            items.append({
                "id": profile.get("id"), "os": profile.get("os"),
                "locale": profile.get("locale"), "timezone": profile.get("timezone"),
                "engine": profile.get("engine"), "created_at": profile.get("created_at"),
                "path": str(path),
            })
        if args.json:
            _emit(items, True)
        else:
            for item in items:
                print(f"{item['id']}  {item['os']:<7} {item['locale']:<6} "
                      f"{item['timezone']:<28} {item['engine']}")
            if not items:
                print("(no profiles)")
        return 0
    if sub == "show":
        profile = load_profile(settings, args.profile_id)
        _emit(public_profile(profile), args.json)
        return 0
    if sub == "delete":
        path = settings.paths.profiles / (args.profile_id + ".json")
        data_dir = settings.paths.profiles / args.profile_id
        if not path.exists() and not data_dir.exists():
            raise ConfigError(f"Profile not found: {args.profile_id}")
        if not args.yes:
            print(f"Refusing to delete {args.profile_id} without --yes", file=sys.stderr)
            return 2
        path.unlink(missing_ok=True)
        shutil.rmtree(data_dir, ignore_errors=True)
        print(f"deleted {args.profile_id}")
        return 0
    if sub == "fpcheck":
        profile = load_profile(settings, args.profile_id)
        actual, mismatches, engine = fpcheck(
            settings, profile, engine=args.engine or "auto",
            headed=args.headed,
            proxy=parse_proxy(args.proxy) if args.proxy else None,
        )
        result = {"profile": profile["id"], "engine": engine,
                  "actual": actual, "mismatches": mismatches}
        _emit(result, args.json)
        return 0 if not mismatches else 1
    raise ConfigError(f"Unknown profile command {sub}")


def cmd_recon(args: argparse.Namespace) -> int:
    from .recon import recon
    settings = load_settings(args.home)
    settings.paths.ensure()
    proxy = parse_proxy(args.proxy) if args.proxy else None
    result = recon(settings, args.url, engine=args.engine, headed=args.headed,
                   settle=args.settle,
                   out_dir=Path(args.out) if args.out else None, proxy=proxy)
    data = {"url": result.url, "out_dir": str(result.out_dir),
            "report": str(result.report), "skeleton": str(result.skeleton),
            "summary": result.summary}
    _emit(data, args.json)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(args.home)
    settings.paths.ensure()
    adapter_path = _adapter_path(settings, args.adapter)
    overrides: dict[str, Any] = {}
    for item in args.var or []:
        if "=" not in item:
            raise ConfigError(f"--var must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        overrides[key.strip()] = value
    summary = run_batch(
        settings, adapter_path, count=args.count, concurrency=args.concurrency,
        engine=args.engine, headed=args.headed, proxy=args.proxy,
        dry_run=args.dry_run, no_verify=args.no_verify,
        var_overrides=overrides, email=args.email, password=args.password,
        stop_on_error=args.stop_on_error,
    )
    _emit(summary.as_dict(), args.json)
    return 0 if summary.failed == 0 else 1


def cmd_verify(args: argparse.Namespace) -> int:
    from .verify import verify_account, verify_all
    settings = load_settings(args.home)
    settings.paths.ensure()
    adapter = load_adapter(_adapter_path(settings, args.adapter))
    if args.all:
        results = verify_all(settings, _adapter_path(settings, args.adapter),
                              engine=args.engine, headed=args.headed, status=args.status)
        _emit(results, args.json)
        return 0 if all(r["ok"] for r in results) else 1
    if not args.account:
        raise ConfigError("verify needs --account ID or --all")
    store = Store(settings)
    try:
        account = store.get_account(args.account)
        if not account:
            raise ConfigError(f"Account {args.account} not found")
        result = verify_account(settings, adapter, account, engine=args.engine,
                                headed=args.headed, proxy_url=args.proxy, store=store)
        _emit(result, args.json)
        return 0 if result["ok"] else 1
    finally:
        store.close()


def cmd_accounts(args: argparse.Namespace) -> int:
    settings = load_settings(args.home)
    settings.paths.ensure()
    store = Store(settings)
    try:
        sub = args.accounts_cmd
        if sub == "list":
            accounts = store.list_accounts(service=args.service, status=args.status)
            rows = [a.public() for a in accounts]
            if args.json:
                _emit(rows, True)
            else:
                for row in rows:
                    print(f"{row['id']:>4}  {row['service']:<24} {row['email']:<38} "
                          f"{row['status']:<11} {row['profile_id'] or '-':<16} {row['created_at']}")
                if not rows:
                    print("(no accounts)")
            return 0
        if sub == "show":
            account = store.get_account(args.account_id)
            if not account:
                raise ConfigError(f"Account {args.account_id} not found")
            data = account.public()
            _emit(data, args.json)
            return 0
        if sub == "export":
            path = store.export(args.path, include_secrets=args.secrets,
                                service=args.service)
            print(f"exported {store.count(args.service)} account(s) to {path}"
                  + (" (with secrets)" if args.secrets else ""))
            return 0
        if sub == "delete":
            if not args.yes:
                print(f"Refusing to delete account {args.account_id} without --yes", file=sys.stderr)
                return 2
            ok = store.delete_account(args.account_id)
            print("deleted" if ok else "not found")
            return 0 if ok else 1
        raise ConfigError(f"Unknown accounts command {sub}")
    finally:
        store.close()


def cmd_adapters(args: argparse.Namespace) -> int:
    settings = load_settings(args.home)
    settings.paths.ensure()
    paths: list[Path] = []
    if args.all:
        paths = sorted(settings.paths.adapters.glob("*.yaml")) + \
            sorted((SKILL_DIR / "assets" / "adapters").glob("*.yaml"))
    elif args.path:
        paths = [_adapter_path(settings, args.path)]
    else:
        raise ConfigError("adapters validate needs a path or --all")
    results = []
    failed = 0
    for path in paths:
        try:
            adapter = load_adapter(path)
            results.append({"path": str(path), "name": adapter.name,
                            "mode": adapter.mode, "steps": len(adapter.steps),
                            "ok": True})
        except Exception as exc:
            failed += 1
            results.append({"path": str(path), "ok": False, "error": friendly_exc(exc)})
    _emit(results, args.json)
    return 0 if failed == 0 else 1


def cmd_mail(args: argparse.Namespace) -> int:
    from .mail import make_provider
    settings = load_settings(args.home)
    settings.paths.ensure()
    provider = make_provider(settings, args.provider)
    try:
        box = provider.create()
        print(f"address: {box.address}")
        if args.wait:
            result = provider.wait_code(
                box, code_regex=args.regex, sender_contains=args.sender,
                subject_contains=args.subject, timeout=args.timeout,
            )
            data = {"address": box.address, "code": result.code,
                    "sender": result.message.sender, "subject": result.message.subject,
                    "how": result.how}
            _emit(data, args.json)
            return 0 if result.code else 1
        return 0
    finally:
        provider.close()


def cmd_proxy(args: argparse.Namespace) -> int:
    proxy = parse_proxy(args.url)
    result = probe(proxy)
    _emit(result, args.json)
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import run_selftest
    return run_selftest(args.home, engine=args.engine, keep=args.keep)


def cmd_version(args: argparse.Namespace) -> int:
    print(__version__)
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autoreg",
        description="Build and run service registrations with consistent identities.",
    )
    parser.add_argument("--home", help="project directory or its .autoreg home")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create .autoreg, config and adapter templates")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check environment, engines, store and config")
    p.add_argument("--probe", action="store_true", help="probe the direct exit IP")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("profile", help="manage identities")
    ps = p.add_subparsers(dest="profile_cmd", required=True)
    pn = ps.add_parser("new")
    pn.add_argument("--seed")
    pn.add_argument("--os", choices=["windows", "macos", "linux"])
    pn.add_argument("--proxy")
    pn.add_argument("--engine", default="auto")
    pn.add_argument("--canvas-noise", action="store_true")
    pn.set_defaults(func=cmd_profile)
    pl = ps.add_parser("list")
    pl.set_defaults(func=cmd_profile)
    psh = ps.add_parser("show")
    psh.add_argument("profile_id")
    psh.set_defaults(func=cmd_profile)
    pd = ps.add_parser("delete")
    pd.add_argument("profile_id")
    pd.add_argument("--yes", action="store_true")
    pd.set_defaults(func=cmd_profile)
    pf = ps.add_parser("fpcheck")
    pf.add_argument("profile_id")
    pf.add_argument("--engine", default="auto")
    pf.add_argument("--headed", action="store_true")
    pf.add_argument("--proxy")
    pf.set_defaults(func=cmd_profile)

    p = sub.add_parser("recon", help="capture a signup flow and generate a skeleton")
    p.add_argument("url")
    p.add_argument("--out")
    p.add_argument("--engine", default="auto")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--settle", type=float, default=3.0)
    p.add_argument("--proxy")
    p.set_defaults(func=cmd_recon)

    p = sub.add_parser("run", help="run an adapter")
    p.add_argument("adapter")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--engine", default=None)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--proxy")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--var", action="append", metavar="KEY=VALUE")
    p.add_argument("--email")
    p.add_argument("--password")
    p.add_argument("--stop-on-error", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("verify", help="re-check an account or all accounts")
    p.add_argument("adapter")
    p.add_argument("--account", type=int)
    p.add_argument("--all", action="store_true")
    p.add_argument("--status")
    p.add_argument("--engine", default=None)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--proxy")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("accounts", help="inspect/export the account store")
    asub = p.add_subparsers(dest="accounts_cmd", required=True)
    al = asub.add_parser("list")
    al.add_argument("--service")
    al.add_argument("--status")
    al.set_defaults(func=cmd_accounts)
    ash = asub.add_parser("show")
    ash.add_argument("account_id", type=int)
    ash.set_defaults(func=cmd_accounts)
    ae = asub.add_parser("export")
    ae.add_argument("path")
    ae.add_argument("--secrets", action="store_true")
    ae.add_argument("--service")
    ae.set_defaults(func=cmd_accounts)
    ad = asub.add_parser("delete")
    ad.add_argument("account_id", type=int)
    ad.add_argument("--yes", action="store_true")
    ad.set_defaults(func=cmd_accounts)

    p = sub.add_parser("adapters", help="validate adapters")
    asub2 = p.add_subparsers(dest="adapters_cmd", required=True)
    av = asub2.add_parser("validate")
    av.add_argument("path", nargs="?")
    av.add_argument("--all", action="store_true")
    av.set_defaults(func=cmd_adapters)

    p = sub.add_parser("mail", help="create a mailbox / wait for a code")
    p.add_argument("action", choices=["test"])
    p.add_argument("--provider")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--regex")
    p.add_argument("--sender")
    p.add_argument("--subject")
    p.set_defaults(func=cmd_mail)

    p = sub.add_parser("proxy", help="check a proxy exit")
    p.add_argument("action", choices=["check"])
    p.add_argument("url")
    p.set_defaults(func=cmd_proxy)

    p = sub.add_parser("selftest", help="run the bundled mock-site end-to-end test")
    p.add_argument("--engine", default="patchright")
    p.add_argument("--keep", action="store_true")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("version")
    p.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    # Global flags are accepted after the subcommand too.  Hoisting keeps
    # `run ... --json` (the natural way to type it) working.
    if "--json" in raw:
        raw = [item for item in raw if item != "--json"]
        raw.insert(0, "--json")
    args = parser.parse_args(raw)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {friendly_exc(exc)}", file=sys.stderr)
        return 1
