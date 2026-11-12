"""Shell and Python templates janus writes to disk, one block per template (section 7, Step 1).
Pure string constants: rendered by their callers (probe, cap_lib, job_script, clean_pueue_script, tidy_host,
scope_script, gate). tests/test_templates.py renders and `sh -n`-checks the shell ones.
"""


PROBE = r'''PATH="$HOME/.local/bin:%(bin)s:$PATH:/usr/bin:/bin:/usr/sbin:/sbin"
if [ "$(uname)" = Darwin ]; then
 # Two readings, the larger wins: the kernel's free level (memory_pressure's "free percentage", read straight from sysctl, no
 # tool run under load) and vm_stat's free + inactive + speculative + purgeable pages. Under load one of them can read 0
 # (memory_pressure timing out or the level lagging), which made the Mac look full; an empty reading is reported as empty.
 mem=$(sysctl -n hw.memsize 2>/dev/null); fp=$(sysctl -n kern.memorystatus_level 2>/dev/null)
 [ -n "$fp" ] || fp=$(memory_pressure 2>/dev/null | awk '/free percentage/{gsub(/%%/,"",$NF);print int($NF)}')
 a=0; [ -n "$fp" ] && [ -n "$mem" ] && a=$((mem/1048576*fp/100))
 b=$(vm_stat 2>/dev/null | awk '/page size of/{ps=$(NF-1)} /^Pages (free|inactive|speculative|purgeable):/{gsub(/\./,"",$NF);n+=$NF} END{if (ps) print int(n*ps/1048576)}')
 mb=${b:-0}; [ "$a" -gt "$mb" ] && mb=$a; [ "$mb" -gt 0 ] || mb=
 echo "avail_mb=$mb"; echo "load=$(sysctl -n vm.loadavg | tr -d '{}' | awk '{print $1}')"; echo "os=mac"
else
 echo "avail_mb=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)"; echo "load=$(cut -d' ' -f1 /proc/loadavg)"
 echo "psi=$(awk '/^some/{sub("avg10=","",$2);print $2}' /proc/pressure/memory 2>/dev/null)"; echo "os=linux"
fi
d=%(root)s; [ -d "$d" ] || d=/; echo "disk_gb=$(df -Pk "$d" 2>/dev/null | awk 'NR==2{print int($4/1048576)}')"
%(extra)s
RD=%(rd)s; CAP_RO=%(ro)s
%(cap)s
cap_report
echo "--pueue"; %(pueue)s status --json 2>/dev/null
echo "--tmux"; tmux ls -F '#S' 2>/dev/null; true'''

DIRECT_PROBE = r'''if [ -d /proc/1 ]; then
 for f in /proc/[0-9]*/environ; do u=$(tr '\0' '\n' < $f 2>/dev/null | sed -n 's/^JANUS_UNIT=//p'); [ -n "$u" ] && echo "${f#/proc/} $u"; done 2>/dev/null | sed 's#/environ##' > /tmp/.janus-units.$$
 ps -A -o pid=,ppid=,pgid=,etimes=,args= | awk -v rx=%(rx)s 'NR == FNR {u[$1] = $2; next} {pp[$1] = $2; pg[$1] = $3; et[$1] = $4; c = $0; sub(/^ *[0-9]+ +[0-9]+ +[0-9]+ +[0-9]+ +/, "", c); cmd[$1] = c}
   END {for (p in u) {if (!(p in cmd) || cmd[p] !~ rx || cmd[p] ~ /\.lane( |$)|janus(\.py)? (run|gate) |janus-test-fixture/) continue; q = pp[p]; lane = 0; n = 0
          while (q > 1 && n++ < 50) {if (cmd[q] ~ /pueued/ || cmd[q] ~ /cmd\.sh|\/runs\/[^ ]*\.sh/) {lane = 1; break}; if (u[q] != "" && cmd[q] ~ rx) {lane = 1; break}; q = pp[q]}
          if (!lane) print "direct=" p, pg[p], u[p], et[p], substr(cmd[p], 1, 100)}}' /tmp/.janus-units.$$ - 2>/dev/null
 rm -f /tmp/.janus-units.$$
fi'''

