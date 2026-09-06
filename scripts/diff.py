"""Compare two normalized administrative-divisions JSON documents and produce a changelog.

Entities are matched across releases with a two-tier strategy (so a redrawn relation —
a new OSM id or a changed/missing national code — is reported as *modified*-like rather
than removed+added):
  1. by `code` (ISO3166-2 for provinces / ONS `ref:ons` for communes) when present,
  2. otherwise by the normalized place name (unique match).

Classification per entity:
  * added          - present in new but matched by no old entity
  * removed        - present in old but matched by no new entity
  * modified       - matched, but a "user-facing" attribute changed (name, wikidata, ...)
  * geometry_only  - matched, attributes identical, but geometry_hash changed (count only)

The `--show-geometry` option lists geometry-only changes by name; otherwise it records
a count only (boundaries are refined daily in OSM, so geometry diffs are noisy).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Fields that count as a user-facing attribute change (a re-tagging of the code/ref
# metadata or a boundary redraw is handled separately: geometry -> geometry-only count).
ATTR_FIELDS = [
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


def _pure_name(rec: dict[str, Any]) -> str:
    """A normalized place-name key used to match entities when code/id differ."""
    for k in ("name_fr", "name_en", "name_latin", "name_ber", "name", "name_ar"):
        v = rec.get(k)
        if v:
            return v.strip().lower()
    return ""


def _classify_pair(
    old_rec: dict[str, Any],
    new_rec: dict[str, Any],
    modified: list[dict[str, Any]],
    geometry_only: list[dict[str, Any]],
) -> None:
    changed = [f for f in ATTR_FIELDS if _norm(old_rec.get(f)) != _norm(new_rec.get(f))]
    if changed:
        modified.append({"old": old_rec, "new": new_rec, "changed": changed})
    elif old_rec.get("geometry_hash") != new_rec.get("geometry_hash"):
        geometry_only.append({"old": old_rec, "new": new_rec})


def _match_entities(
    old: list[dict[str, Any]], new: list[dict[str, Any]]
) -> tuple[list, list, list, list]:
    """Return (added, removed, modified, geometry_only) lists of dicts.

    Two-tier matching so that redrawn entities (a new relation id or a changed/missing
    code, but the same place name) are reported as *modified* rather than removed+added:
      1. match by national code (ISO3166-2 / ONS ``ref:ons``) when present,
      2. match the remainder by normalized place name (unique match only).
    """
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    geometry_only: list[dict[str, Any]] = []

    old_by_code: dict[str, dict[str, Any]] = {}
    old_by_name: dict[str, list[dict[str, Any]]] = {}
    for o in old:
        code = _norm(o.get("code"))
        if code:
            old_by_code[str(code).lower()] = o
        nm = _pure_name(o)
        if nm:
            old_by_name.setdefault(nm, []).append(o)

    new_by_code: dict[str, dict[str, Any]] = {}
    for n in new:
        code = _norm(n.get("code"))
        if code:
            new_by_code[str(code).lower()] = n

    used_old: set[int] = set()
    used_new: set[int] = set()

    # Pass 1: match by the national code (most stable identifier).
    for code, nrec in new_by_code.items():
        orec = old_by_code.get(code)
        if orec is not None:
            _classify_pair(orec, nrec, modified, geometry_only)
            used_old.add(id(orec))
            used_new.add(id(nrec))

    # Pass 2: match the remainder by place name (only a unique old candidate).
    for nrec in new:
        if id(nrec) in used_new:
            continue
        nm = _pure_name(nrec)
        if not nm:
            continue
        candidates = [o for o in old_by_name.get(nm, []) if id(o) not in used_old]
        if len(candidates) == 1:
            _classify_pair(candidates[0], nrec, modified, geometry_only)
            used_old.add(id(candidates[0]))
            used_new.add(id(nrec))

    added = [n for n in new if id(n) not in used_new]
    removed = [o for o in old if id(o) not in used_old]
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


# GitHub release bodies are capped at 125,000 characters, so the (potentially huge)
# full changelog is shipped as an asset while the release body carries a short summary.
def compute_summary(
    old: dict[str, Any] | None, new: dict[str, Any], show_geometry: bool = False
) -> str:
    """A compact release-body summary (counts + a small preview of changes)."""
    version = new.get("version", "?")
    lines: list[str] = [f"# Algeria Administrative Divisions — {version}", ""]
    if old:
        lines.append(f"Comparing against previous release `{(old or {}).get('version')}`.")
    else:
        lines.append("This is the **initial release**.")
    lines.append("")

    for kind, label in (("provinces", "Provinces"), ("communes", "Communes")):
        old_items = (old or {}).get(kind, []) if old else []
        new_items = new.get(kind, [])

        if not old_items:
            lines.append(f"**{len(new_items)}** {label.lower()}.")
            lines.append(f"see `{kind}-{version}.json` for the full list — the full "
                         "changelog is attached.")
            lines.append("")
            continue

        added, removed, modified, geometry_only = _match_entities(old_items, new_items)
        lines.append(f"## {label} ({len(new_items)})")
        lines.append(
            f"+{len(added)} new · -{len(removed)} removed · {len(modified)} modified · "
            f"{len(geometry_only)} geometry-only"
        )
        if added:
            lines.append("\n### New")
            lines.append(_fmt_list([_item_label(r) for r in added[:8]]))
            if len(added) > 8:
                lines.append(f"- … and {len(added) - 8} more")
        if removed:
            lines.append("\n### Removed")
            lines.append(_fmt_list([_item_label(r) for r in removed[:8]]))
            if len(removed) > 8:
                lines.append(f"- … and {len(removed) - 8} more")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
