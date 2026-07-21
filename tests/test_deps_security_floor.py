"""
Guardrails: installed packages must meet security floors (CVE fixes).

Fails CI/production readiness if someone re-installs vulnerable pins.
"""

from __future__ import annotations

import importlib.metadata as md
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"

# Keep in sync with scripts/check_deps_security.py SECURITY_FLOORS
SECURITY_FLOORS = {
    "python-dotenv": "1.2.2",  # CVE-2026-28684
    "requests": "2.33.0",  # CVE-2026-25645
    "urllib3": "2.7.0",  # decompress / DoS advisories
}


def _parse_version(v: str) -> tuple:
    """Loose numeric version tuple for comparison (ignores post/dev tags)."""
    parts = re.findall(r"\d+", v.split("+")[0].split("!")[0])
    return tuple(int(p) for p in parts) if parts else (0,)


def _version_at_least(installed: str, minimum: str) -> bool:
    return _parse_version(installed) >= _parse_version(minimum)


@pytest.mark.parametrize("package,minimum", sorted(SECURITY_FLOORS.items()))
def test_security_floor_installed(package: str, minimum: str):
    try:
        installed = md.version(package)
    except md.PackageNotFoundError:
        pytest.fail(f"{package} not installed — required (min {minimum})")
    assert _version_at_least(installed, minimum), (
        f"{package}=={installed} is below security floor {minimum}. "
        f"Run: pip install -U '{package}>={minimum}'"
    )


def test_requirements_declare_security_floors():
    text = REQUIREMENTS.read_text(encoding="utf-8")
    assert "python-dotenv" in text
    assert "requests" in text
    assert "urllib3" in text
    # Floor markers present so future edits don't silently drop them
    assert "1.2.2" in text or ">=1.2.2" in text
    assert "2.33" in text or ">=2.33" in text
    assert "2.7" in text or ">=2.7" in text


def test_dotenv_load_api_still_works(tmp_path, monkeypatch):
    """load_dotenv must still populate env after upgrade (app uses only this API)."""
    from dotenv import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("LIVELINGO_SECURITY_TEST_KEY=ok123\n", encoding="utf-8")
    monkeypatch.delenv("LIVELINGO_SECURITY_TEST_KEY", raising=False)
    assert load_dotenv(env_file) is True
    import os

    assert os.getenv("LIVELINGO_SECURITY_TEST_KEY") == "ok123"


def test_requests_session_basic():
    """requests.Session still constructs after security bump (used by llm/STT)."""
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": "LiveLingo-test"})
    assert s.headers["User-Agent"] == "LiveLingo-test"
    s.close()
