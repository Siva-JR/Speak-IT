"""Text fixes applied after transcription. Stdlib only.

  * replacements: "a, b, c => Target" rules (case-insensitive, whole words, longest source first)
  * vocabulary:   fuzzy-correct near-miss spellings of the user's own terms ("postgress ql" -> "PostgreSQL"),
                  using a small phonetic key + spelling similarity. Conservative on purpose: short single-word
                  terms (rust, redis, cargo) are only case-fixed, never fuzzy-matched, so "rest" never becomes "Rust".
Written from scratch for this project."""
import re
from difflib import SequenceMatcher

# ---------------------------------------------------------------- replacements
def parse_rules(lines):
    """['cloud code, clawed code => Claude Code'] -> [(source, target)], longest source first, no cycles."""
    rules = []
    for line in lines:
        if "=>" not in line:
            continue
        srcs, dst = line.split("=>", 1)
        dst = dst.strip()
        for s in srcs.split(","):
            s = " ".join(s.split())
            if s and dst and s.lower() != dst.lower():
                rules.append((s, dst))
    # a rule whose target is another rule's source would chain (a->b->c); drop the later one to avoid loops
    srcs = {s.lower() for s, _ in rules}
    out = []
    for s, d in rules:
        if d.lower() in srcs and d.lower() != s.lower():
            continue
        out.append((s, d))
    out.sort(key=lambda r: -len(r[0]))
    return out


def apply_rules(text, rules):
    for s, d in rules:
        pat = r"(?<![\w'])" + r"\s+".join(re.escape(w) for w in s.split()) + r"(?![\w'])"
        text = re.sub(pat, lambda m: d, text, flags=re.I)
    return text


# ---------------------------------------------------------------- phonetic vocabulary
_SUBS = [("ph", "f"), ("gh", ""), ("ck", "k"), ("tch", "ch"), ("dg", "j"), ("sch", "sk"), ("wh", "w"),
         ("qu", "kw"), ("x", "ks"), ("z", "s"), ("q", "k")]


def pkey(word):
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ""
    w = re.sub(r"^(kn|gn|pn)", "n", w)
    w = re.sub(r"^wr", "r", w)
    for a, b in _SUBS:
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w)
    w = w.replace("c", "k")
    w = re.sub(r"(?<=[aeiou])d(?=[aeiou])|(?<=[aeiou])t(?=[aeiou])", "t", w)  # voiced/unvoiced d,t between vowels
    w = w.replace("d", "t") if len(w) > 3 else w
    head, tail = w[0], re.sub(r"[aeiouyhw]", "", w[1:])
    k = head + tail
    return re.sub(r"(.)\1+", r"\1", k)


def _letters(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


class Vocab:
    def __init__(self, terms):
        self.terms = []
        for t in terms:
            t = " ".join(t.split())
            flat = _letters(t)
            if not flat:
                continue
            fuzzy = len(t.split()) > 1 or len(flat) >= 6
            self.terms.append((t, flat, pkey(flat), len(t.split()), fuzzy))
        self.terms.sort(key=lambda x: -len(x[1]))

    def correct(self, text):
        if not self.terms or not text:
            return text
        toks = [(m.start(), m.end(), m.group()) for m in re.finditer(r"[A-Za-z0-9']+", text)]
        used = [False] * len(toks)
        edits = []                                   # (start, end, replacement)
        for term, _f, _k, nwords, _z in self.terms:   # lock text that is already spelled correctly
            for i in range(len(toks) - nwords + 1):
                if text[toks[i][0]:toks[i + nwords - 1][1]] == term:
                    for j in range(i, i + nwords):
                        used[j] = True
        for term, flat, key, nwords, fuzzy in self.terms:
            lo, hi = (max(1, nwords - 1), nwords + 1) if fuzzy else (nwords, nwords)
            cands = []                               # (score, i, n)
            for n in range(lo, hi + 1):
                for i in range(len(toks) - n + 1):
                    if any(used[i:i + n]):
                        continue
                    if any(text[toks[j][1]:toks[j + 1][0]].strip(" -") for j in range(i, i + n - 1)):
                        continue                     # words must be separated by plain spaces/hyphens only
                    cand = text[toks[i][0]:toks[i + n - 1][1]]
                    cflat = _letters(cand)
                    if cand == term or not cflat:
                        continue
                    if cflat == flat:
                        cands.append((3.0, i, n))    # same letters, different case/spacing -> normalise
                    elif fuzzy and flat not in cflat and cflat not in flat:   # containment = an extra/missing real word
                        lr = SequenceMatcher(None, cflat, flat).ratio()
                        ck = pkey(cflat)
                        kr = SequenceMatcher(None, ck, key).ratio()
                        if (0.7 <= len(cflat) / len(flat) <= 1.4 and lr >= 0.6 and kr >= 0.82
                                and (cflat[0] == flat[0] or ck[:1] == key[:1])):
                            cands.append((lr + kr - (0.05 if n != nwords else 0), i, n))
            for score, i, n in sorted(cands, reverse=True):
                if any(used[i:i + n]):
                    continue
                edits.append((toks[i][0], toks[i + n - 1][1], term))
                for j in range(i, i + n):
                    used[j] = True
        for s, e, r in sorted(edits, reverse=True):
            text = text[:s] + r + text[e:]
        return text