# Claude/Anthropic API processes janus did not start (spend safety): an API key in the environment and no JANUS_UNIT (every
# agent janus starts has one), command claude or anthropic. Prints apiproc=<pid> <pgid> <etime> <command>; never the environment.
API_PROBE = r'''if [ -d /proc/1 ]; then
 for f in /proc/[0-9]*/environ; do e=$(tr '\0' '\n' < "$f" 2>/dev/null); case "$e" in *ANTHROPIC_API_KEY=*) case "$e" in *JANUS_UNIT=*) ;; *) p=${f#/proc/}; echo "${p%/environ}";; esac;; esac; done 2>/dev/null > /tmp/.janus-api.$$
else
 ps -Eww -A -o pid=,command= 2>/dev/null | awk '/ANTHROPIC_API_KEY=/ && !/JANUS_UNIT=/ {print $1}' > /tmp/.janus-api.$$
fi
ps -A -o pid=,pgid=,etime=,command= | awk 'NR == FNR {k[$1] = 1; next} ($1 in k) {c = $0; sub(/^ *[0-9]+ +[0-9]+ +[^ ]+ +/, "", c); if (c ~ /(^|\/)claude( |$)|node .*claude|anthropic/) print "apiproc=" $1, $2, $3, substr(c, 1, 100)}' /tmp/.janus-api.$$ - 2>/dev/null
rm -f /tmp/.janus-api.$$'''

