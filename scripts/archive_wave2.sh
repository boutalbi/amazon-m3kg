#!/usr/bin/env bash
# Archives wave 2: one zip per category, then the global archive.
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
"$PY" archive_categories.py --step categories && "$PY" archive_categories.py --step global
echo "[$(date +%H:%M)] DONE archiving wave 2"
