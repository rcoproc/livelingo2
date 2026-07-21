#!/usr/bin/env python3
"""
LiveLingo — Dependency security & freshness audit
=================================================

Valida bibliotecas Python do projeto quanto a:
  1. Vulnerabilidades conhecidas (CVE / OSV / PyPI Advisory)
     → OWASP Top 10 2021 — A06: Vulnerable and Outdated Components
  2. Pacotes desatualizados (versão instalada vs. última no PyPI)
  3. Conformidade com requirements.txt (pins / ranges)

Ferramentas usadas:
  - pip-audit  → CVEs via OSV + PyPI Advisory Database
  - pip list --outdated → versões novas disponíveis

Uso:
  python scripts/check_deps_security.py
  python scripts/check_deps_security.py --fail-on outdated
  python scripts/check_deps_security.py --json report.json
  python scripts/check_deps_security.py --no-install   # não instala pip-audit

Exit codes:
  0  OK (sem vulnerabilidades; outdated só aviso, a menos que --fail-on outdated)
  1  vulnerabilidades encontradas (ou outdated se --fail-on outdated)
  2  erro de execução / ferramenta indisponível
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths / colors
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"

SEVERITY_ORDER = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "moderate": 2,
    "low": 3,
    "unknown": 4,
}

# Advisories that are historical / mis-scoped for current pins (documented).
# PYSEC-2022-252: temporary PyPI account takeover of deep-translator; malicious
# releases were removed. No fixed version exists; 1.11.4 is current legitimate.
KNOWN_FALSE_POSITIVES: dict[str, str] = {
    "PYSEC-2022-252": (
        "deep-translator historical PyPI takeover; no fixed release; "
        "1.11.4 is latest legitimate package"
    ),
}

# Minimum versions required for known CVEs that affect LiveLingo direct deps.
SECURITY_FLOORS: dict[str, str] = {
    "python-dotenv": "1.2.2",  # CVE-2026-28684
    "requests": "2.33.0",  # CVE-2026-25645
    "urllib3": "2.7.0",  # decompress/DoS chain CVEs
}


def c(color: str, text: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"{color}{text}{RESET}"


def banner(title: str) -> None:
    line = "─" * 64
    print()
    print(c(CYAN, line))
    print(c(BOLD, f"  {title}"))
    print(c(CYAN, line))


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Vuln:
    package: str
    installed: str
    vuln_id: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    fix_versions: list[str] = field(default_factory=list)
    severity: str = "unknown"

    @property
    def cve_ids(self) -> list[str]:
        ids = []
        for a in [self.vuln_id, *self.aliases]:
            if a.upper().startswith("CVE-"):
                ids.append(a.upper())
        return ids


@dataclass
class Outdated:
    name: str
    version: str
    latest: str
    in_requirements: bool = False


@dataclass
class AuditReport:
    generated_at: str
    project_root: str
    python: str
    requirements_file: str
    vulnerabilities: list[dict] = field(default_factory=list)
    outdated: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    owasp: dict = field(default_factory=dict)
    tool_versions: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def run(
    cmd: list[str],
    *,
    timeout: int = 300,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        cwd=str(ROOT),
    )


def python_cmd() -> list[str]:
    return [sys.executable]


def ensure_pip_audit(allow_install: bool) -> tuple[bool, str]:
    """Return (ok, version_or_error)."""
    probe = run([*python_cmd(), "-m", "pip_audit", "--version"], timeout=30)
    if probe.returncode == 0:
        return True, (probe.stdout or probe.stderr).strip()

    if not allow_install:
        return False, "pip-audit não instalado. Rode: pip install pip-audit"

    print(c(YELLOW, "→ instalando pip-audit (ferramenta de auditoria CVE/OSV)…"))
    install = run(
        [*python_cmd(), "-m", "pip", "install", "--quiet", "pip-audit"],
        timeout=180,
    )
    if install.returncode != 0:
        return False, f"falha ao instalar pip-audit:\n{install.stderr}"

    probe = run([*python_cmd(), "-m", "pip_audit", "--version"], timeout=30)
    if probe.returncode != 0:
        return False, "pip-audit instalado mas não executa"
    return True, (probe.stdout or probe.stderr).strip()


# ---------------------------------------------------------------------------
# Requirements parsing (for mapping outdated ↔ declared deps)
# ---------------------------------------------------------------------------

_REQ_NAME = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)",
)


def declared_package_names(req_path: Path) -> set[str]:
    names: set[str] = set()
    if not req_path.is_file():
        return names
    for line in req_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        # strip environment markers
        s = s.split(";", 1)[0].strip()
        m = _REQ_NAME.match(s)
        if m:
            names.add(m.group(1).lower().replace("_", "-"))
    return names


def normalize_pkg(name: str) -> str:
    return name.lower().replace("_", "-")


# ---------------------------------------------------------------------------
# Vulnerability scan (pip-audit)
# ---------------------------------------------------------------------------


def _guess_severity(desc: str, vuln_id: str) -> str:
    """Best-effort severity when advisory doesn't ship CVSS."""
    text = f"{vuln_id} {desc}".lower()
    if any(w in text for w in ("critical", "rce", "remote code", "arbitrary code")):
        return "critical"
    if any(
        w in text
        for w in ("high", "sql injection", "ssrf", "path traversal", "auth bypass")
    ):
        return "high"
    if any(
        w in text
        for w in ("medium", "moderate", "xss", "csrf", "denial of service", "dos")
    ):
        return "medium"
    if any(w in text for w in ("low", "info")):
        return "low"
    return "unknown"