CAP_LIB = r'''# janus capacity library for host %(name)s (written by janus, see cap_lib in janus.py; do not edit: the next janus run rewrites it).
# Sourced by every waiting job each poll and sent inline with every probe. Needs RD (the runs dir).
CORES=%(cores)d CPJ=%(cpj)d MEMCAP=%(memcap)d CAPMB=%(capmb)d AGMB=%(agmb)d PSIP=%(psip)s LOADX=%(loadx)s HOLD=%(hold)d DISK=%(disk)d HN=%(hn)d MARGIN=%(margin)d
AGEW=%(agew)s EXPS=%(exps)d
U=$(id -u); AS=/sys/fs/cgroup/user.slice/user-$U.slice/user@$U.service/app.slice; US=/sys/fs/cgroup/user.slice/user-$U.slice/user@$U.service/user.slice
cap_mb() {  # $1 class, $2 default MB, $3 cap MB (0: none): the max of the class's last HN recorded peaks + MARGIN %%, at least
  # 256 (class agent: the median of its last 3 x HN peaks, at least 128: many light agents seldom peak together)
  [ -f "$RD/history.tsv" ] || { echo "$2"; return; }
  awk -F'\t' -v c="$1" -v n=$HN -v m=$MARGIN -v d="$2" -v cap="$3" 'BEGIN {if (c == "agent") n *= 3} $2 == c && $3 > 0 {p[k++ %% n] = $3}
    END {if (!k) {print d; exit}; j = 0; for (i in p) v_[++j] = p[i] + 0
         for (a = 2; a <= j; a++) {x = v_[a]; b = a - 1; while (b > 0 && v_[b] > x) {v_[b + 1] = v_[b]; b--}; v_[b + 1] = x}
         mx = (c == "agent") ? v_[int((j + 1) / 2)] : v_[j]; lo = (c == "agent") ? 128 : 256
         v = int(mx * (100 + m) / 100); if (v < lo) v = lo; if (cap > 0 && v > cap) v = cap; print v}' "$RD/history.tsv"
}
cap_cores() {  # $1 class: the cores it reserves and is capped at: its last HN runs' CPU seconds / run time, the largest, rounded,
  # 1 to CPJ (CPJ, the host's cores_per_job, until the class has runs with a measured CPU figure: column 6 of history.tsv)
  [ -f "$RD/history.tsv" ] || { echo "$CPJ"; return; }
  awk -F'\t' -v c="$1" -v n=$HN -v d=$CPJ '$2 == c && $4 > 0 && $6 > 0 {r[k++ %% n] = $6 / $4}
    END {if (!k) {print d; exit}; mx = 0; for (i in r) if (r[i] > mx) mx = r[i]; v = int(mx + 0.5); if (v < 1) v = 1; if (v > d) v = d; print v}' "$RD/history.tsv"
}
cap_secs() {  # $1 class: its expected run time (s), the median of its recorded runs; empty without history
  [ -f "$RD/history.tsv" ] || return 0
  awk -F'\t' -v c="$1" '$2 == c && $4 ~ /^[0-9]+$/ {v_[++j] = $4 + 0}
    END {if (!j) exit; for (a = 2; a <= j; a++) {x = v_[a]; b = a - 1; while (b > 0 && v_[b] > x) {v_[b + 1] = v_[b]; b--}; v_[b + 1] = x}; print v_[int(j / 2) + 1]}' "$RD/history.tsv"
}
ahead() {  # $1 this job's wait file ("pid expected-s since MB cores"): NB jobs waiting here that go first, holding BCC cores, BMB MB.
  # First: the lower expected run time minus AGEW x seconds waited (short jobs first, a waiting job ages), among the same wait
  # kind (.wait or .gwait). A job is admitted only when it fits together with every job ahead of it, so it never takes their room.
  NB=0 BCC=0 BMB=0; nowt=$(date +%%s); me_e= me_t=; { read -r _ me_e me_t _ _ < "$1"; } 2>/dev/null
  case $1 in *.gwait) pat='*.gwait';; *) pat='*.wait';; esac
  for f in "$RD"/$pat; do
    [ "$f" = "$1" ] || [ ! -e "$f" ] && continue
    p= e= t= m= c=; { read -r p e t m c < "$f"; } 2>/dev/null
    [ -n "$c" ] && kill -0 "$p" 2>/dev/null || continue
    awk -v a="$e" -v ta="$t" -v b="$me_e" -v tb="$me_t" -v n="$nowt" -v w="$AGEW" 'BEGIN {sa = a - w * (n - ta); sb = b - w * (n - tb); exit !(sa < sb || (sa == sb && ta < tb))}' || continue
    NB=$((NB + 1)); BCC=$((BCC + c)); BMB=$((BMB + m))
  done
}
cap_class() {  # the job class of a command: the first [capacity.classes] pattern it matches, else its first command words
  c=$1
%(classes)s  printf '%%s\n' "$c" | awk '{n = split($0, seg, /&&|;|\|\|/)
    for (s = 1; s <= n; s++) {m = split(seg[s], w, " "); i = 1; wr = 0
      while (i <= m && (w[i] ~ /^[A-Za-z_][A-Za-z0-9_]*=/ || w[i] ~ /^(env|nice|ionice|timeout|nohup|time|exec|command|stdbuf|systemd-run)$/ || (wr && (w[i] ~ /^-/ || w[i] ~ /^[0-9.]+[smhd]?$/)))) {if (w[i] !~ /=/ || w[i] ~ /^-/) wr = 1; i++}
      if (i > m || w[i] ~ /^(cd|export|source|\.|set|true|:|\(|\)|echo|printf)$/) continue
      out = ""; k = 0
      for (; i <= m && k < 3; i++) {x = w[i]; if (x ~ /^-/ || x ~ /=/ || x ~ /^[<>|&]/ || x ~ /^[0-9.:]+$/) break
        if (x ~ /\//) {sub(/.*\//, "", x); out = out (k ? "_" : "") x; k++; break}
        out = out (k ? "_" : "") x; k++}
      gsub(/[^A-Za-z0-9_.+-]/, "", out); if (out != "") {print out; exit}}
    print "job"}'
}
res_sum() {  # live reservations: RJ jobs holding RC cores and RM MB, RA agents holding RAM MB; RL lists them. A reservation
  # whose process is gone is removed (not by a read-only probe). Jobs without one (older scripts, another scheduler) count at CAPMB.
  RJ=0 RC=0 RM=0 RA=0 RAM=0 RL=
  for f in "$RD"/*.res; do
    [ -e "$f" ] || continue
    p= c= m= t= k=; { read -r p c m t k < "$f"; } 2>/dev/null
    case "$p$c$m" in *[!0-9]*|"") continue;; esac
    if ! kill -0 "$p" 2>/dev/null; then [ -n "$CAP_RO" ] || rm -f "$f"; continue; fi
    case $k in agent*) RA=$((RA + 1)); RAM=$((RAM + m));; *) RJ=$((RJ + 1)); RC=$((RC + c)); RM=$((RM + m));; esac
    RL="$RL
res=$p $c $m $t $k ${f##*/}"
  done
  for j in $( { %(jobids)s; } 2>/dev/null); do
    [ -e "$RD/$j.res" ] && continue
    RJ=$((RJ + 1)); RC=$((RC + CPJ)); RM=$((RM + CAPMB)); RL="$RL
res=- $CPJ $CAPMB - untracked $j"
  done
}
cap_measure() {  # PSI (macOS: the kernel's pressure level, warn = 50, critical = 100), LOAD (1 min), DF (GB free in RD)
  if [ "$(uname)" = Darwin ]; then
    case $(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null) in 4) PSI=100;; 2) PSI=50;; *) PSI=0;; esac
    LOAD=$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | awk '{print $1}')
  else
    PSI=$(awk '/^some/{sub("avg10=", "", $2); print $2}' /proc/pressure/memory 2>/dev/null); LOAD=$(awk '{print $1}' /proc/loadavg)
  fi
  d=$RD; while [ ! -d "$d" ] && [ "$d" != / ]; do d=$(dirname "$d"); done   # a runs dir not made yet: its disk all the same
  PSI=${PSI:-0}; LOAD=${LOAD:-0}; DF=$(df -Pk "$d" 2>/dev/null | awk 'NR==2{print int($4/1048576)}'); DF=${DF:-0}
}
cap_paused() {  # back-pressure: PAUSE is why admission waits (memory PSI over PSIP; load over LOADX x CORES for HOLD s, tracked in RD/overload)
  PAUSE=; nowt=$(date +%%s)
  awk -v a="$PSI" -v b="$PSIP" 'BEGIN {exit !(a + 0 > b + 0)}' && PAUSE="memory PSI avg10 $PSI over $PSIP"
  if awk -v l="$LOAD" -v c="$CORES" -v x="$LOADX" 'BEGIN {exit !(l + 0 > c * x)}'; then
    since= last=; { read -r since last < "$RD/overload"; } 2>/dev/null
    { [ -n "$last" ] && [ $((nowt - last)) -le $((HOLD + 60)) ]; } || since=$nowt   # a gap in the readings starts a new period
    [ -n "$CAP_RO" ] || { echo "$since $nowt" > "$RD/overload.$$" && mv -f "$RD/overload.$$" "$RD/overload"; }
    [ $((nowt - since)) -ge "$HOLD" ] && PAUSE="${PAUSE:+$PAUSE; }load $LOAD over $LOADX x $CORES cores for $((nowt - since)) s"
  else [ -n "$CAP_RO" ] || rm -f "$RD/overload"; fi
}
admit_ok() {  # $1 the job's class. Sets MB and CC (its reservation) and why; true when it may start now
  MB=$(cap_mb "${1:-job}" $CAPMB $CAPMB); CC=$(cap_cores "${1:-job}"); res_sum; cap_measure; cap_paused
  why="reserved $RJ job(s): $RC of $CORES cores, $RM MB + agents $RAM MB + this ${1:-job} $CC cores $MB MB of $MEMCAP MB (RAM - services); PSI $PSI, load $LOAD, disk $DF GB (floor $DISK)${PAUSE:+; PAUSED: $PAUSE}"
  BLK=; [ -n "$PAUSE" ] && BLK="paused"; [ "$DF" -ge "$DISK" ] || BLK="$BLK disk"
  if [ "$RJ" -gt 0 ]; then [ $((RC + CC)) -le "$CORES" ] || BLK="$BLK cores"; [ $((RM + RAM + MB)) -le "$MEMCAP" ] || BLK="$BLK memory"; fi
  [ -z "$BLK" ]
}
job_cpu() {  # $1 job id: CPU seconds its cgroup used so far (a container or scope; empty where the job is a plain process tree)
  v=
%(cpusample)s
  echo "$v"
}
job_mb() {  # $1 pid, $2 job id: the job's resident memory now, in MB (empty when it cannot be read)
  v=
%(sample)s
  echo "$v"
}
tree_mb() {  # resident memory of process $1 and all its descendants, MB
  ps -A -o pid=,ppid=,rss= | awk -v r="$1" '{pp[$1] = $2; rs[$1] = $3} END {q[r] = 1; do {ch = 0; for (x in pp) if (!(x in q) && (pp[x] in q)) {q[x] = 1; ch = 1}} while (ch); for (x in q) s += rs[x]; print int(s / 1024)}'
}
cap_record() {  # $1 class $2 peak MB $3 seconds $4 exit code $5 CPU seconds $6 queue wait (s): one line of RD/history.tsv (last 2000 kept)
  { [ "${2:-0}" -gt 0 ] || [ "${5:-0}" -gt 0 ]; } 2>/dev/null || return 0   # nothing measured: no line
  printf '%%s\t%%s\t%%s\t%%s\t%%s\t%%s\t%%s\n' "$(date +%%s)" "$1" "$2" "$3" "$4" "${5:-0}" "${6:-0}" >> "$RD/history.tsv"
  [ "$(wc -l < "$RD/history.tsv")" -gt 2400 ] && tail -n 2000 "$RD/history.tsv" > "$RD/history.tsv.$$" && mv -f "$RD/history.tsv.$$" "$RD/history.tsv"
  return 0
}
cap_report() {  # for janus's probe: the sums, the pause, each reservation, the last 300 history lines
  res_sum; cap_measure; cap_paused
  echo "cap_rj=$RJ"; echo "cap_rc=$RC"; echo "cap_rm=$RM"; echo "cap_ra=$RA"; echo "cap_ram=$RAM"; echo "cap_pause=$PAUSE"; echo "cap_psi=$PSI"
  admit_ok job >/dev/null 2>&1; echo "cap_admit=${BLK:-yes}"   # the admission's own verdict for one more job now (yes, or what blocks it)
  [ -n "$RL" ] && printf '%%s\n' "$RL" | sed '/^$/d'
  [ -f "$RD/history.tsv" ] && tail -n 300 "$RD/history.tsv" | sed 's/^/hist=/'
  true
}
'''

