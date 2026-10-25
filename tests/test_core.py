from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from autoreg_pkg.captcha import CaptchaSpec, _proxy_fields, solve as solve_captcha  # noqa: E402
from autoreg_pkg.config import Settings, resolve_home  # noqa: E402
from autoreg_pkg.flow import load_adapter  # noqa: E402
from autoreg_pkg.identity import (compare_fingerprint, make_profile,  # noqa: E402
                                  init_script, validate_profile)
from autoreg_pkg import mail as mail_module  # noqa: E402
from autoreg_pkg.mail import (ImapProvider, MailProvider, MailTimeout,  # noqa: E402
                              VoidashProvider, extract_code)
from autoreg_pkg.proxy import ProxyError, parse_proxy  # noqa: E402
from autoreg_pkg.store import Store  # noqa: E402
from autoreg_pkg.util import (ConfigError, FlowError, classify_error, deep_get,  # noqa: E402
                              policy_for, redact, render)


class CodeExtractionTest(unittest.TestCase):
    def test_keyword(self):
        self.assertEqual(extract_code("Your verification code is 123456")[0], "123456")

    def test_html(self):
        self.assertEqual(extract_code("<p>Code: <b>654321</b></p>")[0], "654321")

    def test_spaced(self):
        self.assertEqual(extract_code("Your code is 123 456.")[0], "123456")

    def test_year_is_not_a_code(self):
        self.assertIsNone(extract_code("Order shipped on 2026-10-24")[0])

    def test_custom_pattern(self):
        self.assertEqual(extract_code("token=abcdef", r"token=(\w+)")[0], "abcdef")

    def test_pin_keyword(self):
        self.assertEqual(extract_code("Your PIN is 4821")[0], "4821")

    def test_alphanumeric_labeled(self):
        self.assertEqual(extract_code("Your verification code: 7F3K9Q")[0], "7F3K9Q")

    def test_isolated_html_tag(self):
        self.assertEqual(extract_code("<td class='code'>7F3K9Q</td>")[0], "7F3K9Q")

    def test_hex_labeled(self):
        self.assertEqual(extract_code("verification code: 5fde27d1")[0], "5fde27d1")

    def test_iso_timestamp_is_not_a_code(self):
        self.assertIsNone(extract_code("created 2026-08-12T16:25:44.015192Z")[0])

    def test_email_digits_ignored(self):
        self.assertIsNone(extract_code("u123456@example.com", email="u123456@example.com")[0])

    def test_zero_code_rejected(self):
        self.assertIsNone(extract_code("code 000000")[0])


class IdentityTest(unittest.TestCase):
    def test_deterministic(self):
        a = make_profile("seed-1", os_name="windows")
        b = make_profile("seed-1", os_name="windows")
        self.assertEqual(a["screen"], b["screen"])
        self.assertEqual(a["hardware"], b["hardware"])
        self.assertEqual(a["webgl"], b["webgl"])
        self.assertEqual(a["id"], b["id"])

    def test_different_seed(self):
        a = make_profile("seed-1", os_name="windows")
        b = make_profile("seed-2", os_name="windows")
        self.assertNotEqual(a["id"], b["id"])

    def test_valid(self):
        profile = make_profile("valid", os_name="macos")
        self.assertEqual(validate_profile(profile), [])
        self.assertLessEqual(profile["viewport"]["width"], profile["screen"]["width"])
        script = init_script(profile)
        self.assertIn("hardwareConcurrency", script)
        self.assertIn("availHeight", script)

    def test_compare(self):
        profile = make_profile("cmp", os_name="windows")
        actual = {
            "webdriver": False, "platform": profile["platform"],
            "language": profile["locale"], "languages": profile["languages"],
            "timezone": profile["timezone"],
            "hardwareConcurrency": profile["hardware"]["concurrency"],
            "deviceMemory": profile["hardware"]["memory"],
            "screen": {
                "width": profile["screen"]["width"],
                "height": profile["screen"]["height"],
                "availWidth": profile["screen"]["avail_width"],
                "availHeight": profile["screen"]["avail_height"],
            },
            "devicePixelRatio": profile["screen"]["pixel_ratio"],
        }
        self.assertEqual(compare_fingerprint(profile, actual), [])
        actual["timezone"] = "Mars/Olympus"
        self.assertTrue(compare_fingerprint(profile, actual))