def _parse_pip_audit_json(raw: str) -> tuple[list[Vuln], Optional[str]]:
    """Parse pip-audit JSON stdout into Vuln list."""
    raw = raw.strip()
    if not raw:
        return [], "saída vazia do pip-audit"

    # pip-audit sometimes dumps human errors on stdout when isolation fails
    if not raw.lstrip().startswith(("{", "[")):
        snippet = raw.replace("\n", " ")[:300]
        return [], f"pip-audit não retornou JSON: {snippet}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], f"JSON inválido do pip-audit: {exc}\n{raw[:400]}"

    vulns: list[Vuln] = []

    # pip-audit JSON shapes:
    #   {"dependencies": [{"name", "version", "vulns": [...]}, ...]}
    # or list form in older versions
    deps: list[dict[str, Any]]
    if isinstance(data, dict) and "dependencies" in data:
        deps = data["dependencies"]
    elif isinstance(data, list):
        deps = data
    else:
        deps = []

    for dep in deps:
        name = dep.get("name") or dep.get("package") or "?"
        version = str(dep.get("version") or dep.get("installed_version") or "?")
        for v in dep.get("vulns") or dep.get("vulnerabilities") or []:
            vuln_id = str(v.get("id") or v.get("vuln_id") or "UNKNOWN")
            aliases = [str(a) for a in (v.get("aliases") or [])]
            desc = str(v.get("description") or v.get("summary") or "").strip()
            fix = v.get("fix_versions") or v.get("fixed_versions") or []
            if isinstance(fix, str):
                fix = [fix]
            fix = [str(x) for x in fix]
            sev = str(v.get("severity") or "").lower() or _guess_severity(desc, vuln_id)
            vulns.append(
                Vuln(
                    package=name,
                    installed=version,
                    vuln_id=vuln_id,
                    aliases=aliases,
                    description=desc[:400],
                    fix_versions=fix,
                    severity=sev if sev in SEVERITY_ORDER else "unknown",
                )
            )

    vulns.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.package, x.vuln_id))
    return vulns, None


def _run_pip_audit(extra: list[str]) -> tuple[list[Vuln], Optional[str], str]:
    """
    Execute pip-audit. Returns (vulns, error, mode_label).
    Exit code 1 with valid JSON = vulns found (not an error).
    """
    cmd = [
        *python_cmd(),
        "-m",
        "pip_audit",
        "--format",
        "json",
        "--progress-spinner",
        "off",
        "--desc",
        "on",
        "--aliases",
        "on",
        *extra,
    ]
    try:
        result = run(cmd, timeout=300)
    except subprocess.TimeoutExpired:
        return [], "pip-audit timeout (>300s)", " ".join(extra) or "env"

    mode = (
        "requirements"
        if any(a in ("-r", "--requirement") for a in extra)
        else "environment"
    )
    raw_out = result.stdout or ""
    raw_err = (result.stderr or "").strip()

    vulns, parse_err = _parse_pip_audit_json(raw_out)
    if parse_err is None:
        return vulns, None, mode

    # Real failure (not just "found vulns")
    detail = parse_err
    if raw_err:
        detail = f"{parse_err}\n{raw_err[:400]}"
    if result.returncode not in (0, 1):
        detail = f"pip-audit exit {result.returncode}: {detail}"
    return [], detail, mode


