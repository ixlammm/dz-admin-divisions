"""Compare two normalized administrative-divisions JSON documents and produce a changelog.

Entities are matched across releases by a stable key:
  * `code` (ISO3166-2 for provinces / ONS `ref:ons` for communes) when present and equal,
  * otherwise by `osm_id`,
  * otherwise by normalized lowercased name.

Classification per entity:
  * added          - key present in new but not old
  * removed        - key present in old but not new
  * modified       - key present in both, but some *attribute* changed
  * geometry_only  - key present in both, attributes identical, but geometry_hash changed

The `--show-geometry` option lists geometry-only changes by name; otherwise it records
a count only (boundaries are refined daily in OSM, so geometry diffs are noisy).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Fields that participate in a "real" attribute change (exclude geometry-only).
ATTR_FIELDS = [
    "code",
    "ref",
    "name",
    "name_ar",
    "name_fr",
    "name_ber",
    "name_latin",
    "name_en",
    "wikidata",
    "wikipedia",
    "population",
]


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        v = value.strip()
        return v if v else None
    return value


def _name_key(rec: dict[str, Any]) -> str:
    for k in ("code", "name", "name_ar", "name_fr", "osm_id"):
        v = rec.get(k)
        if v:
            return str(v).strip().lower()
    return str(rec.get("osm_id", "")) + "@" + str(rec.get("code", ""))


def _stable_key(rec: dict[str, Any]) -> str:
    code = _norm(rec.get("code"))
    if code:
        return "c:" + str(code).lower()
    osm_id = rec.get("osm_id")
    if osm_id is not None:
        return "id:" + str(osm_id)
    return "n:" + _name_key(rec)


def _build_indexes(entities: list[dict[str, Any]]) -> tuple[dict, dict]:
    by_key: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[str]] = {}
    for rec in entities:
        key = _stable_key(rec)
        if key in by_key:
            # pick the richer entry (has code / more info)
            if not by_key[key].get("code") and rec.get("code"):
                by_key[key] = rec
            continue
        by_key[key] = rec
        by_name.setdefault(_name_key(rec), []).append(key)
    return by_key, by_name


def _match_entities(
    old: list[dict[str, Any]], new: list[dict[str, Any]]
) -> tuple[list, list, list, list]:
    """Return (added, removed, modified, geometry_only) lists of dicts."""
    old_index, old_names = _build_indexes(old)
    new_index, new_names = _build_indexes(new)

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    geometry_only: list[dict[str, Any]] = []

    for key, new_rec in new_index.items():
        old_rec = old_index.get(key)
        if old_rec is None:
            # fall back to matching by name (in case code/id changed)
            candidate_keys = new_names.get(_name_key(new_rec), [])
            for ckey in candidate_keys:
                if ckey in old_index:
                    old_rec = old_index[ckey]
                    break
        if old_rec is None:
            added.append(new_rec)
            continue

        changed_fields = [
            f
            for f in ATTR_FIELDS
            if _norm(old_rec.get(f)) != _norm(new_rec.get(f))
        ]
        geom_changed = (
            old_rec.get("geometry_hash") != new_rec.get("geometry_hash")
        )
        if changed_fields:
            modified.append(
                {"old": old_rec, "new": new_rec, "changed": changed_fields}
            )
        elif geom_changed:
            geometry_only.append(
                {"old": old_rec, "new": new_rec}
            )

    for key, old_rec in old_index.items():
        if key not in new_index:
            # only remove if not matched by name elsewhere
            if not any(
                _name_key(old_rec) == _name_key(n) for n in new_index.values()
            ):
                removed.append(old_rec)

    return added, removed, modified, geometry_only


def _item_label(rec: dict[str, Any]) -> str:
    """Compact, human-readable label for an entity in the changelog.

    Prefers the clean Latin/French name over the (sometimes multi-script) `name` tag.
    """
    name = (
        rec.get("name_fr")
        or rec.get("name_en")
        or rec.get("name_latin")
        or rec.get("name")
        or rec.get("name_ar")
    )
    code = rec.get("code")
    if name and code:
        return f"{name} ({code})"
    return name or code or str(rec.get("osm_id"))


def _fmt_list(names: list[str]) -> str:
    if not names:
        return ""
    return "- " + "\n- ".join(names)


def _field_label(field: str) -> str:
    return {
        "name": "Name",
        "name_ar": "Name (AR)",
        "name_fr": "Name (FR)",
        "name_ber": "Name (Tamazight)",
        "code": "Code",
        "ref": "Ref",
        "wikidata": "Wikidata",
        "population": "Population",
        "wikipedia": "Wikipedia",
        "name_latin": "Name (Latin)",
        "name_en": "Name (EN)",
    }.get(field, field)


def _render_modified(entries: list[dict[str, Any]], kind: str) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        old, new = entry["old"], entry["new"]
        label = _item_label(new)
        lines.append(f"**{label}**")
        for field in entry["changed"]:
            old_val = _norm(old.get(field))
            new_val = _norm(new.get(field))
            fname = _field_label(field)
            lines.append(
                f"- {fname}: `{old_val}` → `{new_val}`"
            )
    return lines


def compute_changelog(
    old: dict[str, Any] | None, new: dict[str, Any], show_geometry: bool = False
) -> str:
    """Render a markdown changelog comparing `old` (may be None) to `new`."""
    lines: list[str] = []
    version = new.get("version", "?")
    prev_version = (old or {}).get("version", "none")
    lines.append(f"# Algeria Administrative Divisions — {version}")
    lines.append("")
    if old:
        lines.append(f"Comparing against previous release `{prev_version}`.")
    else:
        lines.append("This is the **initial release**.")
    lines.append("")

    out: list[str] = []
    for kind, label in (("provinces", "Provinces"), ("communes", "Communes")):
        old_items = (old or {}).get(kind, []) if old else []
        new_items = new.get(kind, [])

        if not old_items:
            # Initial release: list everything (it is all "new").
            out.append(f"\n## {label} ({len(new_items)})")
            out.append(
                f"Initial release — **{len(new_items)}** {label.lower()} recorded."
            )
            out.append(f"\n### All {label.lower()}")
            out.append(_fmt_list([_item_label(r) for r in new_items]))
            continue

        added, removed, modified, geometry_only = _match_entities(
            old_items, new_items
        )
        out.append(f"\n## {label} ({len(new_items)})")
        out.append(
            f"**+{len(added)} new · -{len(removed)} removed · "
            f"{len(modified)} modified · {len(geometry_only)} geometry-only**"
        )

        if added:
            out.append("\n### Added")
            out.append(_fmt_list([_item_label(r) for r in added]))
        if removed:
            out.append("\n### Removed")
            out.append(_fmt_list([_item_label(r) for r in removed]))
        if modified:
            out.append("\n### Modified")
            out.extend(_render_modified(modified, kind))
        if geometry_only:
            out.append("\n### Geometry-only changes (count)")
            if show_geometry:
                out.append(_fmt_list([_item_label(r["new"]) for r in geometry_only]))
            else:
                out.append(
                    f"{len(geometry_only)} {label.lower()} had boundary updates "
                    "(names/codes unchanged)."
                )

    lines.extend(out)
    return "\n".join(lines).rstrip() + "\n"


def load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
