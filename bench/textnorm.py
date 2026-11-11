"""Text normalisation applied identically to reference and hypothesis before WER."""
import re

_FILLERS = {"um", "uh", "umm", "uhh", "hmm", "mm", "erm", "ah", "eh"}


def _spell_numbers(t: str) -> str:
    """Write digits as words so '10' and 'ten' score as the same word."""
    from num2words import num2words

    def rep(m):
        raw = m.group(0).replace(",", "")
        try:
            return " " + num2words(float(raw) if "." in raw else int(raw)) + " "
        except Exception:
            return m.group(0)
    t = t.replace("%", " percent ").replace("$", " dollars ")
    return re.sub(r"\d[\d,]*\.?\d*", rep, t)


def normalize(text: str, spell_numbers: bool = True) -> str:
    t = text.lower().replace("’", "'")
    if spell_numbers:
        t = _spell_numbers(t)
    t = t.replace("-", " ")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)      # drop punctuation
    t = re.sub(r"\s+", " ", t).strip()
    words = [w for w in t.split(" ") if w and w not in _FILLERS]
    return " ".join(words)
