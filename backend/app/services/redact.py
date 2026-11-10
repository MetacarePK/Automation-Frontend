"""Keep secrets out of logs. Applied to every line the runner prints before it is stored."""

import re

_KEY = (r"(?:password|passwd|pwd|secret|token|api[_-]?key|session[_-]?id|auth[_-]?state|pass)")
# password: abc / password=abc / "password": "abc" / 'token': 'abc' / password_hash=abc  (but not "passed: 5")
_PAIR = re.compile(rf"""(?ix)(["']?\b{_KEY}(?:[_-]\w+)?["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;&]+)""")
# cookies and authorization headers carry several values: hide the rest of the line
_WHOLE_LINE = re.compile(r"(?im)\b((?:set-)?cookie|authorization)(\s*[:=]\s*).*$")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_OPAQUE = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")          # long opaque strings look like tokens
MASK = "***"


def redact(text: str, secrets=()) -> str:
    """`secrets` are exact values to hide wherever they appear (e.g. the Office Ally password)."""
    for value in secrets:
        if value and len(value) >= 3:
            text = text.replace(value, MASK)
    text = _WHOLE_LINE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    text = _PAIR.sub(lambda m: m.group(1) + MASK, text)
    text = _BEARER.sub(lambda m: m.group(1) + " " + MASK, text)
    return _OPAQUE.sub(MASK, text)


def read_env_secrets(env_path) -> list[str]:
    """Every value in an engine's .env (user:/pass: or KEY=value) - all of them are treated as secret."""
    values = []
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in ("=", ":"):
                if sep in line:
                    values.append(line.split(sep, 1)[1].strip().strip("'\""))
                    break
    except OSError:
        pass
    return [v for v in values if v]
