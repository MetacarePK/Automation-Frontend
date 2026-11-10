"""Finds the automations: every folder in automations/ with a manifest.json.

Adding a new automation = drop a folder with its engine and a manifest.json.
Nothing in the backend or the frontend has to change.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Automation:
    slug: str
    name: str
    description: str
    accent: str
    insurance_id_label: str
    required_fields: dict
    input_fields: list
    output_fields: list
    max_concurrent_runs: int
    engine_dir: Path = field(compare=False)

    def public(self) -> dict:
        return {
            "slug": self.slug, "name": self.name, "description": self.description,
            "accent": self.accent, "insurance_id_label": self.insurance_id_label,
            "required_fields": self.required_fields,
            "input_fields": self.input_fields, "output_fields": self.output_fields,
        }


def _load_one(folder: Path) -> Automation:
    manifest_path = folder / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{manifest_path}: {exc}") from exc
    slug = raw.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ManifestError(f"{manifest_path}: bad or missing slug")
    if slug != folder.name:
        raise ManifestError(f"{manifest_path}: slug {slug!r} must match its folder name {folder.name!r}")
    for key in ("name", "description", "required_fields", "input_fields", "output_fields"):
        if key not in raw:
            raise ManifestError(f"{manifest_path}: missing {key!r}")
    for key, _label, _req in raw["input_fields"] + raw["output_fields"]:
        if not isinstance(key, str):
            raise ManifestError(f"{manifest_path}: field keys must be strings")
    for needed in ("main.py", "patients.py"):
        if not (folder / needed).exists():
            raise ManifestError(f"{folder}: engine file {needed} is missing")
    return Automation(
        slug=slug, name=raw["name"], description=raw["description"],
        accent=raw.get("accent", "#444444"),
        insurance_id_label=raw.get("insurance_id_label", "Insurance ID"),
        required_fields=raw["required_fields"],
        input_fields=raw["input_fields"], output_fields=raw["output_fields"],
        # How many runs of this automation may use its Office Ally login at once. Keep 1 unless
        # separate accounts/sessions have been verified safe.
        max_concurrent_runs=max(1, int(raw.get("max_concurrent_runs", 1))),
        engine_dir=folder)


class Registry:
    def __init__(self, automations_dir: Path):
        self._items: dict[str, Automation] = {}
        self.errors: list[str] = []
        if automations_dir.exists():
            for folder in sorted(p for p in automations_dir.iterdir() if p.is_dir()):
                if not (folder / "manifest.json").exists():
                    continue
                try:
                    item = _load_one(folder)
                except ManifestError as exc:
                    self.errors.append(str(exc))
                    continue
                self._items[item.slug] = item

    def all(self) -> list[Automation]:
        return list(self._items.values())

    def get(self, slug: str) -> Automation | None:
        return self._items.get(slug)

    def slugs(self) -> list[str]:
        return list(self._items)
