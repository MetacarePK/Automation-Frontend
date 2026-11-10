"""Read and update an automation's Office Ally login (the `.env` its engine reads).

The engines stay untouched: they read `username=` / `password=` from `.env` in their own folder, and
this module is the only thing that writes it. The password is WRITE-ONLY: it is never returned by the
API, never logged and never put in the audit trail.
"""

import os
import re
from pathlib import Path

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class CredentialError(ValueError):
    pass


def env_path(automation) -> Path:
    return automation.engine_dir / ".env"


def _parse(path: Path) -> list[tuple[str, str, str]]:
    """[(raw_line, key_lower, value)] using the same rules as the engines (first '=' else first ':')."""
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        key = value = ""
        if line and not line.startswith("#"):
            for sep in ("=", ":"):
                if sep in line:
                    key, value = line.split(sep, 1)
                    key, value = key.strip().lower(), value.strip().strip("'\"")
                    break
        out.append((raw, key, value))
    return out


def _names(field) -> set[str]:
    return {field["key"].lower(), *(a.lower() for a in field.get("aliases", []))}


def read_status(automation) -> dict:
    """Current values for non-secret fields and whether each secret field is set."""
    parsed = _parse(env_path(automation))
    fields = []
    for field in automation.credential_fields:
        names = _names(field)
        value = next((v for _, k, v in parsed if k in names and v), "")
        item = {"key": field["key"], "label": field["label"], "secret": bool(field.get("secret")), "is_set": bool(value)}
        if not item["secret"]:
            item["value"] = value
        fields.append(item)
    path = env_path(automation)
    return {"fields": fields, "updated_at": path.stat().st_mtime if path.exists() else None}


def validate_value(label: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise CredentialError(f"{label} cannot be empty.")
    if len(value) > 200:
        raise CredentialError(f"{label} is too long.")
    if _CONTROL.search(value):
        raise CredentialError(f"{label} cannot contain line breaks or control characters.")
    if value != value.strip() or value[0] in "'\"" or value[-1] in "'\"":
        # the engines strip spaces and quote marks around a value, so such a value would be altered silently
        raise CredentialError(f"{label} cannot start or end with a space or a quote mark.")
    return value


def update(automation, values: dict) -> list[str]:
    """Write the given values; fields not mentioned (or a blank secret) are left unchanged.
    Returns the keys that were changed."""
    by_key = {f["key"]: f for f in automation.credential_fields}
    unknown = [k for k in values if k not in by_key]
    if unknown:
        raise CredentialError("Unknown field.")
    changes = {}
    for key, value in values.items():
        field = by_key[key]
        if value in (None, "") and field.get("secret"):
            continue                                   # blank password = keep the current one
        changes[key] = validate_value(field["label"], value)
    if not changes:
        raise CredentialError("Nothing to update.")

    path = env_path(automation)
    parsed = _parse(path)
    replaced = set()
    for key in changes:
        replaced |= _names(by_key[key])
    # keep every other line exactly as it is; drop the old spelling of what changes (user:/pass: etc.)
    lines = [raw for raw, k, _ in parsed if k not in replaced]
    lines += [f"{key}={value}" for key, value in changes.items()]
    text = "\n".join(lines).strip("\n") + "\n"

    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return list(changes)