def audit_vulnerabilities(
    req_path: Path,
    *,
    prefer_requirements: bool = False,
) -> tuple[list[Vuln], Optional[str], str]:
    """
    Scan for known CVEs via pip-audit / OSV / PyPI Advisory.

    Strategy (robust on Debian/WSL without python3-venv):
      1. Default: audit the *installed* environment (real attack surface).
      2. Optional: resolve requirements.txt in an isolated env (--from-requirements).
         If isolation fails (missing ensurepip/venv), fall back to environment.

    Returns (vulns, error_message, mode_used).
    """
    notes: list[str] = []

    if prefer_requirements and req_path.is_file():
        vulns, err, mode = _run_pip_audit(["--requirement", str(req_path)])
        if err is None:
            return vulns, None, mode
        notes.append(f"modo requirements falhou ({err.splitlines()[0][:120]})")
        # Common on Ubuntu/WSL: ensurepip missing → temp venv cannot be created
        if "ensurepip" in (err or "") or "virtual environment" in (err or "").lower():
            notes.append("fallback → audit do ambiente instalado")
        else:
            # Try pinned-only path without pip resolve (needs exact == pins)
            vulns2, err2, mode2 = _run_pip_audit(
                ["--requirement", str(req_path), "--no-deps", "--disable-pip"]
            )
            if err2 is None:
                return vulns2, None, mode2 + "+no-deps"
            notes.append("fallback --no-deps também falhou")

    # Primary / fallback: audit what is actually installed under this interpreter
    extra: list[str] = ["--local"]
    vulns, err, mode = _run_pip_audit(extra)
    if err is None:
        # Filter noise: keep vulns for declared project deps + anything with CVE
        # (full env is still reported; filter only if huge global site-packages)
        if notes:
            # surface fallback reason once via stderr-like print is done by caller via mode
            mode = mode + " (fallback)"
        return vulns, None, mode

    if notes:
        joined = " | ".join(notes)
        return [], f"{joined}; env: {err}", mode
    return [], err, mode


# ---------------------------------------------------------------------------
# Outdated packages
# ---------------------------------------------------------------------------


def list_outdated(declared: set[str]) -> tuple[list[Outdated], Optional[str]]:
    result = run(
        [*python_cmd(), "-m", "pip", "list", "--outdated", "--format=json"],
        timeout=180,
    )
    if result.returncode != 0:
        return [], (result.stderr or "pip list --outdated falhou").strip()

    raw = (result.stdout or "").strip() or "[]"
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], f"JSON inválido do pip list: {exc}"

    items: list[Outdated] = []
    for row in rows:
        name = str(row.get("name") or "")
        if not name:
            continue
        items.append(
            Outdated(
                name=name,
                version=str(row.get("version") or "?"),
                latest=str(row.get("latest_version") or row.get("latest") or "?"),
                in_requirements=normalize_pkg(name) in declared,
            )
        )
    # Project deps first, then transitive
    items.sort(key=lambda o: (not o.in_requirements, o.name.lower()))
    return items, None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def filter_ignored_vulns(
    vulns: list[Vuln],
    ignore_ids: set[str],
) -> tuple[list[Vuln], list[Vuln]]:
    """Split into (actionable, ignored). Match vuln_id or any CVE/alias."""
    actionable: list[Vuln] = []
    ignored: list[Vuln] = []
    for v in vulns:
        ids = {v.vuln_id.upper(), *(a.upper() for a in v.aliases)}
        if ids & {i.upper() for i in ignore_ids}:
            ignored.append(v)
        else:
            actionable.append(v)
    return actionable, ignored


