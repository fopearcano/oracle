#!/bin/bash
# Double-click in Finder (macOS) to start Oracle. Also works from a Linux terminal.
cd "$(dirname "$0")" || exit 1
python3 oracle.py "$@"
status=$?
if [ $status -ne 0 ]; then read -r -p "Press Enter to close..." _; fi
exit $status