class StoreTest(unittest.TestCase):
    def test_encrypted_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(home=Path(tmp))
            store = Store(settings)
            try:
                account_id = store.upsert_account(
                    service="svc", email="a@example.com", password="SuperSecret!42",
                    status="verified", profile_id="p_x", proxy="http://h:1",
                    proxy_secret="http://user:pass@h:1",
                )
                self.assertEqual(store.get_password(account_id), "SuperSecret!42")
                self.assertEqual(store.get_proxy(account_id), "http://user:pass@h:1")
                raw = settings.paths.db.read_bytes()
                self.assertNotIn(b"SuperSecret!42", raw)
                self.assertNotIn(b"user:pass", raw)
                store.mark_status(account_id, "invalid", error="boom")
                account = store.get_account(account_id)
                self.assertEqual(account.status, "invalid")
                exported = store.export(Path(tmp) / "out.json")
                rows = json.loads(exported.read_text(encoding="utf-8"))
                self.assertEqual(rows[0]["email"], "a@example.com")
                self.assertNotIn("password", rows[0])
                jsonl = store.export(Path(tmp) / "out.jsonl")
                lines = [json.loads(x) for x in jsonl.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(lines[0]["email"], "a@example.com")
            finally:
                store.close()


class ImapAliasTest(unittest.TestCase):
    def test_gmail_plus_alias(self):
        provider = ImapProvider({
            "host": "imap.gmail.com", "user": "base@gmail.com", "password": "x",
            "alias": {"template": "{user}+{tag}@gmail.com", "tag_prefix": "reg"},
        })
        box = provider.create()
        self.assertTrue(box.address.startswith("base+reg"))
        self.assertTrue(box.address.endswith("@gmail.com"))
        self.assertTrue(provider._search_criteria(box.address).startswith('TO "base+reg'))

    def test_catch_all(self):
        provider = ImapProvider({
            "host": "imap.example.com", "user": "bot@example.com", "password": "x",
            "catch_all": True, "alias_domain": "mail.example.com",
            "alias_prefix": "pk",
        })
        box = provider.create()
        self.assertTrue(box.address.startswith("pkreg"))
        self.assertTrue(box.address.endswith("@mail.example.com"))

    def test_fixed_address(self):
        provider = ImapProvider({
            "host": "imap.example.com", "user": "bot@example.com", "password": "x",
        })
        self.assertEqual(provider.create().address, "bot@example.com")


class VoidashTest(unittest.TestCase):
    def test_create_and_messages(self):
        original = mail_module._http_json

        def fake(method, url, **kwargs):
            if url.endswith("/inboxes"):
                return {"address": "a@voidash.bond", "session_key": "sk"}
            if url.endswith("/messages"):
                return {"messages": [{
                    "id": "m1", "from": "noreply@target.test",
                    "subject": "Your code", "text": "Your code is 123456",
                    "created_at": "2026-10-24T10:00:00Z",
                }]}
            if url.endswith("/messages/m1"):
                return {"text": "Your code is 123456"}
            raise AssertionError(url)

        mail_module._http_json = fake
        try:
            provider = VoidashProvider({})
            box = provider.create()
            self.assertEqual(box.address, "a@voidash.bond")
            self.assertEqual(box.token, "sk")
            messages = provider.messages(box)
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].sender, "noreply@target.test")
            self.assertEqual(extract_code(messages[0].body, email=box.address)[0], "123456")
        finally:
            mail_module._http_json = original