STOP_GROUP = r'''p=%(pid)d g=%(pgid)d
kids() { ps -A -o pid=,ppid= | awk -v r=$p '{pp[$1] = $2} END {q[r] = 1; do {c = 0; for (x in pp) if (!(x in q) && (pp[x] in q)) {q[x] = 1; c = 1}} while (c); for (x in q) print x}'; }
if [ "$g" -gt 1 ] && ! ps -A -o pgid=,args= | awk -v g=$g '$1 == g' | grep -Eq '(^|/)(claude|codex)( |$)|node .*(claude|codex)|pueued'; then
  kill -TERM -$g 2>/dev/null; sleep 5; kill -KILL -$g 2>/dev/null; echo "group $g stopped"
else
  t=$(kids); kill -TERM $t 2>/dev/null; sleep 5; kill -KILL $t 2>/dev/null; echo "process $p and its children stopped (group $g holds the agent)"
fi'''

CLEAN_PUEUE = r'''#!/bin/sh
# pueue with an allow-listed environment (written by janus): a task never stores the caller's ANTHROPIC_API_KEY or tokens
real=%(real)s
# an agent's own `pueue add` gets its unit's label and the project's id, so janus check never calls it an orphan (issue 24)
if [ "$1" = add ] && [ -n "$JANUS_UNIT" ]; then case " $* " in *" -l "*|*" --label "*|*" --label="*) ;; *) shift; set -- add -l "unit:$JANUS_UNIT${JANUS_PROJECT:+@$JANUS_PROJECT}" "$@";; esac; fi
exec env -i HOME="$HOME" USER="$USER" LOGNAME="${LOGNAME:-$USER}" PATH="$PATH" LANG="${LANG:-C.UTF-8}" SHELL="${SHELL:-/bin/sh}" \
  TMPDIR="${TMPDIR:-/tmp}" ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"} ${XDG_CONFIG_HOME:+XDG_CONFIG_HOME="$XDG_CONFIG_HOME"} ${JANUS_UNIT:+JANUS_UNIT="$JANUS_UNIT"} ${JANUS_ROLE:+JANUS_ROLE="$JANUS_ROLE"} "$real" "$@"
'''

