#!/bin/sh
# sandbox/linux.sh - run a command under bwrap: the root read-only, only the agent's paths read-write.
#
#   linux.sh <worktree> <outdir> -- <cmd...>
#
# The same policy as sandbox/run on macOS: reads are allowed broadly except the deny list; writes are allowed
# only in the worktree, the output folder, the temp dir and the caches; the network stays open. run exports
# the two lists below (canonical, ':'-separated); when linux.sh is run on its own it falls back to the
# worktree, the output folder and the temp dir. It refuses, with a clear message, when bwrap is missing.
set -eu
set -f

prog=${0##*/}
die() { printf '%s: %s\n' "$prog" "$*" >&2; exit 2; }

if [ $# -lt 3 ]; then die "usage: $prog <worktree> <outdir> -- <cmd...>"; fi
worktree=$1 outdir=$2
shift 2
if [ "${1:-}" != "--" ]; then die "usage: $prog <worktree> <outdir> -- <cmd...>"; fi
shift
if [ $# -lt 1 ]; then die "no command after --"; fi

command -v bwrap >/dev/null 2>&1 || die "bwrap not found: install bubblewrap (the sandbox refuses to run the command unsandboxed)"

canon() {
  _p=$1
  case $_p in /*) ;; *) _p=$PWD/$_p ;; esac
  while [ "$_p" != "/" ] && [ "${_p%/}" != "$_p" ]; do _p=${_p%/}; done
  _head=$_p _tail=
  while [ "$_head" != "/" ] && [ ! -d "$_head" ]; do   # walk up to the deepest existing directory
    _tail=/${_head##*/}$_tail
    _head=${_head%/*}
    [ -n "$_head" ] || _head=/
  done
  _root=$(CDPATH= cd -- "$_head" 2>/dev/null && pwd -P) || _root=$_head
  case $_root in
    /) printf '/%s\n' "${_tail#/}" ;;
    *) printf '%s%s\n' "$_root" "$_tail" ;;
  esac
}

if [ -n "${SANDBOX_WRITE_PATHS:-}" ]; then
  write_paths=$SANDBOX_WRITE_PATHS
else
  write_paths=$(canon "$worktree"):$(canon "$outdir"):$(canon "${TMPDIR:-/tmp}")
fi
deny_paths=${SANDBOX_DENY_PATHS:-}

# Build bwrap's options NUL-separated and hand them over with --args, so paths with spaces or quotes survive
# without eval. The file is unlinked before bwrap starts; the open fd 3 keeps the data readable.
opts=$(mktemp "${TMPDIR:-/tmp}/janus-sandbox-args.XXXXXX") || die "mktemp failed"
exec 3<"$opts" 4>"$opts"     # 3 reads for bwrap, 4 writes the options: a descriptor opened to read cannot be written
rm -f "$opts"
{
  printf '%s\0' --die-with-parent --dev /dev --ro-bind / /
  _ifs=$IFS; IFS=:
  for _p in $write_paths; do
    [ -n "$_p" ] || continue
    printf '%s\0' --bind-try "$_p" "$_p"     # read-write where the agent works; missing paths are skipped
  done
  for _p in $deny_paths; do
    [ -n "$_p" ] || continue
    if [ -d "$_p" ]; then printf '%s\0' --tmpfs "$_p"            # hide a denied directory behind an empty one
    elif [ -e "$_p" ]; then printf '%s\0' --ro-bind /dev/null "$_p"  # hide a denied file behind /dev/null
    fi
  done
  IFS=$_ifs
  printf '%s\0' --chdir "$worktree"
} >&4
exec 4>&-

exec bwrap --args 3 -- "$@"
