@echo off
:: =======================================================================
:: LiveLingo — run from this folder (portable; no absolute user path)
:: =======================================================================
cd /d "%~dp0"
python main.py %*
