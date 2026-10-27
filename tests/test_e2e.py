from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mock_site  # noqa: E402

ENGINE = os.getenv("AUTOREG_TEST_ENGINE", "patchright")


def run_cli(home: Path, args: list[str], timeout: float = 240) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPTS / "autoreg.py"), "--home", str(home)] + args
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def parse_json(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, _, cls.base_url = mock_site.start()
        cls.tmp = Path(tempfile.mkdtemp(prefix="autoreg-e2e-"))
        cls.home = cls.tmp / "project"
        proc = run_cli(cls.home, ["init"])
        assert proc.returncode == 0, proc.stderr + proc.stdout

    @classmethod
    def tearDownClass(cls) -> None:
        mock_site.stop(cls.server)
        # Keep artifacts on failure for inspection.
        if os.getenv("AUTOREG_KEEP") == "1":
            print("kept:", cls.tmp)
        else:
            import shutil
            shutil.rmtree(cls.tmp, ignore_errors=True)

    @unittest.skipUnless(ENGINE == "camoufox", "Camoufox-specific persistent identity")
    def test_0_first_fpcheck_keeps_identity_across_processes(self) -> None:
        proc = run_cli(self.home, [
            "profile", "new", "--seed", "first-fpcheck", "--os", "windows",
            "--engine", "camoufox", "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        profile = parse_json(proc)
        path = Path(profile["path"])
        snapshots = []
        for _ in range(2):
            proc = run_cli(self.home, [
                "profile", "fpcheck", profile["id"], "--engine", "camoufox", "--json",
            ])
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            snapshots.append(parse_json(proc)["actual"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(saved["camoufox_fp"], dict)
            if len(snapshots) == 1:
                expected = saved["camoufox_fp"]
            else:
                self.assertEqual(saved["camoufox_fp"], expected)
        for key in ("ua", "platform", "language", "timezone", "screen", "webgl"):
            self.assertEqual(snapshots[0][key], snapshots[1][key], key)

    def test_1_browser_signup_verify_fpcheck(self) -> None:
        proc = run_cli(self.home, [
            "run", "mock.yaml", "--count", "1", "--engine", ENGINE,
            "--var", f"base_url={self.base_url}", "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        summary = parse_json(proc)
        self.assertEqual(summary["success"], 1, summary)
        self.assertTrue(summary["results"][0]["account_id"])
        account_id = summary["results"][0]["account_id"]
        profile_id = summary["results"][0]["profile_id"]
        profile_path = self.home / ".autoreg" / "profiles" / (profile_id + ".json")
        if ENGINE == "camoufox":
            # Old releases could save accounts without a generated fingerprint.
            saved = json.loads(profile_path.read_text(encoding="utf-8"))
            saved["camoufox_fp"] = None
            profile_path.write_text(json.dumps(saved), encoding="utf-8")

        proc = run_cli(self.home, [
            "verify", "mock.yaml", "--account", str(account_id),
            "--engine", ENGINE, "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(parse_json(proc)["ok"])
        if ENGINE == "camoufox":
            saved = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertIsInstance(saved["camoufox_fp"], dict)

        proc = run_cli(self.home, [
            "profile", "fpcheck", profile_id, "--engine", ENGINE, "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        self.assertEqual(parse_json(proc)["mismatches"], [])

        proc = run_cli(self.home, ["accounts", "list", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = parse_json(proc)
        self.assertTrue(any(r["id"] == account_id for r in rows))

    def test_2_recon(self) -> None:
        out = self.tmp / "recon"
        proc = run_cli(self.home, [
            "recon", f"{self.base_url}/signup", "--out", str(out),
            "--engine", ENGINE, "--settle", "1", "--json",
        ], timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        data = parse_json(proc)
        self.assertTrue(Path(data["report"]).is_file())
        self.assertTrue(Path(data["skeleton"]).is_file())
        summary = data["summary"]
        self.assertIn("cloudflare_turnstile", summary["protections"])
        self.assertTrue(summary["forms"])
        self.assertTrue(any(c.get("id") == "email" for c in summary["controls"]))
        # The skeleton must be valid YAML and load as an adapter.
        skeleton = Path(data["skeleton"])
        sys.path.insert(0, str(SCRIPTS))
        from autoreg_pkg.flow import load_adapter
        adapter = load_adapter(skeleton)
        self.assertTrue(adapter.steps)

    def test_3_browser_fetch(self) -> None:
        adapter = self.tmp / "browser-fetch.yaml"
        adapter.write_text(
            "name: browser-fetch\n"
            "mode: browser\n"
            "steps:\n"
            "  - goto: \"{{base_url}}/signup\"\n"
            "  - browser_fetch:\n"
            "      method: POST\n"
            "      url: \"{{base_url}}/api/rpc\"\n"
            "      json: {id: 310, query: {ping: true}}\n"
            "      expect: 200\n"
            "      save: rpc\n"
            "  - extract: {from: rpc, path: result.token, as: token}\n"
            "  - assert: {var: token, equals: rpc-token}\n"
            "verify:\n"
            "  js: \"() => true\"\n",
            encoding="utf-8")
        proc = run_cli(self.home, [
            "run", str(adapter), "--count", "1", "--engine", ENGINE,
            "--var", f"base_url={self.base_url}", "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        summary = parse_json(proc)
        self.assertEqual(summary["success"], 1, summary)
        self.assertEqual(summary["verified"], 1, summary)

    def test_4_deferred_verify(self) -> None:
        adapter = self.tmp / "deferred.yaml"
        adapter.write_text(
            "name: deferred-mock\n"
            "mode: browser\n"
            "mail:\n"
            "  provider: mock\n"
            '  base: "{{base_url}}"\n'
            "  defer: true\n"
            "steps:\n"
            "  - goto: \"{{base_url}}/signup\"\n"
            "verify:\n"
            "  js: \"() => true\"\n",
            encoding="utf-8")
        proc = run_cli(self.home, [
            "run", str(adapter), "--count", "1", "--engine", ENGINE,
            "--var", f"base_url={self.base_url}", "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        summary = parse_json(proc)
        self.assertEqual(summary["pending"], 1, summary)
        account_id = summary["results"][0]["account_id"]
        proc = run_cli(self.home, ["accounts", "list", "--json"])
        rows = {r["id"]: r for r in parse_json(proc)}
        self.assertEqual(rows[account_id]["status"], "pending")
        proc = run_cli(self.home, [
            "verify", str(adapter), "--account", str(account_id),
            "--engine", ENGINE, "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-2000:])
        self.assertTrue(parse_json(proc)["ok"])

    def test_5_vision_and_mouse(self) -> None:
        mock_site.GRID_CLICKS.clear()
        adapter = self.tmp / "vision-grid.yaml"
        adapter.write_text(
            "name: vision-grid\n"
            "mode: browser\n"
            "vision:\n"
            "  provider: openai_compat\n"
            '  base: "{{base_url}}/api"\n'
            "  key: test\n"
            "  model: mock\n"
            "steps:\n"
            "  - goto: \"{{base_url}}/grid\"\n"
            "  - solve_image_captcha:\n"
            "      target: \"#grid\"\n"
            "      prompt: \"click tiles 1 3 7\"\n"
            "      tile_selector: \".tile\"\n"
            "  - mouse_click:\n"
            "      target: \".tile[data-idx='2']\"\n"
            "verify:\n"
            "  js: \"() => true\"\n",
            encoding="utf-8")
        proc = run_cli(self.home, [
            "run", str(adapter), "--count", "1", "--engine", ENGINE,
            "--var", f"base_url={self.base_url}", "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        summary = parse_json(proc)
        self.assertEqual(summary["success"], 1, summary)
        import requests
        clicks = requests.get(f"{self.base_url}/api/grid", timeout=10).json().get("clicks", [])
        self.assertEqual(clicks, [1, 3, 7, 2], f"grid clicks: {clicks}")

    def test_6_click_by_vision(self) -> None:
        mock_site.GRID_CLICKS.clear()
        adapter = self.tmp / "vision-click.yaml"
        adapter.write_text(
            "name: vision-click\n"
            "mode: browser\n"
            "vision:\n"
            "  provider: openai_compat\n"
            '  base: "{{base_url}}/api"\n'
            "  key: test\n"
            "  model: mock\n"
            "steps:\n"
            "  - goto: \"{{base_url}}/grid\"\n"
            "  - click_by_vision:\n"
            "      target: \"#grid\"\n"
            "      description: \"the top-left tile\"\n"
            "verify:\n"
            "  js: \"() => true\"\n",
            encoding="utf-8")
        proc = run_cli(self.home, [
            "run", str(adapter), "--count", "1", "--engine", ENGINE,
            "--var", f"base_url={self.base_url}", "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        summary = parse_json(proc)
        self.assertEqual(summary["success"], 1, summary)
        import requests
        clicks = requests.get(f"{self.base_url}/api/grid", timeout=10).json().get("clicks", [])
        self.assertEqual(clicks, [1], f"grid clicks: {clicks}")

    def test_7_fill_native(self) -> None:
        adapter = self.tmp / "fill-native.yaml"
        adapter.write_text(
            "name: fill-native-mock\n"
            "mode: browser\n"
            "vars:\n"
            '  signup_url: "{{base_url}}/signup"\n'
            "mail:\n"
            "  provider: mock\n"
            '  base: "{{base_url}}"\n'
            '  code_regex: "Your code is (\\\\d{6})"\n'
            "captcha:\n"
            "  provider: static\n"
            "  type: turnstile\n"
            "  token: mock-token\n"
            "steps:\n"
            "  - goto: \"{{signup_url}}\"\n"
            "  - fill_native:\n"
            "      target: \"#email\"\n"
            "      value: \"{{email}}\"\n"
            "  - fill_native:\n"
            "      target: \"#password\"\n"
            "      value: \"{{password}}\"\n"
            "  - click: \"#send-code\"\n"
            "  - wait_for: \"#code-sent\"\n"
            "  - otp:\n"
            "      target: \"#code\"\n"
            "  - captcha:\n"
            "      provider: static\n"
            "  - click: \"#create\"\n"
            "  - wait_for: \"text=Welcome\"\n"
            "verify:\n"
            "  url: \"{{base_url}}/welcome\"\n"
            "  selector: \"text=Sign out\"\n",
            encoding="utf-8")
        proc = run_cli(self.home, [
            "run", str(adapter), "--count", "1", "--engine", ENGINE,
            "--var", f"base_url={self.base_url}", "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        summary = parse_json(proc)
        self.assertEqual(summary["success"], 1, summary)
        self.assertEqual(summary["verified"], 1, summary)

    def test_7_protocol_mode(self) -> None:
        adapter = self.tmp / "protocol-mock.yaml"
        adapter.write_text(
            "name: protocol-mock\n"
            "mode: protocol\n"
            "protocol:\n"
            "  impersonate: chrome\n"
            "mail:\n"
            "  provider: mock\n"
            '  base: "{{base_url}}"\n'
            '  code_regex: "Your code is (\\\\d{6})"\n'
            "steps:\n"
            "  - request:\n"
            "      method: POST\n"
            '      url: "{{base_url}}/api/send-code"\n'
            "      json:\n"
            '        email: "{{email}}"\n'
            "      expect: 200\n"
            "  - otp:\n"
            "      timeout: 60\n"
            "  - request:\n"
            "      method: POST\n"
            '      url: "{{base_url}}/api/verify"\n'
            "      json:\n"
            '        email: "{{email}}"\n'
            '        password: "{{password}}"\n'
            '        code: "{{otp}}"\n'
            '        token: "mock-token"\n'
            "      expect: 200\n"
            "verify:\n"
            "  request:\n"
            "    method: GET\n"
            '    url: "{{base_url}}/welcome"\n'
            "    expect: 200\n",
            encoding="utf-8")
        proc = run_cli(self.home, [
            "run", str(adapter), "--count", "1", "--engine", ENGINE,
            "--var", f"base_url={self.base_url}", "--json",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        summary = parse_json(proc)
        self.assertEqual(summary["success"], 1, summary)
        self.assertEqual(summary["verified"], 1, summary)

    # -- deferred verify against a token-protected inbox ------------------
    _DEFERRED_HEADER = (
        "attempts: 1\n"
        "vars:\n"
        '  base_url: "{{base_url}}"\n'
        "mail:\n"
        "  provider: voidash\n"
        '  base: "{{base_url}}/api/voidash"\n'
        "  defer: true\n"
        "  subject_contains: verification\n"
        "  timeout: 30\n"
    )

    def _deferred_home(self, label: str) -> Path:
        home = self.tmp / f"deferred-{label}"
        proc = run_cli(home, ["init"])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return home

    def _poison_voidash(self, home: Path) -> None:
        import yaml
        path = home / ".autoreg" / "config.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        mail = data.setdefault("mail", {})
        providers = mail.setdefault("providers", {})
        providers["voidash"] = {"base": "http://127.0.0.1:1",
                                "headers": {"Authorization": "Bearer wrong-current-config"}}
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def _deliver_newer_code(self, email: str) -> None:
        """The confirmation mail lands BEFORE verify runs; it is the new code."""
        with mock_site.STATE_LOCK:
            mock_site.CODES[email] = ("654321", __import__("time").time())
            messages = mock_site.MAILBOXES.get(email, [])
            fresh = [m for m in messages if not str(m["id"]).startswith("old-")]
            self.assertEqual(len(fresh), 1, messages)
            fresh[0]["body"] = "Your code is 654321. It expires in 10 minutes."
            fresh[0]["subject"] = "Your verification code"

    def _assert_verified(self, home: Path, adapter: Path, base_url: str) -> None:
        proc = run_cli(home, ["accounts", "list", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = parse_json(proc)
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["status"], "pending")
        proc = run_cli(home, ["verify", str(adapter), "--account", str(row["id"]),
                              "--engine", ENGINE, "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        result = parse_json(proc)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "verified")
        with mock_site.STATE_LOCK:
            account = mock_site.ACCOUNTS.get(row["email"])
        self.assertIsNotNone(account, "mock account was not created")
        self.assertTrue(base_url)

    def test_8_deferred_token_mail_protocol(self) -> None:
        home = self._deferred_home("protocol")
        adapter = home / "deferred-token-protocol.yaml"
        adapter.write_text(
            "name: deferred-token-protocol\n"
            "mode: protocol\n"
            + self._DEFERRED_HEADER +
            "steps:\n"
            "  - request:\n"
            "      method: POST\n"
            '      url: "{{base_url}}/api/send-code"\n'
            "      json:\n"
            '        email: "{{email}}"\n'
            "      expect: 200\n"
            "verify:\n"
            "  - otp:\n"
            "      timeout: 5\n"
            "      interval: 0.1\n"
            "  - request:\n"
            "      method: POST\n"
            '      url: "{{base_url}}/api/verify"\n'
            "      json:\n"
            '        email: "{{email}}"\n'
            '        password: "{{password}}"\n'
            '        code: "{{otp}}"\n'
            '        token: "mock-token"\n'
            "      expect: 200\n"
            "  - request:\n"
            '      url: "{{base_url}}/welcome"\n'
            "      expect: 200\n",
            encoding="utf-8")
        proc = run_cli(home, ["run", str(adapter), "--count", "1",
                              "--var", f"base_url={self.base_url}", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        summary = parse_json(proc)
        self.assertEqual(summary["pending"], 1, summary)
        email = summary["results"][0]["email"]
        self.assertTrue(email.endswith("@mock.test"), email)

        self._deliver_newer_code(email)
        self._poison_voidash(home)
        self._assert_verified(home, adapter, self.base_url)

    def test_9_deferred_token_mail_browser(self) -> None:
        home = self._deferred_home("browser")
        adapter = home / "deferred-token-browser.yaml"
        adapter.write_text(
            "name: deferred-token-browser\n"
            "mode: browser\n"
            + self._DEFERRED_HEADER +
            "steps:\n"
            '  - goto: "{{base_url}}/signup"\n'
            "  - fill:\n"
            "      target: \"#email\"\n"
            '      value: "{{email}}"\n'
            "  - click: \"#send-code\"\n"
            "  - wait_for: \"#code-sent\"\n"
            "verify:\n"
            '  - goto: "{{base_url}}/signup"\n'
            "  - fill:\n"
            "      target: \"#email\"\n"
            '      value: "{{email}}"\n'
            "  - fill:\n"
            "      target: \"#password\"\n"
            '      value: "{{password}}"\n'
            "  - otp:\n"
            "      target: \"#code\"\n"
            "      timeout: 5\n"
            "      interval: 0.1\n"
            "  - captcha:\n"
            "      provider: static\n"
            "      type: turnstile\n"
            "      token: mock-token\n"
            "      sitekey: mock-sitekey\n"
            "  - click: \"#create\"\n"
            "  - wait_for:\n"
            '      target: "text=Welcome"\n'
            "      timeout: 10\n",
            encoding="utf-8")
        proc = run_cli(home, ["run", str(adapter), "--count", "1",
                              "--engine", ENGINE,
                              "--var", f"base_url={self.base_url}", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-3000:])
        summary = parse_json(proc)
        self.assertEqual(summary["pending"], 1, summary)
        email = summary["results"][0]["email"]

        self._deliver_newer_code(email)
        self._poison_voidash(home)
        self._assert_verified(home, adapter, self.base_url)


if __name__ == "__main__":
    unittest.main()