UNIT_PROCS = r'''u=%(unit)s; me=$$; roots="%(roots)s"
if [ -d /proc/1 ]; then
  env_pids=$(grep -lsaz "^JANUS_UNIT=$u\$" /proc/[0-9]*/environ 2>/dev/null | cut -d/ -f3)
fi   # macOS: the caller passes those pids in roots (mac_env_pids)
ps -A -o pid=,ppid=,pgid= | awk -v seeds="$env_pids $roots" -v me=$me '
  {p[$1] = $2; g[$1] = $3; kids[$2] = kids[$2] " " $1}
  END {n = split(seeds, s, " "); for (i = 1; i <= n; i++) if (s[i] in p) q[s[i]] = 1
       do {ch = 0; for (x in q) {m = split(kids[x], k, " "); for (j = 1; j <= m; j++) if (!(k[j] in q)) {q[k[j]] = 1; ch = 1}}} while (ch)
       for (x in q) if (x != me && x > 1) print x, g[x]}'
'''

FINGERPRINT = r'''cd %(root)s 2>/dev/null || exit 3
d=$( { [ -d base/%(n)s ] && diff -ruN %(ex)s base/%(n)s worktrees/%(n)s 2>/dev/null; } | cksum)
f() { cd out/%(n)s 2>/dev/null && find . -type f ! -name changes.patch ! -name backup.patch ! -name 'task-*.log' ! -name '*.key' "$@"; }
o=$(f -exec cksum {} + | sort | cksum); n=$(f | wc -l); p=$(grep -cv '^VERDICT: PENDING$' out/%(n)s/REPORT.md 2>/dev/null)
echo "fp=$d|$o"; echo "n=$n"; echo "p=${p:-0}"'''

