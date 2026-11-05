#!/bin/sh

PATH=/bin:/usr/bin
export PATH
[ -z "$TEST_TMUX" ] && TEST_TMUX=$(readlink -f ../tmux)
command -v python3 >/dev/null || exit 101
exec python3 -B ./program_status_test.py "$TEST_TMUX" "${1:-all}"
