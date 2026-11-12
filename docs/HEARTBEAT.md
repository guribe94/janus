# Heartbeat watchdog

`janus watch` runs on the coordinating machine every few minutes. If that machine
sleeps, crashes or loses its network, nothing notices: the watcher simply stops
running and the work sits still. The heartbeat watchdog is a small, separate
process on a second machine that notices the silence and raises an alert.

It is three shell scripts and one test file:

| File | Runs on | What it does |
|---|---|---|
| `heartbeat/beat` | the coordinating machine | records one watcher run and copies the record to the watchdog host |
| `heartbeat/watchdog` | the watchdog host | alerts once when the record goes stale, posts recovery when it returns |
| `heartbeat/install-watchdog` | the watchdog host | installs the systemd user service and 5-minute timer (and `--uninstall`) |
| `tests/test_heartbeat.py` | anywhere | stdlib unittest: stale alerts once, no repeat, recovery once |

## How it works

`beat` appends one line to a heartbeat file:

```
<epoch> <host> <check_exit>
```

`epoch` is the run time in seconds, `host` is the coordinating machine's name and
`check_exit` is the exit code of the check that pass ran. The file is append-only;
the watchdog reads its last line. When a remote is configured, `beat` copies the
whole file to the watchdog host over the existing ssh (`scp`; set
`HEARTBEAT_SCP=rsync` to use rsync). It swallows every error and always exits 0, so
a broken
copy can never break the watcher.

`watchdog` compares the newest beat with the current time. If the age is greater
than `HEARTBEAT_MAX_AGE` (default 900 seconds, 15 minutes) it raises one alert per
outage:

- a post through the board's outbox, when one is configured and present, in the
  board's own line format (`YYYY-MM-DD HH:MM <agent> blocked: <text>`), so the
  next `board.sh sync` on the coordinating machine delivers it; and
- a configurable command (`HEARTBEAT_ALERT_CMD`), for a desktop notification, an
  `ntfy` publish, a webhook, or anything else. No service is hard-coded.

A state file remembers that the current outage was already reported, so later
runs stay quiet. When beats resume, the watchdog posts recovery once
(`<agent> status: heartbeat recovered: ...`) and clears the state. A missing
heartbeat file counts as an outage.

## Install

On the watchdog host, with the repository checked out and ssh access to the
coordinating machine already working:

```sh
HEARTBEAT_FILE="$HOME/.local/state/janus/heartbeat" \
HEARTBEAT_MAX_AGE=900 \
HEARTBEAT_OUTBOX="$HOME/janus/board-outbox/outbox.log" \
HEARTBEAT_ALERT_CMD='notify-send "janus heartbeat" "$1"' \
  heartbeat/install-watchdog
```

The installer writes `janus-heartbeat.service` and `janus-heartbeat.timer` under
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`, writes the configuration to
`${XDG_CONFIG_HOME:-$HOME/.config}/janus/heartbeat.env`, then runs
`systemctl --user daemon-reload` and `systemctl --user enable --now
janus-heartbeat.timer`. Re-running is safe: the units are rewritten, the timer is
already enabled, and values already in the env file are kept when the variable is
not set on the command line.

The timer fires five minutes after boot and every five minutes after that
(`OnBootSec=5min`, `OnUnitActiveSec=5min`, `Persistent=true`).

To remove it:

```sh
heartbeat/install-watchdog --uninstall
```

That disables and deletes the units; the env file is kept.

On a headless host, enable lingering so the user timer runs without a login
session:

```sh
loginctl enable-linger "$USER"
```

## Configuration

`beat` (coordinating machine):

| Variable | Default | Meaning |
|---|---|---|
| `HEARTBEAT_FILE` | `${JANUS_STATE:-.janus}/heartbeat` | local heartbeat file |
| `HEARTBEAT_CHECK_EXIT` | `$1`, else `0` | the check's exit code to record |
| `HEARTBEAT_HOST` | `hostname -s` | host name to record |
| `HEARTBEAT_REMOTE` | unset | ssh target of the watchdog host; unset means local only |
| `HEARTBEAT_REMOTE_PATH` | unset | heartbeat path on the watchdog host (required with `HEARTBEAT_REMOTE`) |
| `HEARTBEAT_SSH` / `HEARTBEAT_SCP` | `ssh` / `scp` | the commands used to copy (`HEARTBEAT_SCP=rsync` works) |
| `HEARTBEAT_SSH_OPTS` | unset | extra options for ssh/scp |

`watchdog` (watchdog host):

| Variable | Default | Meaning |
|---|---|---|
| `HEARTBEAT_FILE` | `$HOME/.local/state/janus/heartbeat` | heartbeat file to read |
| `HEARTBEAT_MAX_AGE` | `900` | seconds before a beat is stale |
| `HEARTBEAT_STATE` | `<HEARTBEAT_FILE>.watchdog` | `0` fresh / `1` alerted |
| `HEARTBEAT_OUTBOX` | unset | board outbox file or its directory; unset means no board post |
| `HEARTBEAT_AGENT` | `heartbeat` | board agent name for the posts |
| `HEARTBEAT_ALERT_CMD` | unset | shell command run on an alert; the message is `$1` and `$HEARTBEAT_MESSAGE` |
| `HEARTBEAT_HOST` | `hostname -s` | name used in messages |

The alert command is a shell command, so it can be anything:

```sh
HEARTBEAT_ALERT_CMD='notify-send "janus heartbeat" "$1"'
HEARTBEAT_ALERT_CMD='ntfy publish my-topic "$1"'
HEARTBEAT_ALERT_CMD='curl -fsS -d "$1" https://example.invalid/hook'
```

## How janus will call `beat`

This round ships the scripts and the tests only; `janus.py` is not changed. In the
improvements round the watcher will call `beat` at the end of each `janus watch`
pass, after the check, with the check's exit code:

```sh
HEARTBEAT_FILE="$state/heartbeat" \
HEARTBEAT_CHECK_EXIT="$check_rc" \
HEARTBEAT_REMOTE="$watchdog_ssh" \
HEARTBEAT_REMOTE_PATH="$watchdog_heartbeat" \
  heartbeat/beat
```

`$state` is the project's state folder (`.janus` by default), so the record lives
beside the rest of the watcher's state. The call is fire-and-forget: `beat` never
fails the pass, and the copy is best-effort. The watchdog host needs only the
heartbeat file and, optionally, a board outbox; it does not need the project.

## Notes

- The age comparison uses epoch seconds, so the two hosts' clocks must agree
  closely enough for the threshold to mean what it says. A few minutes of skew is
  harmless at a 15-minute threshold; a large skew is not.
- The watchdog is deliberately quiet: one alert per outage, one recovery per
  return, exit 0 always. A watchdog that fails its own timer is worse than one
  that says nothing.
- The board post is written to the outbox, not to the board directly. The
  coordinating machine's `board.sh sync` merges it like any other host's post.
