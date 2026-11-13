"""Learning from corrections. When you correct a dictation (in the dashboard's history, or `dictate fix "..."`), the
words that changed become SUGGESTIONS: a vocabulary term when adding it to vocab.txt would have produced your spelling,
otherwise an exact replacement rule. Nothing is applied until you accept it.

Files: STATE/corrections.jsonl (what you corrected), STATE/suggestions.json (open / accepted / dismissed suggestions).
Also home of the vocab.txt / replacements.txt editing helpers shared by the CLI and the dashboard. Stdlib only."""
import json, re, threading, time
from difflib import SequenceMatcher

import textfix
from common import CONF_DIR, STATE

_lock = threading.Lock()


# ----------------------------------------------------------------------------- vocab.txt / replacements.txt
def list_lines(name):
    f = CONF_DIR / name
    return [l.strip() for l in f.read_text().splitlines()] if f.exists() else []


def live(lines):
    return [l for l in lines if l and not l.startswith("#")]


def list_add(name, line):
    """Append a line unless it is already there (case-insensitive). Returns True if added."""
    with _lock:
        lines = list_lines(name)
        if line.lower() in [l.lower() for l in lines]:
            return False
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        (CONF_DIR / name).write_text("\n".join([l for l in lines if l] + [line]) + "\n")
        return True


def list_remove(name, line):
    with _lock:
        lines = list_lines(name)
        keep = [l for l in lines if l.lower() != line.lower()]
        (CONF_DIR / name).write_text("\n".join(keep) + "\n")
        return len(keep) < len(lines)


# ----------------------------------------------------------------------------- correction -> suggestions
_EDGE = re.compile(r"^[^\w]+|[^\w+#]+$")


def _clean(tok):
    return _EDGE.sub("", tok)


def suggest(original, corrected):
    """[{'kind': 'vocab'|'replace', 'heard', 'fix', 'line'}] for the words that changed between the two texts.
    Insertions/deletions alone (an added comma, a dropped filler) and changes of more than 4 words are ignored."""
    a = [_clean(t) for t in original.split()]
    b = [_clean(t) for t in corrected.split()]
    a = [t for t in a if t]; b = [t for t in b if t]
    out, seen = [], set()
    sm = SequenceMatcher(None, [t.lower() for t in a], [t.lower() for t in b], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace" and i2 - i1 <= 4 and j2 - j1 <= 4:
            pairs = [(" ".join(a[i1:i2]), " ".join(b[j1:j2]))]
        elif op == "equal":                                   # same words, different case: 'github' -> 'GitHub'
            pairs = [(x, y) for x, y in zip(a[i1:i2], b[j1:j2])
                     if x != y and not (x[1:] == y[1:] and x[:1].lower() == y[:1].lower())]   # not just a sentence start
        else:
            continue
        for heard, fix in pairs:
            if not heard or not fix or (heard, fix) in seen:
                continue
            seen.add((heard, fix))
            if textfix.Vocab([fix]).correct(heard) == fix:
                out.append({"kind": "vocab", "heard": heard, "fix": fix, "line": fix})
            else:
                out.append({"kind": "replace", "heard": heard, "fix": fix, "line": f"{heard.lower()} => {fix}"})
    return out


def _load():
    try:
        return json.loads((STATE / "suggestions.json").read_text())
    except (OSError, ValueError):
        return {"items": []}


def _save(d):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / "suggestions.json.tmp"
    tmp.write_text(json.dumps(d, indent=1)); tmp.replace(STATE / "suggestions.json")


def record_correction(dictation_ts, original, corrected, source="dashboard"):
    """Store a correction and merge its suggestions (a repeat of the same fix bumps its count). Returns the new ones."""
    sugs = suggest(original, corrected)
    with _lock:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "corrections.jsonl", "a") as f:
            f.write(json.dumps({"ts": dictation_ts, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source,
                                "original": original, "corrected": corrected}) + "\n")
        d = _load()
        vocab = {l.lower() for l in live(list_lines("vocab.txt"))}
        rules = {l.lower() for l in live(list_lines("replacements.txt"))}
        for s in sugs:
            if s["line"].lower() in (vocab if s["kind"] == "vocab" else rules):
                s["status"] = "already"; continue
            key = (s["kind"], s["line"].lower())
            hit = next((x for x in d["items"] if (x["kind"], x["line"].lower()) == key), None)
            if hit:
                hit["count"] += 1; hit["last"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                if hit["status"] == "dismissed" and hit["count"] >= 3:
                    hit["status"] = "new"                     # you keep making the same fix: ask again
            else:
                n = max([x["id"] for x in d["items"]] + [0]) + 1
                d["items"].append({**s, "id": n, "count": 1, "status": "new",
                                   "first": time.strftime("%Y-%m-%dT%H:%M:%S"), "last": time.strftime("%Y-%m-%dT%H:%M:%S")})
        _save(d)
    return sugs


def corrections():
    """{dictation ts: latest corrected text}."""
    out = {}
    try:
        for l in (STATE / "corrections.jsonl").read_text().splitlines():
            try:
                r = json.loads(l); out[r["ts"]] = r["corrected"]
            except (ValueError, KeyError):
                pass
    except OSError:
        pass
    return out


def suggestions(status="new"):
    return [x for x in _load()["items"] if status is None or x["status"] == status]


def resolve(sid, action):
    """action 'accept' (writes vocab.txt / replacements.txt) or 'dismiss'. Returns the item or None."""
    with _lock:
        d = _load()
        item = next((x for x in d["items"] if x["id"] == int(sid)), None)
        if not item or action not in ("accept", "dismiss"):
            return None
        item["status"] = "accepted" if action == "accept" else "dismissed"
        _save(d)
    if action == "accept":
        list_add("vocab.txt" if item["kind"] == "vocab" else "replacements.txt", item["line"])
    return item
