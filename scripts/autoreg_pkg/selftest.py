"""End-to-end smoke test against the bundled mock site."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import __version__

SKILL_DIR = Path(__file__).resolve().parents[2]
LAUNCHER = SKILL_DIR / "scripts" / "autoreg.py"


def _run(home: Path, args: list[str], timeout: float = 240) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(LAUNCHER), "--home", str(home)] + args
    print("$ " + " ".join(command))
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _json(proc: subprocess.CompletedProcess[str]) -> Any:
    try:
        return json.loads(proc.stdout)
    except Exception:
        return None


def run_selftest(home: str | Path | None, *, engine: str = "patchright",
                 keep: bool = False) -> int:
    import mock_site  # sibling module

    server, _, base_url = mock_site.start()
    tmp = Path(home) if home else Path(tempfile.mkdtemp(prefix="autoreg-selftest-"))
    tmp.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    account_id: int | None = None
    profile_id: str | None = None
    try:
        print(f"autoreg {__version__} selftest; engine={engine}; home={tmp}; mock={base_url}")
        proc = _run(tmp, ["init"])
        if proc.returncode != 0:
            failures.append("init: " + proc.stderr.strip())
        proc = _run(tmp, ["doctor", "--json"])
        data = _json(proc)
        if proc.returncode != 0:
            failures.append("doctor: " + (proc.stderr.strip() or proc.stdout.strip()))
        elif not isinstance(data, dict):
            failures.append("doctor: non-JSON output")

        proc = _run(tmp, ["run", "mock.yaml", "--count", "1", "--engine", engine,
                          "--var", f"base_url={base_url}", "--json"])
        data = _json(proc)
        if proc.returncode != 0 or not isinstance(data, dict) or data.get("failed"):
            failures.append("run: " + (proc.stderr.strip() or proc.stdout.strip()[-1500:]))
        else:
            results = data.get("results") or []
            if results:
                account_id = results[0].get("account_id")
                profile_id = results[0].get("profile_id")
            if not account_id:
                failures.append("run: no account_id in result")

        if account_id:
            proc = _run(tmp, ["verify", "mock.yaml", "--account", str(account_id),
                              "--engine", engine, "--json"])
            data = _json(proc)
            if proc.returncode != 0 or not isinstance(data, dict) or not data.get("ok"):
                failures.append("verify: " + (proc.stderr.strip() or proc.stdout.strip()[-1000:]))

        if profile_id:
            proc = _run(tmp, ["profile", "fpcheck", profile_id, "--engine", engine, "--json"])
            data = _json(proc)
            if proc.returncode != 0 or not isinstance(data, dict) or data.get("mismatches"):
                failures.append("fpcheck: " + (proc.stderr.strip() or proc.stdout.strip()[-1000:]))

        proc = _run(tmp, ["accounts", "list", "--json"])
        data = _json(proc)
        if proc.returncode != 0 or not isinstance(data, list) or not data:
            failures.append("accounts list: " + (proc.stderr.strip() or proc.stdout.strip()[-800:]))

        if failures:
            print("\nFAIL")
            for item in failures:
                print(" - " + item)
            print(f"\nartifacts kept at {tmp}")
            return 1
        print("\nPASS: signup, verify, fingerprint check and account store all work")
        return 0
    finally:
        mock_site.stop(server)
        if not keep and not failures:
            shutil.rmtree(tmp, ignore_errors=True)
