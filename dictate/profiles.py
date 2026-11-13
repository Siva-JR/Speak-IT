"""Per-app profiles: how dictated text is formatted and typed, depending on the app that has focus.

Built in: `terminal` (shells and terminal agents such as Claude Code) and `chat` (Slack, Discord, ...). Everything else
uses `default`. Override or add profiles in config.toml:

    [profiles.terminal]
    enter_phrases = ["send it"]        # say "... send it" to press Enter after typing
    [profiles.notes]
    apps = ["obsidian", "notion"]
    final_period = "keep"

Standard library only (the daemon imports this; it must stay small)."""
import re

OPTIONS = {
    "apps": [],               # focused-app names this profile applies to (case and punctuation ignored)
    "capitalize": True,       # False: lower-case the first word ("Git status" -> "git status"), never names or "I"
    "final_period": "keep",   # "keep" | "single" (drop it when the text is one sentence) | "strip" (always drop it)
    "trailing_space": None,   # None: use the global trailing_space setting
    "enter_phrases": [],      # phrases that, said at the very end, are removed and press Enter instead
    "type_delay_ms": None,    # None: use the global type_delay_ms
    "output": None,           # None: use the global output ("auto", "clipboard", ...)
}

BUILTIN = {
    "terminal": {
        "apps": ["gnome-terminal-server", "gnome-terminal", "kgx", "ptyxis", "konsole", "kitty", "alacritty",
                 "wezterm", "wezterm-gui", "foot", "tilix", "terminator", "xterm", "urxvt", "ghostty", "warp",
                 "tabby", "hyper", "blackbox", "WindowsTerminal", "wt", "cmd", "powershell", "pwsh", "conhost",
                 "mintty", "iterm2", "Terminal"],
        "capitalize": False,
        "final_period": "single",
    },
    "chat": {
        "apps": ["slack", "discord", "telegram", "telegram-desktop", "signal", "whatsapp", "teams", "ms-teams",
                 "element", "zulip", "mattermost"],
        "final_period": "single",
    },
}

_KEEP_CAPS = {"i", "i'm", "i'll", "i've", "i'd"}


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def load(user_profiles=None):
    """{name: full profile} = defaults <- built-ins <- the user's [profiles.*] tables (keys merged per profile)."""
    out = {"default": dict(OPTIONS)}
    for name, p in BUILTIN.items():
        out[name] = {**OPTIONS, **p}
    for name, p in (user_profiles or {}).items():
        if isinstance(p, dict):
            base = out.get(name, dict(OPTIONS))
            out[name] = {**base, **{k: v for k, v in p.items() if k in OPTIONS}}
    return out


def match(app, profiles):
    """(name, profile) for the focused app. Exact name match wins over a prefix match; unknown apps get 'default'."""
    a = _norm(app)
    if a:
        for exact in (True, False):
            for name, p in profiles.items():
                if name == "default":
                    continue
                for pat in map(_norm, p.get("apps") or []):
                    if pat and (a == pat if exact else (len(pat) >= 4 and a.startswith(pat))):
                        return name, p
    return "default", profiles["default"]


def _strip_enter(text, phrases):
    for ph in sorted((p for p in phrases or [] if p.strip()), key=len, reverse=True):
        pat = r"(?:^|[\s,;:.!?]+)" + r"[\s,]+".join(re.escape(w) for w in ph.split()) + r"[\s.,!?]*$"
        m = re.search(pat, text, flags=re.I)
        if m:
            return text[: m.start()].rstrip(" ,;:"), True
    return text, False


def apply(text, prof, protect=()):
    """Format `text` for a profile. Returns (text, press_enter). `protect`: words never lower-cased (vocabulary)."""
    text, enter = _strip_enter(text.strip(), prof.get("enter_phrases"))
    if not text:
        return "", enter
    fp = prof.get("final_period", "keep")
    if text.endswith(".") and not text.endswith(".."):
        body = text[:-1]
        if fp == "strip" or (fp == "single" and not re.search(r"[.!?]\s", body)):
            text = body
    if not prof.get("capitalize", True):
        m = re.match(r"([A-Z][a-z']*)\b", text)
        if m and m.group(1).lower() not in _KEEP_CAPS and m.group(1) not in set(protect):
            text = text[0].lower() + text[1:]
    return text, enter
