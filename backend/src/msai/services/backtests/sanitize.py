"""Sanitize raw worker exception messages before surfacing to clients.

Strips:
- Absolute container paths ``/app/...`` -> ``<DATA_ROOT>/...`` or ``<APP>/...``
- Absolute home paths ``/Users/...`` / ``/home/...`` -> ``<HOME>``
- Stack-trace ``File "...", line N`` bookkeeping (keeps the final exception)
  Also handles ``SyntaxError``-style frames that lack the ``in <func>`` suffix.
- DSN-shaped connection strings (``postgresql://user:pass@host:port/db``,
  ``redis://:pass@host``, etc.) that arrive inside SQLAlchemy or redis errors.
- JWT-shaped triples + common secret patterns -> ``<redacted>``

Truncates to 1 KB. Does NOT try to defeat a determined adversary --
this is single-user box hygiene, not multi-tenant security.
"""

from __future__ import annotations

import re

_MAX_LEN = 1024

# Order matters -- more-specific patterns first.
_DATA_ROOT = re.compile(r"/app/data(?=/|\b)")
_APP_ROOT = re.compile(r"/app(?=/|\b)")
_HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s:]+")
# Traceback frames come in two shapes:
#   1. Runtime frame:  `  File "path", line N, in <func>\n    <src-line>\n`
#   2. SyntaxError frame (no "in <func>" suffix):
#        `  File "path", line N\n    def bad(:\n           ^\n`
# Both should be stripped before the path-prefix rules run so we don't
# leak source paths or source-context lines through to the UI.
_TRACEBACK_FILE_LINE = re.compile(
    r'\s*File "[^"]+", line \d+(?:, in [^\n]+)?\n(?:[^\n]*\n)?(?:\s*\^+\s*\n)?'
)
_TRACEBACK_HEADER = re.compile(r"^Traceback \(most recent call last\):\s*\n", re.MULTILINE)
# DSN with embedded credentials, e.g.
#   postgresql+asyncpg://user:pass@host:5432/db
#   redis://:secret@host:6379/0
#   mongodb://user:pass@host:27017
# The userinfo portion (before `@`) is the sensitive part. We rewrite the
# whole DSN to `<scheme>://<redacted>` to kill both creds + host leak.
_DSN_WITH_CREDS = re.compile(
    # scheme://[user]:pass@host — the user half may be empty (redis://:pass@...).
    # Password half can be anything non-whitespace except `@` / `/` up to the `@`.
    r"\b([a-z][a-z0-9+.\-]*?)://[^\s@/]*:[^\s@/]*@[^\s]+",
    flags=re.IGNORECASE,
)
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{8,}")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}")
_SECRET_KV = re.compile(
    r"(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*['\"]?[A-Za-z0-9._\-]{8,}['\"]?"
)


def sanitize_public_message(raw: str | None) -> str | None:
    """Public-safe version of a raw worker exception message.

    ``None`` passes through unchanged. Empty string -> empty string.
    """
    if raw is None:
        return None
    s = raw

    s = _TRACEBACK_FILE_LINE.sub("", s)
    s = _TRACEBACK_HEADER.sub("", s)
    s = _DATA_ROOT.sub("<DATA_ROOT>", s)
    s = _APP_ROOT.sub("<APP>", s)
    s = _HOME_PATH.sub("<HOME>", s)
    s = _DSN_WITH_CREDS.sub(r"\1://<redacted>", s)
    s = _JWT.sub("<redacted>", s)
    s = _BEARER.sub("<redacted>", s)
    s = _SECRET_KV.sub(r"\1=<redacted>", s)
    s = s.strip()

    if len(s) > _MAX_LEN:
        s = s[: _MAX_LEN - 3] + "..."
    return s