ADMIT = r'''# Admission (janus-fixes8): by reservations, rule in $RD/cap.sh (re-read each poll). One job is judged at a time under the lock;
# an admitted job writes its reservation ($R.res) before the lock is released. A jobs-lane job yields while a gates job waits.
[ -f "$RD/cap.sh" ] || { echo "janus: no $RD/cap.sh on this host (janus run or janus launch writes it)"; exit 125; }
lane=%(lane)s; BROAD=%(broad)s; . "$RD/cap.sh"; CLS=$(cap_class %(clsrc)s)
EXP=$(cap_secs "$CLS"); EXP=${EXP:-$EXPS}; [ -n "$BROAD" ] && EXP=$((EXP * 2))   # expected run time; a broad run counts double (goes later)
if command -v flock >/dev/null 2>&1; then exec 7>>"$RD/admit.lock"; lk() { flock 7; }; ulk() { flock -u 7; }
else   # no flock: a lock dir holding its owner's pid; a dead owner's dir is taken over (a SIGKILL never leaves the host stuck)
  lk() { until mkdir "$RD/admit.d" 2>/dev/null; do o=$(cat "$RD/admit.d/pid" 2>/dev/null); [ -n "$o" ] && ! kill -0 "$o" 2>/dev/null && rm -rf "$RD/admit.d"; sleep 1; done; echo $$ > "$RD/admit.d/pid"; HELD=1; }
  ulk() { rm -rf "$RD/admit.d"; HELD=; }; fi
gates_waiting() { for f in "$RD"/*.gwait; do [ -e "$f" ] && kill -0 "$(cut -d' ' -f1 "$f")" 2>/dev/null && return 0; done; return 1; }
W="$R.wait"; [ "$lane" = gates ] && W="$R.gwait"; echo "$$ $EXP $(date +%%s) $(cap_mb "$CLS" $CAPMB $CAPMB) $(cap_cores "$CLS")" > "$W"
HELD=; trap 'rm -f "$W" "$R.res"; [ -n "$HELD" ] && rm -rf "$RD/admit.d"' EXIT; trap 'rm -f "$W" "$R.res"; [ -n "$HELD" ] && rm -rf "$RD/admit.d"; exit 143' TERM INT HUP   # a job killed while waiting leaves no files (L1)
said=
while :; do
  if [ "$lane" = gates ] || ! gates_waiting; then
    lk; CAP_RO=; . "$RD/cap.sh"
    if admit_ok "$CLS"; then
      ahead "$W"   # shorter or older jobs waiting here go first unless this one fits beside them
      if [ "$NB" -gt 0 ] && { [ $((RC + CC + BCC)) -gt "$CORES" ] || [ $((RM + RAM + MB + BMB)) -gt "$MEMCAP" ]; }; then BLK="yield to $NB shorter or older job(s)"
      else echo "$$ $CC $MB $(date +%%s) $CLS" > "$R.res.$$" && mv -f "$R.res.$$" "$R.res"; ulk; break; fi
    fi
    ulk; [ "$said" = "$BLK" ] || { echo "janus: waiting for admission ($CLS; $BLK): $why"; said=$BLK; }
  fi
  sleep ${JANUS_POLL_S:-15}
done
rm -f "$W"; exec 7>&-; trap 'rm -f "$R.res"' EXIT
WAITED=$(( $(date +%%s) - ${TQ:-$(date +%%s)} ))
echo "janus: admitted ($CLS, reserves $CC cores and $MB MB, expected ${EXP}s, waited ${WAITED}s): $why"
# The job's CPU is capped at its reservation: build and test parallelism (the job's own setting wins), and on Linux the scope's
# CPUQuota or the container's --cpus (job_script).
export JANUS_CORES=$CC MAKEFLAGS="${MAKEFLAGS:--j$CC}" CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-$CC} RUST_TEST_THREADS=${RUST_TEST_THREADS:-$CC} \
  NEXTEST_TEST_THREADS=${NEXTEST_TEST_THREADS:-$CC} PYTEST_XDIST_AUTO_NUM_WORKERS=${PYTEST_XDIST_AUTO_NUM_WORKERS:-$CC} OMP_NUM_THREADS=${OMP_NUM_THREADS:-$CC}
cpu_s() { awk 'NR == 2 {s = 0; for (i = 1; i <= 2; i++) {x = $i; sub(/s$/, "", x); split(x, y, "m"); s += y[1] * 60 + y[2]}; print s}' "$1"; }
run_sampled() {  # run "$@", sample its memory (and a cgroup's CPU) every 5 s, then record class, peak MB, seconds, exit code,
  # CPU seconds (the waited-for children's, from the shell's times; a container's from its cgroup) and queue wait in history.tsv.
  # The peak is also the kernel's high-water mark of its largest process (platform.MAXRSS_PY), so a job that ends between two
  # samples, or before its first sample sees it allocate (a slow start under load), still records its real peak.
  t0=$(date +%%s); pf="$R.maxrss"; rm -f "$pf"; times > "$R.t0"
  if command -v python3 >/dev/null 2>&1; then python3 -c %(maxrss)s "$pf" "$@" & else "$@" & fi; pid=$!; peak=0; cpu=0
  while kill -0 $pid 2>/dev/null; do
    v=$(job_mb $pid %(jid)s); [ "${v:-0}" -gt "$peak" ] 2>/dev/null && peak=$v
    v=$(job_cpu %(jid)s); [ "${v:-0}" -gt "$cpu" ] 2>/dev/null && cpu=$v; sleep 5
  done
  wait $pid; rc=$?; times > "$R.t1"; k=$(cat "$pf" 2>/dev/null); rm -f "$pf"; [ "${k:-0}" -gt "$peak" ] 2>/dev/null && peak=$k
  v=$(awk -v a="$(cpu_s "$R.t0")" -v b="$(cpu_s "$R.t1")" 'BEGIN {print int(b - a + 0.5)}'); rm -f "$R.t0" "$R.t1"
  [ "${v:-0}" -gt "$cpu" ] 2>/dev/null && cpu=$v
  cap_record "$CLS" "$peak" $(( $(date +%%s) - t0 )) $rc $cpu $WAITED; return $rc
}
'''

