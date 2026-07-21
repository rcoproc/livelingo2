#!/usr/bin/env bash
# ======================================================================= #
# LiveLingo — checks WSL/Linux (format → security → tests)
# ======================================================================= #
# Uso (na raiz do projeto ou de qualquer lugar):
#   bash scripts/run_checks.sh
#   ./scripts/run_checks.sh
#   ./scripts/run_checks.sh --skip-format
#   ./scripts/run_checks.sh --fail-on any
#
# Requer: python3
# Opcional: ruff (preferido) ou black / isort  →  pip install -r requirements-dev.txt
# ======================================================================= #

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
SKIP_FORMAT=0
SECURITY_EXTRA=()
PYTEST_EXTRA=()

usage() {
  cat <<'EOF'
Uso: scripts/run_checks.sh [opções]

  --skip-format     Não formata código
  --fail-on MODE    Passa para o audit (vuln|outdated|any)  [default: vuln]
  --project-only    Audit só deps do requirements (default: ligado)
  --no-project-only Audit do ambiente inteiro
  --pytest ARG      Extra args pro pytest (repetível)
  -h, --help        Esta ajuda

Ordem:
  1) format (ruff format | black | isort, o que existir)
  2) segurança  →  python3 scripts/check_deps_security.py
  3) testes     →  python3 -m pytest tests/
EOF
}

PROJECT_ONLY=1
FAIL_ON="vuln"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-format) SKIP_FORMAT=1; shift ;;
    --fail-on)
      FAIL_ON="${2:?--fail-on precisa de valor (vuln|outdated|any)}"
      shift 2
      ;;
    --project-only) PROJECT_ONLY=1; shift ;;
    --no-project-only) PROJECT_ONLY=0; shift ;;
    --pytest)
      PYTEST_EXTRA+=("${2:?}")
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Opção desconhecida: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# --- cores (só se TTY) ---
if [[ -t 1 ]]; then
  C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
  C_CYAN=; C_GREEN=; C_YELLOW=; C_RED=; C_BOLD=; C_DIM=; C_RESET=
fi

step() { echo; echo "${C_CYAN}${C_BOLD}══▶ $*${C_RESET}"; }
ok()   { echo "${C_GREEN}✓ $*${C_RESET}"; }
warn() { echo "${C_YELLOW}! $*${C_RESET}"; }
die()  { echo "${C_RED}✗ $*${C_RESET}" >&2; exit 1; }

echo "${C_BOLD}LiveLingo · run_checks (WSL)${C_RESET}"
echo "${C_DIM}  root:    $ROOT${C_RESET}"
echo "${C_DIM}  python:  $($PYTHON --version 2>&1)  ($PYTHON)${C_RESET}"

command -v "$PYTHON" >/dev/null 2>&1 || die "python3 não encontrado no PATH"

# Paths de código do projeto (não formata .venv / cache)
FORMAT_PATHS=(
  main.py
  config.py
  list_devices.py
  dev_reload.py
  livelingo
  scripts
  tests
)

# -----------------------------------------------------------------------
# 1) Format
# -----------------------------------------------------------------------
if [[ "$SKIP_FORMAT" -eq 1 ]]; then
  step "1/3 Format — pulado (--skip-format)"
else
  step "1/3 Format código Python"
  FORMATTED=0

  # Preferência: ruff (rápido, format + import sort moderno)
  if "$PYTHON" -m ruff --version >/dev/null 2>&1; then
    echo "  usando: ruff format + ruff check --fix (imports/lint seguro)"
    "$PYTHON" -m ruff format "${FORMAT_PATHS[@]}"
    # Só regras de auto-fix seguras (imports); não reescreve lógica
    "$PYTHON" -m ruff check --select I --fix "${FORMAT_PATHS[@]}" || true
    FORMATTED=1
    ok "ruff concluído"
  elif "$PYTHON" -m black --version >/dev/null 2>&1; then
    echo "  usando: black"
    "$PYTHON" -m black "${FORMAT_PATHS[@]}"
    FORMATTED=1
    ok "black concluído"
    if "$PYTHON" -m isort --version >/dev/null 2>&1; then
      echo "  + isort"
      "$PYTHON" -m isort "${FORMAT_PATHS[@]}"
      ok "isort concluído"
    fi
  elif command -v black >/dev/null 2>&1; then
    echo "  usando: black (PATH)"
    black "${FORMAT_PATHS[@]}"
    FORMATTED=1
    ok "black concluído"
  fi

  if [[ "$FORMATTED" -eq 0 ]]; then
    warn "Nenhum formatter instalado (ruff/black)."
    warn "Instale:  $PYTHON -m pip install -r requirements-dev.txt"
    warn "Seguindo sem formatar…"
  fi
fi

# -----------------------------------------------------------------------
# 2) Security
# -----------------------------------------------------------------------
step "2/3 Segurança (OWASP A06 / pip-audit)"
SEC=(scripts/check_deps_security.py --fail-on "$FAIL_ON")
if [[ "$PROJECT_ONLY" -eq 1 ]]; then
  SEC+=(--project-only)
fi
# shellcheck disable=SC2068
"$PYTHON" "${SEC[@]}"
ok "audit de dependências OK (critério: --fail-on $FAIL_ON)"

# -----------------------------------------------------------------------
# 3) Tests
# -----------------------------------------------------------------------
step "3/3 Testes (pytest)"
if ! "$PYTHON" -m pytest --version >/dev/null 2>&1; then
  die "pytest não instalado. Rode: $PYTHON -m pip install -r requirements-dev.txt"
fi

# -q enxuto; -v se quiser detalhe via --pytest -v
"$PYTHON" -m pytest tests/ -q --tb=short "${PYTEST_EXTRA[@]+"${PYTEST_EXTRA[@]}"}"
ok "testes OK"

echo
echo "${C_GREEN}${C_BOLD}══▶ Todos os checks passaram${C_RESET}"
echo "${C_DIM}  format → security → tests${C_RESET}"
exit 0
