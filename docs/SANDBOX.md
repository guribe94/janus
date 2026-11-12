# Sandbox

`sandbox/run` runs an agent's command with a write limit: the command can write only where that agent works,
and it cannot read the keychain or a list of denied paths. It is **optional and not wired into launches**: no janus
launch, job or watcher calls it, so nothing enforces the owner's rule that an agent may write only its worktree and
its output folder, and may not read the keychain or another repository (owner ruling: the sandbox is optional and
unwired). An agent, a launcher or you can wrap a command with it by hand: it is built and tested, and it is the
intended enforcement for the trust model, but it is not the enforcement in use.

## What it is

    sandbox/run <worktree> <outdir> -- <cmd...>

`run` canonicalises every path (the profiles match real paths, so a symlinked prefix such as `/var` must be
resolved), builds the allow and deny lists, picks the profile for the OS, renders it and runs the command:

| OS | mechanism | files |
|---|---|---|
| macOS | `sandbox-exec` with a rendered SBPL profile | `run` renders `sandbox/macos.sb.in` and execs `sandbox-exec -p` |
| Linux | `bwrap` (bubblewrap) | `run` hands the lists to `sandbox/linux.sh`, which binds the root read-only and the agent's paths read-write |

Where the tool is missing (`bwrap` on Linux, `sandbox-exec` on macOS) the runner refuses with a clear message:
it never runs the command unsandboxed. Paths are quoted, and nothing reaches a profile without escaping.

## Allowed

- **Reads:** everything, so an agent can read its worktree, its brief, the skill, the system and its toolchains.
- **Writes, and only these:**
  - the agent's worktree (arg 1) and its output folder (arg 2);
  - the temp dir (`$TMPDIR`, default `/tmp`);
  - the tool caches an agent needs: `~/.cargo/registry`, uv (`~/.cache/uv`, `~/Library/Caches/uv`), `~/.npm`;
  - the runtime's own state dir (`~/.claude`, `~/.codex`);
  - the standard streams (`/dev/null`, `/dev/stdout`, `/dev/stderr`, `/dev/tty`, `/dev/fd`);
  - anything listed in `SANDBOX_WRITE` (colon-separated), for a cache this list does not know.
- **The network** (see Limits).

## Denied

- Reads of the macOS keychain dirs: `~/Library/Keychains` and `/Library/Keychains`.
- Reads of anything in `SANDBOX_DENY` (colon-separated). This is where janus passes the other repositories and
  any secret paths an agent must not read. On macOS the deny is a `deny file-read*` after the broad allow, so
  the read fails; on Linux a denied directory is hidden behind an empty `tmpfs` and a denied file behind
  `/dev/null`, so its content reads as empty.
- Every write outside the allowed list: the worktree's siblings, `$HOME`, other repositories, the system.

The write list is what makes "only its worktree and its output folder" true; the deny list is what makes
"cannot read the keychain or another repository" true. Both are per run, so two agents on the same machine get
different allow lists from the same code.

## How janus would call it

Nothing in janus calls `sandbox/run` today (see the top). If it were wired in, this is the interface:
janus would pass the unit's worktree and output folder, and `SANDBOX_DENY` with the other repositories (and any
secret paths) the agent must not read:

    sandbox/run <unit-worktree> <out/<unit>> -- <agent command...>

## Limits (known, not fixed here)

- **The network stays open:** the sandbox bounds reads and writes, not what a command can send or fetch.
- **Agents still act as the user for the paths they may write:** inside the worktree and the caches a command
  has the user's full rights (create, delete, replace files there).
- **Not a process jail:** a shell and its children run normally; only their file access is bounded.
- The macOS profile is passed to `sandbox-exec` as text (`-p`); a deny list large enough to exceed the argument
  limit would need the file form (`-f`).
- The Linux path is not exercised by this repo's tests on a Mac (there is no `bwrap` there); `sandbox/linux.sh`
  refuses clearly when it is absent.

## Tests

`tests/test_sandbox.py` runs a command through `sandbox/run` and checks that a write in the worktree and in the
output folder succeeds while a write in a sibling folder or in `$HOME` fails and a file named in the deny list
cannot be read. It skips, with the reason, where the OS tool is missing. Run it with
`python3 -m unittest tests.test_sandbox`.
