#!/bin/bash

# ======================================================================= #
# LiveLingo Global Execution Script (Linux/WSL/macOS)
# ======================================================================= #

PROJECT_DIR="/mnt/c/Users/rcopr/LiveLingo/LiveLingo"

cd "$PROJECT_DIR" || {
    echo -e "\033[1;31m[x] Error: Project directory not found ($PROJECT_DIR).\033[0m"
    exit 1
}

python3 main.py "$@"
