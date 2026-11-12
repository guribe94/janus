#!/bin/bash
# Message board for the orchestrator and every agent on every machine (shipped with orch; plain files, no service).
# The board lives on the coordinating machine in $BOARD_DIR (orch sets it; default <this script>/../board), marked by .master:
#   log.md               append-only, one line per post: YYYY-MM-DD HH:MM <agent> <kind>: <text>
#   agents/<name>.md     the agent's card: task, brief, machine, state, step, blockers, report, check, updated
#   to/<name>.md         the orchestrator's directions to that agent, newest first
#   questions/<id>.md    each question (id q<N>): it stays "needs answer" until the orchestrator answers it
#   .hosts               other hosts, one per line: <name> <ssh target> <root> (orch writes it from orch.toml)
#   .notify              a command run in the background on a new question or block (orch: wakes the orchestrator at once)
# On another host the same script (<root>/bin/board.sh) writes posts to <root>/board-outbox/outbox.log; `sync` on the
# coordinating machine merges each outbox in order and pushes a read-only copy of the board to <root>/board/ on each host,
# where `read`, `summary` and the peers.md / index.md files orch writes there can be read.
# Usage:
#   board.sh post <agent> <kind> <text>   kind: status blocked question waiting learned done (step: kept, but steps belong in the
#                                         report). waiting <job ids>: parked on jobs, never SILENT or GONE. learned <fact>: a
#                                         proposal for the session memory (orch check lists it until remembered or rejected).
#                                         A launcher sets the card's fixed fields on its first post: BOARD_MACHINE, BOARD_BRIEF,
#                                         BOARD_REPORT, BOARD_CHECK ("pid <n>", "herdr <agent>", "pueue <host>:<task>", "tab <label>").
#   board.sh direct <agent|all> <text>    orchestrator to an agent (or every open card); answers that agent's open questions
#   board.sh answer <question id> <text>  orchestrator: answer one question (a direction to its agent)
#   board.sh read <agent>                 the agent's directions
#   board.sh questions                    open questions: <id> <agent> <asked> <text> (tab-separated)
#   board.sh summary                      every open card grouped: needs answer, blocked, working, waiting, done; flags SILENT
#                                         (> BOARD_SILENT_MIN, default 30 min, without a post) and GONE (process ended without done)
#   board.sh digest [--since 1h]          for humans: landed, started, asked, answered, failed in that window
#   board.sh close <agent>                archive a card and its directions
#   board.sh rule <agent> [host]          the board rule a launcher adds to an agent's prompt (one place)
#   board.sh sync                         pull every host's outbox, then push the board copy to every host
# Every write on the coordinating machine takes one flock (.lock); bash 3.2 compatible.
set -u
self=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")
BD=${BOARD_DIR:-$(cd "$(dirname "$0")/.." && pwd)/board}
OUT=${BOARD_OUTBOX:-$(dirname "$BD")/board-outbox}
FLOCK=$(command -v flock || { [ -x /opt/homebrew/bin/flock ] && echo /opt/homebrew/bin/flock; } || echo)
master=0; [ -e "$BD/.master" ] && master=1
KINDS="status step blocked question waiting learned done"

die() { echo "board.sh: $*" >&2; exit 2; }
now() { date '+%Y-%m-%d %H:%M'; }
okname() { case $1 in ''|*[!A-Za-z0-9._-]*) die "bad agent name '$1'";; esac; }
okkind() { case " $KINDS " in *" $1 "*) return 0;; esac; return 1; }
oneline() { printf '%s' "$*" | tr '\n\r\t' '   '; }
field() { sed -n "s/^- $2: //p" "$1" 2>/dev/null | head -1; }
epoch_of() { date -j -f '%Y-%m-%d %H:%M' "$1" +%s 2>/dev/null || date -d "$1" +%s 2>/dev/null || echo 0; }
locked() { local f=$1; shift; if [ -n "$FLOCK" ]; then ( "$FLOCK" 9 || exit 1; "$@" ) 9>>"$f"; else "$@"; fi; }
localname() { local n; n=$(head -1 "$BD/.master" 2>/dev/null); [ -n "$n" ] && echo "$n" || hostname -s 2>/dev/null || echo local; }
notify() { [ -s "$BD/.notify" ] && { nohup sh -c "$(head -1 "$BD/.notify")" </dev/null >/dev/null 2>&1 & } ; true; }