CONDENSE = r'''
import glob, json, os, re, sys
mode, grep, tail, since, cwd = sys.argv[1:6]; tail = int(tail); since = float(since); pats = sys.argv[6:]
rx = re.compile(grep, re.I) if grep else None
def flat(x):
    if isinstance(x, list): return " ".join(flat(y) for y in x)
    if isinstance(x, dict): return str(x.get("text") or x.get("content") or x.get("output") or json.dumps(x))
    return str(x or "")
def show(tag, full, n):
    full = re.sub(r"\s+", " ", full).strip()
    m = rx.search(full) if rx else None
    if m and len(full) > n: a = max(0, m.start() - n // 3); return f"{tag}{'...' if a else ''}{full[a:a + n]}{'...' if a + n < len(full) else ''}"
    return tag + (full if len(full) <= n else full[:n] + "...")
def tool(name, inp):
    if isinstance(inp, str):
        try: inp = json.loads(inp)
        except Exception: pass
    k = next((k for k in ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description") if isinstance(inp, dict) and inp.get(k)), None)
    return f"> {name}: ", flat(inp[k]) if k else (inp if isinstance(inp, str) else json.dumps(inp)), 160
def items(f):
    for l in open(f, errors="replace"):
        try: o = json.loads(l)
        except Exception: continue
        t = o.get("type")
        if t in ("user", "assistant") and isinstance(o.get("message"), dict):      # Claude Code
            c = o["message"].get("content"); who = "USER: " if t == "user" else "AGENT: "
            if isinstance(c, str): yield who, c, 600; continue
            for b in c or []:
                if not isinstance(b, dict): continue
                if b.get("type") == "text" and b.get("text", "").strip(): yield who, b["text"], 600
                elif b.get("type") == "tool_use": yield tool(b.get("name"), b.get("input"))
                elif b.get("type") == "tool_result": yield ("< ERROR " if b.get("is_error") else "< "), flat(b.get("content")), 200
        elif t == "response_item":                                                  # Codex
            p = o.get("payload") or {}; pt = p.get("type")
            if pt == "message" and p.get("role") in ("user", "assistant"):
                tx = flat(p.get("content"))
                if tx.strip() and not tx.lstrip().startswith(("<", "# AGENTS.md")): yield ("USER: " if p["role"] == "user" else "AGENT: "), tx, 600
            elif pt in ("function_call", "custom_tool_call", "local_shell_call"): yield tool(p.get("name") or pt, p.get("arguments") or p.get("input") or p.get("action"))
            elif pt in ("function_call_output", "custom_tool_call_output"): yield "< ", flat(p.get("output")), 200
fs = []
for p in pats: fs += glob.glob(os.path.expanduser(p), recursive=True)
fs = sorted(set(fs), key=os.path.getmtime)
if mode == "newest": fs = fs[-1:]
if mode == "codex-cwd":   # Codex sessions of a cwd (no thread id known): their session_meta line names it
    def meta_cwd(f):
        try: return (json.loads(open(f, errors="replace").readline()).get("payload") or {}).get("cwd")
        except Exception: return None
    fs = [f for f in fs if os.path.getmtime(f) >= since and meta_cwd(f) == cwd]
if not fs: print("(no transcript found)"); sys.exit(3)
out = []
for f in fs:
    body = [show(tag, full, n) for tag, full, n in items(f) if not rx or rx.search(full)]
    out.append((f, body))
if tail > 0:   # the last N lines over all sessions, newest session last
    keep, left = [], tail
    for f, body in reversed(out):
        if left <= 0: break
        keep.insert(0, (f, body[-left:])); left -= len(body[-left:])
    out = keep
print("\n".join(f"## {f}\n" + "\n".join(b) for f, b in out))
'''
