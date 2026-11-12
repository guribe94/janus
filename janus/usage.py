"""What an agent session has used, read from its transcript (one JSON object per line). Standard library only and free of
every other janus module: for a unit on another host janus runs this very file there (`python3 - <transcript> <since>` with
the file on stdin) and reads the one JSON line it prints."""
import calendar, json, sys, time

USE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

def usage_scan(path, since=0):
    """{"ctx": tokens of the session's last request and answer (what a resume must load again), "model": its model, "use":
    {model: {tokens per kind}} of the assistant messages after `since` (a time; 0: all)}. A message written on several lines
    counts once (its id)."""
    seen, last = {}, None
    with open(path, errors="replace") as f:
        for l in f:
            if '"usage"' not in l: continue
            try: d = json.loads(l)
            except ValueError: continue
            m = d.get("message") if isinstance(d, dict) else None
            if d.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict): continue
            last = m
            if since:
                try:
                    if calendar.timegm(time.strptime(str(d.get("timestamp", ""))[:19], "%Y-%m-%dT%H:%M:%S")) < since: continue   # UTC
                except ValueError: pass
            seen[m.get("id") or len(seen)] = m
    use = {}
    for m in seen.values():
        a = use.setdefault(str(m.get("model")), dict.fromkeys(USE_KEYS, 0))
        for k in USE_KEYS: a[k] += int(m["usage"].get(k) or 0)
    return {"ctx": sum(int(last["usage"].get(k) or 0) for k in USE_KEYS) if last else 0, "model": str(last.get("model")) if last else None, "use": use}

if __name__ == "__main__":
    try: print(json.dumps(usage_scan(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 0)))
    except OSError as e: sys.exit(f"no transcript: {e}")
