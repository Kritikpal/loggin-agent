"""Makes decoded log text safe to store: PostgreSQL text/jsonb reject NUL, and terminal escape codes are noise."""

import re

# Escape sequences: CSI "ESC [ ... final", string sequences (OSC, DCS, SOS, PM, APC) that end at BEL / ESC \ / end of
# line / 4096 chars (an unterminated one must never swallow the lines after it), and two-character escapes.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[\]PX^_][^\x07\x1b\n]{0,4096}(?:\x07|\x1b\\)?|[@-Z\\-_])")
# C0/C1 controls (keeping tab and newline), DEL, lone surrogates, U+2028/2029, BOM, bidi controls (Trojan Source) and
# zero-width characters, and the U+FFFE/FFFF non-characters.
_CONTROL = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff​-‏ -‮⁠-⁤⁦-⁩﻿￾￿]"
)


def sanitize(text: str) -> str:
    """Strip ANSI escape sequences and unsafe or invisible characters (including NUL); tabs, newlines and ordinary
    Unicode stay. Also apply it to strings parsed out of JSON, where \\u0000 and lone surrogates can reappear."""
    return _CONTROL.sub("", _ANSI.sub("", text))
