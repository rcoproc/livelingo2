#!/bin/bash
# ======================================================================= #
# LiveLingo — run from this folder (portable; no absolute user path)
# ======================================================================= #
set -e
cd "$(dirname "$0")" || {
    echo -e "\033[1;31m[x] Error: cannot cd to script directory.\033[0m"
    exit 1
}
python3 main.py "$@"