# Apply one post on the master board (caller holds the lock): log line, question record, card rewritten through temp + mv.
apply() {
  local t=$1 a=$2 k=$3 x=$4 c f task brief machine state step blockers report check q n
  mkdir -p "$BD/agents" "$BD/to"
  if [ "$k" = question ]; then   # a question gets an id; it needs an answer until the orchestrator gives one
    mkdir -p "$BD/questions"; n=$(( $(cat "$BD/.qseq" 2>/dev/null || echo 0) + 1 )); echo $n > "$BD/.qseq"; q=q$n
    printf -- '- id: %s\n- agent: %s\n- asked: %s\n- state: open\n- text: %s\n' "$q" "$a" "$t" "$x" > "$BD/questions/$q.md"
    x="[$q] $x"
  fi
  printf '%s %s %s: %s\n' "$t" "$a" "$k" "$x" >> "$BD/log.md"
  c=$BD/agents/$a.md
  task=$(field "$c" task) brief=$(field "$c" brief) machine=$(field "$c" machine) state=$(field "$c" state)
  step=$(field "$c" step) blockers=$(field "$c" blockers) report=$(field "$c" report) check=$(field "$c" check)
  if [ -n "${BOARD_BRIEF:-}" ]; then
    brief=$BOARD_BRIEF; task=$(sed -n 's/^# *//p' "$brief" 2>/dev/null | head -1)
    [ -n "${BOARD_REPORT:-}" ] || report=$(grep -o "[^ \`'\"]*/out/$a/[A-Za-z0-9._-]*\.md" "$brief" 2>/dev/null | head -1)
  fi
  [ -n "${BOARD_REPORT:-}" ] && report=$BOARD_REPORT
  [ -n "${BOARD_MACHINE:-}" ] && machine=$BOARD_MACHINE
  [ -n "$machine" ] || machine=${BOARD_HOST:-}
  [ -n "${BOARD_CHECK:-}" ] && check=$BOARD_CHECK
  case $k in
    status)   state=working blockers= ;;
    step)     state=working step=$x blockers= ;;
    blocked)  state=blocked blockers=$x ;;
    question) state=question blockers=$x ;;
    waiting)  state=waiting step="waiting on $x" blockers= ;;
    learned)  [ -n "$state" ] || state=working ;;
    done)     state=done step=$x blockers= ;;
  esac
  f=$c.tmp.$$
  { echo "# $a"
    echo "- task: $task"; echo "- brief: $brief"; echo "- machine: $machine"; echo "- state: $state"; echo "- step: $step"
    echo "- blockers: $blockers"; echo "- report: $report"; echo "- check: $check"; echo "- updated: $t"
  } > "$f" && mv "$f" "$c"
  case $k in question|blocked) notify ;; esac
}

# Direct one agent (caller holds the lock): newest first in to/<agent>.md, a log line, and its open questions answered.
direct1() {
  local a=$1 x=$2 t=$3 f q c ids=
  if [ -z "${NOANSWER:-}" ]; then   # the agent's open questions this answers (a broadcast answers none)
    for q in "$BD"/questions/q*.md; do
      [ -e "$q" ] && [ "$(field "$q" agent)" = "$a" ] && [ "$(field "$q" state)" = open ] || continue
      [ -n "${ONLY_Q:-}" ] && [ "$(basename "$q" .md)" != "$ONLY_Q" ] && continue
      sed -e 's/^- state: open$/- state: answered/' "$q" > "$q.tmp.$$" && printf -- '- answered: %s\n- answer: %s\n' "$t" "$x" >> "$q.tmp.$$" && mv "$q.tmp.$$" "$q"
      ids="$ids$(basename "$q" .md) "
    done
  fi
  case $x in "["q*) ;; *) [ -n "$ids" ] && x="[$(echo $ids | tr ' ' ',')] $x" ;; esac
  f=$BD/to/$a.md
  { echo "- $t $x"; cat "$f" 2>/dev/null || true; } > "$f.tmp.$$" && mv "$f.tmp.$$" "$f"
  printf '%s orchestrator direction: @%s %s\n' "$t" "$a" "$x" >> "$BD/log.md"
  c=$BD/agents/$a.md
  if [ -e "$c" ] && [ "$(field "$c" state)" = question ] && ! openq | awk -F '\t' -v a="$a" '$2 == a' | grep -q .; then
    sed -e 's/^- state: question$/- state: working/' -e 's/^- blockers: .*/- blockers: /' "$c" > "$c.tmp.$$" && mv "$c.tmp.$$" "$c"
  fi
}

