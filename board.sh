#!/bin/sh
# board.sh: the message board agents post to. A thin shim over the board's Python module (janus/msgboard.py), kept as
# this exact path because it is what launchers put in every agent prompt and ship to every other host. It stays a shell
# file (a launcher ships its text and runs it); the commands and the board files (log.md, agents/, to/, questions/, the
# flock) are unchanged: see msgboard.py's header for the contract. Runs from a checkout/release (<dir>/janus/msgboard.py)
# or from a remote host's <root>/bin (board.sh and msgboard.py next to each other).
d=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
m=$d/msgboard.py
[ -f "$m" ] || m=$d/janus/msgboard.py
exec python3 "$m" "$d/board.sh" "$@"