def print_vulns(vulns: list[Vuln], ignored: Optional[list[Vuln]] = None) -> None:
    banner("1) Vulnerabilidades (OWASP A06 — Vulnerable & Outdated Components)")
    if ignored:
        print(c(DIM, f"  (ignoradas por allowlist: {len(ignored)})"))
        for v in ignored:
            reason = KNOWN_FALSE_POSITIVES.get(v.vuln_id, "CLI --ignore-vuln")
            print(c(DIM, f"    · {v.package} {v.vuln_id} — {reason}"))
        print()

    if not vulns:
        print(c(GREEN, "  ✓ Nenhuma vulnerabilidade acionável nas deps do projeto"))
        print(c(DIM, "    Fontes: OSV + PyPI Advisory Database (via pip-audit)"))
        return

    print(c(RED, f"  ✗ {len(vulns)} vulnerabilidade(s) encontrada(s)\n"))
    for v in vulns:
        sev = v.severity.upper()
        sev_color = {
            "critical": RED,
            "high": RED,
            "medium": YELLOW,
            "moderate": YELLOW,
            "low": CYAN,
        }.get(v.severity, DIM)
        cves = ", ".join(v.cve_ids) if v.cve_ids else "—"
        fix = ", ".join(v.fix_versions) if v.fix_versions else "sem fix publicado"
        print(f"  {c(sev_color, f'[{sev}]')} {c(BOLD, v.package)}=={v.installed}")
        print(f"         ID: {v.vuln_id}  |  CVE: {cves}")
        print(f"         Fix: {fix}")
        if v.description:
            desc = v.description.replace("\n", " ")
            if len(desc) > 120:
                desc = desc[:117] + "..."
            print(f"         {c(DIM, desc)}")
        print()


def print_outdated(items: list[Outdated]) -> None:
    banner("2) Pacotes desatualizados (instalados vs. PyPI)")
    if not items:
        print(c(GREEN, "  ✓ Tudo na última versão disponível no PyPI"))
        return

    proj = [o for o in items if o.in_requirements]
    other = [o for o in items if not o.in_requirements]

    print(c(YELLOW, f"  ! {len(items)} pacote(s) com versão mais nova no PyPI"))
    print(
        c(
            DIM,
            "    (aviso de frescor — NÃO é CVE; exit 0 com --fail-on vuln)",
        )
    )
    print(
        c(
            DIM,
            f"    (diretos no requirements: {len(proj)} | transitivos/outros: {len(other)})\n",
        )
    )

    def row(o: Outdated) -> None:
        tag = c(CYAN, "req") if o.in_requirements else c(DIM, "env")
        print(f"  [{tag}] {o.name:28} {o.version:12} → {c(GREEN, o.latest)}")

    if proj:
        print(c(BOLD, "  Dependências do projeto:"))
        for o in proj:
            row(o)
        print()
    if other:
        print(c(BOLD, "  Outros no ambiente (transitivos / dev):"))
        for o in other[:30]:
            row(o)
        if len(other) > 30:
            print(c(DIM, f"  … +{len(other) - 30} omitidos"))
        print()


def print_owasp_summary(
    vulns: list[Vuln],
    outdated: list[Outdated],
    *,
    vuln_scan_ok: bool,
    outdated_scan_ok: bool = True,
) -> None:
    banner("3) Resumo OWASP / recomendações")
    a06_fail = bool(vulns) or not vuln_scan_ok
    a06_warn = bool(outdated)

    status = (
        c(RED, "FAIL")
        if a06_fail
        else (c(YELLOW, "WARN (só frescor)") if a06_warn else c(GREEN, "PASS"))
    )
    print(f"  A06:2021 Vulnerable and Outdated Components  →  {status}")
    if a06_warn and not a06_fail:
        print(
            c(
                DIM,
                "  (WARN = há versões mais novas no PyPI; sem CVE acionável → deploy OK)",
            )
        )
    print()
    print("  Checklist alinhado OWASP Dependency Checking:")

    # status: "pass" | "warn" | "fail"
    # "monitorados" = inventário/scan rodou (não exige zero outdated)
    rows: list[tuple[str, str]] = [
        (
            "Inventário de componentes (requirements.txt)",
            "pass" if REQUIREMENTS.is_file() else "fail",
        ),
        (
            "Scan de CVEs conhecidos (pip-audit / OSV)",
            "fail" if (not vuln_scan_ok or vulns) else "pass",
        ),
        (
            "Componentes desatualizados monitorados (scan PyPI)",
            "pass" if outdated_scan_ok else "fail",
        ),
        (
            "Deps do projeto na última versão do PyPI",
            "pass" if not outdated else "warn",
        ),
        (
            "Sem CVE acionável (ou com fix publicado)",
            "fail"
            if (not vuln_scan_ok or (vulns and not all(v.fix_versions for v in vulns)))
            else "pass",
        ),
    ]
    for label, st in rows:
        if st == "pass":
            mark = c(GREEN, "✓")
        elif st == "warn":
            mark = c(YELLOW, "!")
        else:
            mark = c(RED, "✗")
        print(f"    {mark}  {label}")

    print()
    if not vuln_scan_ok:
        print(c(RED, "  Scan de vulnerabilidades NÃO completou — trate como falha."))
        print(
            "    Dica WSL/Ubuntu: use o venv do projeto ou `sudo apt install python3-venv`"
        )
        print("    Ex.:  .venv/bin/python scripts/check_deps_security.py")
    elif vulns:
        print(c(BOLD, "  Ação prioritária:"))
        print("    1. Atualize pacotes vulneráveis para as versões em Fix acima")
        print("    2. Rode de novo este script até zerar vulnerabilidades")
        print("    3. Commit do requirements.txt atualizado")
    elif outdated:
        print(c(BOLD, "  Ação opcional (não bloqueia produção):"))
        print("    • Avalie upgrades de deps diretas (coluna [req]) quando tiver tempo")
        print(
            "    • Teste áudio/STT após bump (sounddevice, soundfile, edge-tts, …)"
        )
        print(
            c(
                DIM,
                "    • Para falhar o CI se houver outdated:  --fail-on outdated  ou  --fail-on any",
            )
        )
    else:
        print(c(GREEN, "  Nenhuma ação de segurança pendente nas dependências."))