openq() {   # id <tab> agent <tab> asked <tab> text, oldest first
  local q
  for q in $(ls "$BD"/questions/q*.md 2>/dev/null | sed 's/.*\/q\([0-9]*\)\.md/\1/' | sort -n); do
    q=$BD/questions/q$q.md
    [ "$(field "$q" state)" = open ] && printf '%s\t%s\t%s\t%s\n' "$(field "$q" id)" "$(field "$q" agent)" "$(field "$q" asked)" "$(field "$q" text)"
  done; true
}

cardrows() {   # <group>\t<line> per card: 1 question, 2 blocked, 3 working, 4 waiting, 5 done
  local c a st u ck age flag gone s hp th tid b g n me
  n=$(date +%s) me=$(localname)
  for c in "$BD"/agents/*.md; do
    [ -e "$c" ] || continue
    a=$(basename "$c" .md) st=$(field "$c" state) u=$(field "$c" updated) ck=$(field "$c" check)
    age=$(( (n - $(epoch_of "$u")) / 60 )) flag=
    if [ "$st" != done ] && [ "$st" != waiting ]; then   # a parked agent (waiting) is never SILENT or GONE
      [ $age -gt ${BOARD_SILENT_MIN:-30} ] && flag="SILENT ${age}m"
      if [ $master = 1 ]; then
        gone=0
        case $ck in
          "pid "*) kill -0 "${ck#pid }" 2>/dev/null || gone=1 ;;
          "herdr "*) s=$(herdr agent get "${ck#herdr }" 2>/dev/null | jq -r '.result.agent.agent_status // empty' 2>/dev/null)
                     [ -n "$s" ] && [ "$s" != done ] || gone=1 ;;
          "pueue "*) hp=${ck#pueue }; th=${hp%%:*} tid=${hp#*:}
                     if [ "$th" = "$me" ] || ! hostsf | awk '{print $1}' | grep -qxF "$th"; then
                       s=$(pueue status --json 2>/dev/null | jq -r --arg t "$tid" '.tasks[$t].status | if type == "string" then . else keys[0] end' 2>/dev/null)
                       [ "$s" = Running ] || [ "$s" = Queued ] || gone=1
                     fi ;;
          "tab "*) herdr tab list 2>/dev/null | jq -e --arg L "${ck#tab }" '.result.tabs[]|select(.label==$L)' >/dev/null 2>&1 || gone=1 ;;
        esac
        [ $gone = 1 ] && flag="${flag:+$flag, }GONE (no done post)"
      fi
    fi
    b=$(field "$c" blockers); g=3
    case $st in question) g=1;; blocked) g=2;; working) g=3;; waiting) g=4;; done) g=5;; esac
    printf '%s\t%-24s %-8s %-9s %-6s %-40.40s %s\n' "$g" "$a" "$(field "$c" machine)" "$st" "${age}m" "$(field "$c" step)" "$b${b:+${flag:+; }}$flag"
  done
}

hostsf() { [ -s "$BD/.hosts" ] && grep -v '^[[:space:]]*\(#\|$\)' "$BD/.hosts"; true; }

cmd=${1:-}; shift || true
case $cmd in
  post)
    a=${1:-} k=${2:-}; shift 2 2>/dev/null || die "usage: post <agent> <kind> <text>"
    okname "$a"; okkind "$k" || die "kind must be one of: $KINDS"
    x=$(oneline "$*")
    if [ $master = 1 ]; then
      mkdir -p "$BD/agents" "$BD/to"
      BOARD_HOST=$(localname) locked "$BD/.lock" apply "$(now)" "$a" "$k" "$x" || die "post failed"
    else
      mkdir -p "$OUT"
      locked "$OUT/.lock" sh -c 'printf "%s\n" "$1" >> "$2"' _ "$(now) $a $k: $x" "$OUT/outbox.log" || die "post failed"
    fi ;;
  direct)
    [ $master = 1 ] || die "direct runs on the coordinating machine only"
    a=${1:-}; shift || true; x=$(oneline "$*"); [ -n "$x" ] || die "usage: direct <agent|all> <text>"
    mkdir -p "$BD/to"
    if [ "$a" = all ]; then
      t=$(now); n=0
      for c in "$BD"/agents/*.md; do
        [ -e "$c" ] || continue; b=$(basename "$c" .md); [ "$(field "$c" state)" = done ] && continue
        NOANSWER=1 locked "$BD/.lock" direct1 "$b" "(to all) $x" "$t" || die "direct failed"; n=$((n + 1))
      done
      echo "broadcast to $n open card(s). If it is a standing instruction, also: orch remember --decision \"$x\""
    else
      okname "$a"; locked "$BD/.lock" direct1 "$a" "$x" "$(now)" || die "direct failed"
    fi ;;
  answer)
    [ $master = 1 ] || die "answer runs on the coordinating machine only"
    q=${1:-}; shift || true; x=$(oneline "$*"); [ -n "$x" ] || die "usage: answer <question id> <text>"
    [ -e "$BD/questions/$q.md" ] || die "no question $q"
    a=$(field "$BD/questions/$q.md" agent); ONLY_Q=$q locked "$BD/.lock" direct1 "$a" "$x" "$(now)" || die "answer failed" ;;
  read)
    a=${1:-}; okname "$a"
    if [ -s "$BD/to/$a.md" ]; then cat "$BD/to/$a.md"; else echo "(no directions for $a)"; fi ;;
  questions) openq ;;
  summary)
    n=$(date +%s) me=$(localname)
    rows=$(cardrows | sort -t "$(printf '\t')" -k1,1n -s)
    printf '%-24s %-8s %-9s %-6s %-40s %s\n' AGENT MACHINE STATE AGE STEP "BLOCKERS / FLAGS"
    qs=$(openq)
    if [ -n "$qs" ]; then
      echo "== needs answer (board.sh answer <id> <text>)"
      printf '%s\n' "$qs" | while IFS="$(printf '\t')" read -r id ag at tx; do
        printf '  ? %-5s %-20s %3sm  %s\n' "$id" "$ag" "$(( (n - $(epoch_of "$at")) / 60 ))" "$tx"; done
    fi
    last=
    printf '%s\n' "$rows" | while IFS="$(printf '\t')" read -r g line; do
      [ -n "$line" ] || continue
      if [ "$g" != "$last" ]; then
        case $g in 1) echo "== question";; 2) echo "== blocked";; 3) echo "== working";; 4) echo "== waiting";; 5) echo "== done";; esac; last=$g
      fi
      printf '%s\n' "$line"
    done ;;
  digest)
    since=1h; [ "${1:-}" = --since ] && since=${2:-1h}
    case $since in *m) s=$(( ${since%m} * 60 ));; *h) s=$(( ${since%h} * 3600 ));; *d) s=$(( ${since%d} * 86400 ));; *) s=$since;; esac
    cut=$(( $(date +%s) - s )); cs=$(date -r "$cut" '+%Y-%m-%d %H:%M' 2>/dev/null || date -d "@$cut" '+%Y-%m-%d %H:%M')
    echo "board digest since $cs"
    awk -v cs="$cs" '
      { k = $4; sub(/:$/, "", k); if (($1 " " $2) < cs) next; who = $3; txt = $0; sub(/^[^ ]+ [^ ]+ [^ ]+ [^ ]+ /, "", txt)
        line = "  " $2 " " who ": " substr(txt, 1, 140)
        if (k == "done" && txt ~ /DONE/ && txt ~ /land/) L["landed"] = L["landed"] "\n" line
        else if (k == "done" && txt ~ /(BLOCKED|FAIL)/) L["failed"] = L["failed"] "\n" line
        else if (k == "done") L["done"] = L["done"] "\n" line
        else if (k == "blocked") L["failed"] = L["failed"] "\n" line
        else if (k == "question") L["asked"] = L["asked"] "\n" line
        else if (who == "orchestrator" && k == "direction" && txt ~ /\[q[0-9]+\]/) L["answered"] = L["answered"] "\n" line
        else if (who == "orchestrator" && k == "direction" && txt ~ /\(to all\)/) { bk = txt; sub(/^@[^ ]+ /, "", bk); if (!(bk in B)) { B[bk] = 1; L["broadcast"] = L["broadcast"] "\n" line } }
        else if (k == "status" && txt ~ /^launched/) L["started"] = L["started"] "\n" line
        else if (k == "learned") L["learned"] = L["learned"] "\n" line
        else if (txt ~ /landed/) L["landed"] = L["landed"] "\n" line }
      END { split("landed started done asked answered failed learned broadcast", O, " ")
            for (i = 1; i <= 8; i++) { n = split(L[O[i]], x, "\n") - 1; printf "%s: %d%s\n", O[i], (n < 0 ? 0 : n), L[O[i]] } }' "$BD/log.md" ;;
  close)
    [ $master = 1 ] || die "close runs on the coordinating machine only"
    a=${1:-}; okname "$a"; [ -e "$BD/agents/$a.md" ] || die "no card for $a"
    mkdir -p "$BD/agents/archive" "$BD/to/archive"
    s=$(date +%Y%m%d-%H%M)
    locked "$BD/.lock" sh -c 'mv "$1/agents/$2.md" "$1/agents/archive/$2.$3.md"; [ -e "$1/to/$2.md" ] && mv "$1/to/$2.md" "$1/to/archive/$2.$3.md"; true' _ "$BD" "$a" "$s" || die "close failed" ;;
  rule)
    a=${1:-}; okname "$a"; h=${2:-}
    p=$self
    if [ -n "$h" ]; then r=$(hostsf | awk -v h="$h" '$1 == h {print $3}'); [ -n "$r" ] && p=$r/bin/board.sh; fi
    # No quote characters: launchers embed this in quoted prompts and remote command lines.
    echo "Message board: run $p post $a status <one line> when you start; $p post $a waiting <job ids> when you park on jobs; $p post $a blocked <what blocks you> or $p post $a question <your question> when stuck; $p post $a learned <a fact every agent should know, with its source> when you find one; $p post $a done <your VERDICT line> at the end. Post only these state changes: your steps go in your report. Before each step run $p read $a and follow any new direction." ;;
  sync)
    [ $master = 1 ] || die "sync runs on the coordinating machine only"
    if [ -n "$FLOCK" ]; then exec 8>>"$BD/.sync.lock"; "$FLOCK" -n 8 || exit 0; fi   # one sync at a time
    S="ssh -n -o BatchMode=yes -o ConnectTimeout=8"
    if [ -s "$OUT/outbox.log" ]; then   # posts made here before this folder became the coordinating board
      mv "$OUT/outbox.log" "$OUT/local.$$"
      lmerge() { while IFS= read -r l; do
          d=${l%% *} r=${l#* }; tm=${r%% *} r=${r#* }; a=${r%% *} r=${r#* }; k=${r%%:*} x=${r#*: }
          okkind "$k" || continue; case $a in ''|*[!A-Za-z0-9._-]*) continue;; esac
          BOARD_BRIEF= BOARD_MACHINE= BOARD_CHECK= BOARD_REPORT= BOARD_HOST=$(localname) apply "$d $tm" "$a" "$k" "$x"
        done < "$OUT/local.$$"; }
      locked "$BD/.lock" lmerge && rm -f "$OUT/local.$$"
    fi
    hostsf | while read -r h ssh root; do
      # Pull: under the host's outbox lock, rename outbox.log to a numbered sending file, then merge every sending file in name
      # order. A merged name is recorded before the file is deleted there, so nothing is merged twice or lost.
      files=$($S "$ssh" "o=$root/board-outbox; mkdir -p \$o; cd \$o && { command -v flock >/dev/null && flock .lock sh -c '[ -s outbox.log ] && mv outbox.log sending.\$(date +%s%N); true' || { [ -s outbox.log ] && mv outbox.log sending.\$(date +%s)\$\$; true; }; } && ls sending.* 2>/dev/null | sort" 2>/dev/null) || continue
      for f in $files; do
        if ! grep -qxF "$f" "$BD/.merged-$h" 2>/dev/null; then
          body=$($S "$ssh" "cat $root/board-outbox/$f" 2>/dev/null) || continue
          merge() {
            printf '%s\n' "$body" | while IFS= read -r l; do
              d=${l%% *} r=${l#* }; tm=${r%% *} r=${r#* }; a=${r%% *} r=${r#* }; k=${r%%:*} x=${r#*: }
              okkind "$k" || continue
              case $a in ''|*[!A-Za-z0-9._-]*) continue;; esac
              BOARD_BRIEF= BOARD_MACHINE= BOARD_CHECK= BOARD_REPORT= BOARD_HOST=$h apply "$d $tm" "$a" "$k" "$x"
            done
            echo "$f" >> "$BD/.merged-$h"
          }
          locked "$BD/.lock" merge || continue
        fi
        $S "$ssh" "rm -f $root/board-outbox/$f" 2>/dev/null
      done
      # Push the read-only copy and this script (<root>/bin/board.sh); lock, merge records, hosts and notify stay here.
      $S "$ssh" "mkdir -p $root/bin $root/board" 2>/dev/null
      rsync -a --delete --chmod=Fa-w --exclude .lock --exclude .sync.lock --exclude '.merged-*' --exclude .master --exclude .notify \
        --exclude .hosts --exclude .qseq --exclude '*.tmp.*' -e 'ssh -o BatchMode=yes -o ConnectTimeout=8' "$BD/" "$ssh:$root/board/" </dev/null 2>/dev/null &&
        $S "$ssh" "find $root/board -type f -perm -u+w -exec chmod a-w {} +" 2>/dev/null
      rsync -a -e 'ssh -o BatchMode=yes -o ConnectTimeout=8' "$self" "$ssh:$root/bin/board.sh" </dev/null 2>/dev/null
    done ;;
  *) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
