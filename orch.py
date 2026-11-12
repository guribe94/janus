#!/usr/bin/env python3
"""orch: thin glue over beads (bd), pueue, tmux, herdr, ssh, rsync and git. Python 3.11+, standard library only.
Reads orch.toml from the current directory or a parent. The code is the orchkit package next to this file (docs/DESIGN.md)."""
import sys
from orchkit.cli import main

if __name__ == "__main__": sys.exit(main())