def build_report(
    vulns: list[Vuln],
    outdated: list[Outdated],
    tool_versions: dict[str, str],
) -> AuditReport:
    by_sev: dict[str, int] = {}
    for v in vulns:
        by_sev[v.severity] = by_sev.get(v.severity, 0) + 1

    return AuditReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        project_root=str(ROOT),
        python=sys.version.split()[0],
        requirements_file=str(REQUIREMENTS) if REQUIREMENTS.is_file() else "",
        vulnerabilities=[asdict(v) for v in vulns],
        outdated=[asdict(o) for o in outdated],
        summary={
            "vulnerability_count": len(vulns),
            "outdated_count": len(outdated),
            "outdated_project_deps": sum(1 for o in outdated if o.in_requirements),
            "by_severity": by_sev,
        },
        owasp={
            "A06_2021": {
                "name": "Vulnerable and Outdated Components",
                "status": "fail" if vulns else ("warn" if outdated else "pass"),
                "vulnerabilities": len(vulns),
                "outdated_components": len(outdated),
            }
        },
        tool_versions=tool_versions,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Audita dependências Python: CVEs (OWASP A06) + pacotes desatualizados.",
    )
    p.add_argument(
        "--requirements",
        type=Path,
        default=REQUIREMENTS,
        help=f"Arquivo de requirements (default: {REQUIREMENTS})",
    )
    p.add_argument(
        "--json",
        type=Path,
        metavar="FILE",
        help="Grava relatório completo em JSON",
    )
    p.add_argument(
        "--fail-on",
        choices=("vuln", "outdated", "any"),
        default="vuln",
        help="Quando sair com código 1 (default: vuln)",
    )
    p.add_argument(
        "--no-install",
        action="store_true",
        help="Não instala pip-audit automaticamente se ausente",
    )
    p.add_argument(
        "--skip-outdated",
        action="store_true",
        help="Pula checagem de versões desatualizadas",
    )
    p.add_argument(
        "--skip-vuln",
        action="store_true",
        help="Pula scan de vulnerabilidades",
    )
    p.add_argument(
        "--from-requirements",
        action="store_true",
        help=(
            "Tenta resolver requirements.txt em ambiente isolado (precisa python3-venv). "
            "Se falhar, faz fallback para o ambiente instalado."
        ),
    )
    p.add_argument(
        "--project-only",
        action="store_true",
        help="No relatório de vulns/outdated, destaca só pacotes do requirements.txt",
    )
    p.add_argument(
        "--ignore-vuln",
        action="append",
        default=[],
        metavar="ID",
        help="Ignora advisory (CVE/PYSEC/GHSA). Pode repetir. Defaults incluem KNOWN_FALSE_POSITIVES.",
    )
    p.add_argument(
        "--no-default-ignores",
        action="store_true",
        help="Não aplica KNOWN_FALSE_POSITIVES (só --ignore-vuln explícitos)",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    req_path: Path = args.requirements.resolve()
    declared = declared_package_names(req_path)

    print(c(BOLD, "LiveLingo · Dependency Security Audit"))
    print(c(DIM, f"  root: {ROOT}"))
    print(c(DIM, f"  python: {sys.executable} ({sys.version.split()[0]})"))
    print(
        c(
            DIM,
            f"  requirements: {req_path if req_path.is_file() else '(ausente — auditando env)'}",
        )
    )
    print(c(DIM, f"  OWASP foco: A06:2021 Vulnerable and Outdated Components"))

    tool_versions: dict[str, str] = {
        "python": sys.version.split()[0],
        "pip": "?",
    }
    pip_v = run([*python_cmd(), "-m", "pip", "--version"], timeout=30)
    if pip_v.returncode == 0:
        tool_versions["pip"] = (pip_v.stdout or "").strip()

    vulns: list[Vuln] = []
    outdated: list[Outdated] = []
    errors: list[str] = []
    vuln_scan_ok = args.skip_vuln  # skipped = not a failure of the scan itself
    outdated_scan_ok = args.skip_outdated
    audit_mode = ""

    # --- Vulnerabilities ---
    if not args.skip_vuln:
        ok, info = ensure_pip_audit(allow_install=not args.no_install)
        if not ok:
            print(c(RED, f"\n[erro] {info}"))
            return 2
        tool_versions["pip-audit"] = info
        print(c(DIM, f"  pip-audit: {info}"))

        vulns, err, audit_mode = audit_vulnerabilities(
            req_path,
            prefer_requirements=args.from_requirements,
        )
        if err:
            vuln_scan_ok = False
            errors.append(err)
            print(c(RED, f"\n[erro] scan de vulnerabilidades: {err}"))
        else:
            vuln_scan_ok = True
            tool_versions["audit_mode"] = audit_mode
            print(c(DIM, f"  modo audit: {audit_mode}"))
            if args.project_only and declared:
                # Keep declared deps + security-floor transitive (urllib3, etc.)
                keep = set(declared) | {normalize_pkg(n) for n in SECURITY_FLOORS}
                vulns = [v for v in vulns if normalize_pkg(v.package) in keep]

            ignore_ids: set[str] = set(args.ignore_vuln or [])
            if not args.no_default_ignores:
                ignore_ids |= set(KNOWN_FALSE_POSITIVES)
            vulns, ignored = filter_ignored_vulns(vulns, ignore_ids)
            print_vulns(vulns, ignored=ignored)
    else:
        print(c(DIM, "\n  (scan de vulnerabilidades pulado)"))

    # --- Outdated ---
    if not args.skip_outdated:
        outdated, err = list_outdated(declared)
        if err:
            outdated_scan_ok = False
            errors.append(err)
            print(c(RED, f"\n[erro] checagem outdated: {err}"))
        else:
            outdated_scan_ok = True
            if args.project_only:
                outdated = [o for o in outdated if o.in_requirements]
            print_outdated(outdated)
    else:
        print(c(DIM, "\n  (checagem outdated pulada)"))

    if not args.skip_vuln or not args.skip_outdated:
        print_owasp_summary(
            vulns,
            outdated,
            vuln_scan_ok=vuln_scan_ok,
            outdated_scan_ok=outdated_scan_ok,
        )
    report = build_report(vulns, outdated, tool_versions)
    report.summary["vuln_scan_ok"] = vuln_scan_ok
    report.summary["audit_mode"] = audit_mode

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(asdict(report), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(c(DIM, f"\n  relatório JSON: {args.json}"))

    # Footer summary line
    banner("Resultado")
    print(f"  Vulnerabilidades : {c(RED if vulns else GREEN, str(len(vulns)))}")
    print(
        f"  Desatualizados   : {c(YELLOW if outdated else GREEN, str(len(outdated)))}"
    )
    if errors:
        print(f"  Erros de tool    : {c(RED, str(len(errors)))}")

    # Failed vulnerability scan is always a hard error (cannot claim "secure")
    if not vuln_scan_ok and not args.skip_vuln:
        print(c(RED, "\n  EXIT 2 — scan de vulnerabilidades incompleto"))
        return 2

    fail = False
    if args.fail_on == "vuln" and vulns:
        fail = True
    elif args.fail_on == "outdated" and (vulns or outdated):
        fail = True
    elif args.fail_on == "any" and (vulns or outdated):
        fail = True

    if fail:
        print(c(RED, "\n  EXIT 1 — correção necessária antes de merge/deploy"))
        return 1

    print(c(GREEN, "\n  EXIT 0 — deps OK para o critério selecionado"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nabortado.", file=sys.stderr)
        raise SystemExit(130)