class CaptchaHelperTest(unittest.TestCase):
    def test_proxy_fields(self):
        proxy = parse_proxy("socks5://u:p@1.2.3.4:1080")
        two = _proxy_fields(proxy, two_captcha=True)
        self.assertEqual(two["proxy"], "1.2.3.4:1080")
        self.assertEqual(two["proxytype"], "SOCKS5")
        self.assertEqual(two["proxy_user"], "u")
        yes = _proxy_fields(proxy, yescaptcha=True)
        self.assertEqual(yes["proxyType"], "socks5")
        self.assertEqual(yes["proxyPort"], 1080)

    def test_empty_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(home=Path(tmp))
            self.assertEqual(solve_captcha(settings, CaptchaSpec(provider="empty")), "")


class ErrorPolicyTest(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(classify_error(MailTimeout("no mail")), "mail")
        self.assertEqual(classify_error(ProxyError("dead")), "proxy")
        self.assertEqual(classify_error(FlowError("HTTP 429 too many requests")), "ratelimit")
        self.assertEqual(classify_error(FlowError("Locator.click: waiting for locator('#x')")), "target")
        self.assertEqual(classify_error(FlowError("ConfigError: bad adapter")), "config")
        self.assertEqual(policy_for("blocked"), "stop")
        self.assertEqual(policy_for("target"), "stop")
        self.assertEqual(policy_for("mail"), "retry")
        self.assertEqual(policy_for("ratelimit"), "retry")


class ProxyTest(unittest.TestCase):
    def test_formats(self):
        p = parse_proxy("http://user:pass@1.2.3.4:8080")
        self.assertEqual((p.host, p.port, p.username), ("1.2.3.4", 8080, "user"))
        self.assertEqual(p.playwright()["server"], "http://1.2.3.4:8080")
        q = parse_proxy("1.2.3.4:8080:user:pass")
        self.assertEqual(q.username, "user")
        self.assertIsNone(parse_proxy("direct"))
        self.assertNotIn("pass", str(p))

    def test_redact(self):
        text = redact("Authorization: Bearer abc123 token=xyz password=hunter2")
        self.assertNotIn("abc123", text)
        self.assertNotIn("hunter2", text)


class AdapterTest(unittest.TestCase):
    def test_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.yaml"
            path.write_text(
                "name: t\nsteps:\n  - goto: https://example.com\n"
                "  - wait_for: '#x'\n", encoding="utf-8")
            adapter = load_adapter(path)
            self.assertEqual(adapter.name, "t")
            self.assertEqual(adapter.mode, "browser")

    def test_unknown_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("name: bad\nsteps:\n  - teleport: now\n", encoding="utf-8")
            with self.assertRaises(Exception):
                load_adapter(path)

    def test_missing_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("name: bad\nsteps: []\n", encoding="utf-8")
            with self.assertRaises(Exception):
                load_adapter(path)


class UtilTest(unittest.TestCase):
    def test_render_and_deep_get(self):
        data = {"a": {"b": [{"c": 7}]}}
        self.assertEqual(deep_get(data, "a.b.0.c"), 7)
        self.assertIsNone(deep_get(data, "a.nope"))
        self.assertEqual(render("{{a}}/{{b}}", {"a": 1, "b": 2}), "1/2")
        self.assertEqual(render("{{a}}", {"a": {"x": 1}}), {"x": 1})
        with self.assertRaises(Exception):
            render("{{missing}}", {})

    def test_home(self):
        self.assertEqual(resolve_home(None).name, ".autoreg")
        self.assertEqual(resolve_home("C:/tmp/project/.autoreg").name, ".autoreg")
        self.assertTrue(str(resolve_home("C:/tmp/project")).endswith(".autoreg"))


class _StaticProvider(MailProvider):
    """Returns a fixed message list; only the snapshot path is exercised."""

    name = "static"

    def __init__(self, messages):
        super().__init__({})
        self.items = list(messages)

    def create(self):
        raise AssertionError("create() must not run for a restored session")

    def messages(self, box):
        return list(self.items)


def _msg(mid: str, body: str) -> mail_module.Message:
    return mail_module.Message(id=mid, sender="no-reply@target.test",
                               subject="Your verification code", body=body)


class MailSessionTest(unittest.TestCase):
    def _session(self) -> mail_module.MailSession:
        return mail_module.MailSession(
            provider_config={"type": "voidash", "base": "https://mail.test/api",
                             "domain": "voidash.bond"},
            mailbox=mail_module.Mailbox(address="a@voidash.bond",
                                        token="fixture-mail-secret",
                                        meta={"base": "https://mail.test/api",
                                              "password": "box-pass"}),
            ignored_ids={"m3", "m1"},
            baseline_armed=True,
        )

    def test_roundtrip(self):
        session = self._session()
        restored = mail_module.MailSession.from_dict(json.loads(json.dumps(session.to_dict())))
        self.assertEqual(restored.provider_config, session.provider_config)
        self.assertEqual(restored.mailbox.address, session.mailbox.address)
        self.assertEqual(restored.mailbox.token, "fixture-mail-secret")
        self.assertEqual(restored.mailbox.meta, session.mailbox.meta)
        self.assertEqual(restored.ignored_ids, {"m1", "m3"})
        self.assertTrue(restored.baseline_armed)
        self.assertEqual(session.to_dict()["ignored_ids"], ["m1", "m3"])

    def test_snapshot_is_detached(self):
        session = self._session()
        payload = session.to_dict()
        payload["provider_config"]["base"] = "https://evil.test"
        self.assertEqual(session.provider_config["base"], "https://mail.test/api")

    def test_invalid_payloads(self):
        good = self._session().to_dict()
        cases = [
            None,
            "not-a-mapping",
            {**good, "version": 99},
            {**good, "provider_config": {}},
            {**good, "provider_config": {"type": 5}},
            {**good, "mailbox": {"address": ""}},
            {**good, "mailbox": {"address": "a@b.c", "token": 7}},
            {**good, "mailbox": {"address": "a@b.c", "meta": []}},
            {**good, "ignored_ids": [1, 2]},
            {**good, "baseline_armed": "yes"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(mail_module.MailSessionError):
                    mail_module.MailSession.from_dict(payload)

    def test_resolved_provider_ignores_current_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(home=Path(tmp), raw={
                "mail": {"default": "custom_http", "providers": {
                    "custom_http": {"base": "http://127.0.0.1:1",
                                    "headers": {"X-Current-Config": "wrong"}}}}})
            provider = mail_module.make_provider(settings, {
                "type": "custom_http", "base": "https://mail.test/api",
                "headers": {"Authorization": "Bearer saved"},
            }, resolved=True)
            self.assertEqual(provider.base, "https://mail.test/api")
            self.assertEqual(provider.headers, {"Authorization": "Bearer saved"})
            merged = mail_module.make_provider(settings, {
                "type": "custom_http", "headers": {"Authorization": "Bearer saved"},
            })
            self.assertEqual(merged.base, "http://127.0.0.1:1")
            with self.assertRaises(ConfigError):
                mail_module.make_provider(settings, "custom_http", resolved=True)


class MailSessionStoreTest(unittest.TestCase):
    def test_encrypted_roundtrip_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(home=Path(tmp))
            session = MailSessionTest()._session()
            store = Store(settings)
            try:
                account_id = store.upsert_account(
                    service="svc", email="a@voidash.bond", password="SuperSecret!42",
                    status="pending", mail_session=session)
                restored = store.get_mail_session(account_id)
                self.assertEqual(restored.mailbox.token, "fixture-mail-secret")
                self.assertEqual(restored.ignored_ids, {"m1", "m3"})
                raw = settings.paths.db.read_bytes()
                for wal in settings.paths.db.parent.glob("accounts.db-*"):
                    raw += wal.read_bytes()
                self.assertNotIn(b"fixture-mail-secret", raw)
                self.assertNotIn(b"box-pass", raw)

                # An update without a session must not drop the stored one.
                store.upsert_account(service="svc", email="a@voidash.bond",
                                     password="SuperSecret!42", status="pending")
                self.assertEqual(store.get_mail_session(account_id).mailbox.token,
                                 "fixture-mail-secret")

                for suffix in (".json", ".csv", ".jsonl"):
                    exported = store.export(Path(tmp) / f"out{suffix}", include_secrets=True)
                    self.assertNotIn("fixture-mail-secret",
                                     exported.read_text(encoding="utf-8"))
                    self.assertNotIn("box-pass", exported.read_text(encoding="utf-8"))
                    self.assertNotIn("mail_session", exported.read_text(encoding="utf-8"))

                store.set_mail_session(account_id, session.__class__(
                    provider_config=session.provider_config, mailbox=session.mailbox,
                    ignored_ids={"m1", "m3", "m4"}, baseline_armed=True))
                self.assertEqual(store.get_mail_session(account_id).ignored_ids,
                                 {"m1", "m3", "m4"})

                store.delete_account(account_id)
                self.assertIsNone(store.get_mail_session(account_id))
            finally:
                store.close()

    def test_migration_from_old_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(home=Path(tmp))
            settings.paths.ensure()
            legacy = """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL,
                email TEXT NOT NULL, password_enc BLOB,
                status TEXT NOT NULL DEFAULT 'created', profile_id TEXT,
                proxy TEXT, engine TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, verified_at TEXT, meta TEXT,
                artifact_dir TEXT, UNIQUE(service, email));
            """
            conn = sqlite3.connect(str(settings.paths.db))
            conn.executescript(legacy)
            conn.execute(
                "INSERT INTO accounts (service,email,status,created_at,updated_at)"
                " VALUES ('svc','old@example.com','pending','now','now')")
            conn.commit()
            conn.close()

            session = MailSessionTest()._session()
            for _ in range(2):
                store = Store(settings)
                try:
                    account = store.find_account("svc", "old@example.com")
                    self.assertIsNotNone(account)
                    self.assertEqual(account.status, "pending")
                    if _ == 0:
                        store.set_mail_session(account.id, session)
                    self.assertEqual(store.get_mail_session(account.id).mailbox.address,
                                     "a@voidash.bond")
                finally:
                    store.close()

    def test_corrupt_session_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(home=Path(tmp))
            store = Store(settings)
            try:
                account_id = store.upsert_account(service="svc", email="x@example.com",
                                                  password="pw", status="pending")
                store.conn.execute("UPDATE accounts SET mail_session_enc=? WHERE id=?",
                                   (b"garbage", account_id))
                store.conn.commit()
                with self.assertRaises(mail_module.MailSessionError):
                    store.get_mail_session(account_id)
                self.assertEqual(store.get_account(account_id).status, "pending")
            finally:
                store.close()


class MailArmTest(unittest.TestCase):
    def _ctx(self, provider, messages, *, adapter_extra=None):
        from autoreg_pkg.flow import FlowContext
        data = {"name": "t", "steps": [{"goto": "https://example.test"}]}
        data.update(adapter_extra or {})
        adapter = load_adapter(self._write_adapter(data))
        return FlowContext(
            settings=Settings(home=Path(tempfile.mkdtemp(prefix="autoreg-arm-"))),
            adapter=adapter, store=None, run_id="arm", attempt=1,
            email="a@voidash.bond", password="pw",
            mailbox=mail_module.Mailbox(address="a@voidash.bond"),
            mail_provider=provider, profile={}, profile_dir=Path("."),
            proxy=None, artifacts_dir=Path("."),
        )

    def _write_adapter(self, data):
        import yaml
        tmp = Path(tempfile.mkdtemp(prefix="autoreg-adapter-"))
        path = tmp / "a.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_auto_arm_is_idempotent_and_force_refreshes(self):
        from autoreg_pkg.flow import _arm_if_needed
        provider = _StaticProvider([_msg("m1", "Your code is 111111")])
        ctx = self._ctx(provider, provider.items)
        _arm_if_needed(ctx)
        self.assertTrue(ctx.mail_armed)
        self.assertEqual(ctx.mail_ids, {"m1"})

        provider.items.append(_msg("m2", "Your code is 654321"))
        _arm_if_needed(ctx)
        self.assertEqual(ctx.mail_ids, {"m1"}, "auto re-arm must not swallow new mail")
        result = provider.wait_code(ctx.mailbox, timeout=1, interval=0.01,
                                    since_ids=set(ctx.mail_ids))
        self.assertEqual(result.code, "654321")

        provider.items.append(_msg("m3", "Your code is 765432"))
        _arm_if_needed(ctx, force=True)
        self.assertEqual(ctx.mail_ids, {"m1", "m2", "m3"})
        with self.assertRaises(MailTimeout):
            provider.wait_code(ctx.mailbox, timeout=0.2, interval=0.01,
                               since_ids=set(ctx.mail_ids))

    def test_inherited_baseline_is_never_rearmed(self):
        from autoreg_pkg.flow import _arm_if_needed
        provider = _StaticProvider([_msg("m1", "Your code is 111111")])
        ctx = self._ctx(provider, provider.items)
        ctx.mail_armed = False
        ctx.mail_auto_arm = False
        _arm_if_needed(ctx)
        self.assertEqual(ctx.mail_ids, set())
        self.assertFalse(ctx.mail_armed)
        # The explicit action still works, and only it.
        _arm_if_needed(ctx, force=True)
        self.assertEqual(ctx.mail_ids, {"m1"})


class VerifyStatusTest(unittest.TestCase):
    """A restore problem must never be reported as an invalid account."""

    def _adapter(self, body: str):
        import yaml
        tmp = Path(tempfile.mkdtemp(prefix="autoreg-verify-"))
        path = tmp / "a.yaml"
        path.write_text(body, encoding="utf-8")
        return load_adapter(path)

    def _settings(self):
        tmp = Path(tempfile.mkdtemp(prefix="autoreg-verify-home-"))
        settings = Settings(home=tmp)
        settings.paths.ensure()
        return settings

    def test_missing_session_keeps_original_status(self):
        from autoreg_pkg.runner import save_profile
        from autoreg_pkg.verify import verify_account
        from autoreg_pkg.identity import make_profile
        adapter = self._adapter(
            "name: v\nsteps:\n  - goto: https://example.test\n"
            "verify:\n  - otp:\n      timeout: 1\n")
        for status in ("pending", "created", "unverified"):
            with self.subTest(status=status):
                settings = self._settings()
                profile = make_profile(f"seed-{status}", os_name="windows")
                save_profile(settings, profile)
                store = Store(settings)
                try:
                    account_id = store.upsert_account(
                        service="v", email="a@example.com", password="pw",
                        status=status, profile_id=profile["id"])
                    account = store.get_account(account_id)
                    result = verify_account(settings, adapter, account, store=store)
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["status"], status, result)
                    self.assertIn("MailSessionError", result["error"])
                    self.assertEqual(store.get_account(account_id).status, status)
                finally:
                    store.close()

    def test_mail_failure_classification(self):
        from autoreg_pkg.verify import _is_mail_failure
        self.assertTrue(_is_mail_failure("MailTimeout: no matching mail"))
        self.assertTrue(_is_mail_failure("MailError: HTTP 401 GET /messages"))
        self.assertTrue(_is_mail_failure("MailSessionError: Cannot restore stored mail session"))
        self.assertFalse(_is_mail_failure("FlowError: HTTP 429 too many requests"))
        self.assertFalse(_is_mail_failure(None))


if __name__ == "__main__":
    unittest.main()
