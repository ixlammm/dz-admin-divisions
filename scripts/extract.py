"""Extract Algerian provinces (wilayas) and communes from an OSM .pbf into normalized JSON.

Uses pyosmium (cross-platform, no external binary / Docker). Only lightweight summaries
are kept (labels, codes, centroid, bbox, geometry hash) — the full geometry is discarded.

Algeria's admin hierarchy in OSM (as found in the Geofabrik extract):
  * wilaya (province)   = admin_level == 4,  ISO3166-2 = DZ-xx
  * daïra (district)    = admin_level == 6   (intermediate; not published here)
  * commune (municip.)  = admin_level == 8

The Geofabrik extract is *not* clipped to Algeria: whole multipolygon relations from
neighbouring countries (Morocco, Tunisia, Libya, Mali, Niger, Mauritania) that touch the
border are included. We therefore:
  * provinces: keep only relations whose ISO3166-2 code starts with "DZ-", and
  * communes : keep only relations whose area centroid lies inside the Algeria country
               boundary (the admin_level=2 relation). The country boundary is assembled
               from its member ways, and a point-in-polygon (even-odd) test is used.

Because the PBF is sorted Type_then_ID (nodes, ways, relations) we read it three times so
we only retain the nodes/ways we actually need:
  1. relations  -> the Algeria country boundary + candidate provinces/communes,
  2. ways       -> node-id lists of the relevant ways,
  3. nodes      -> locations of the relevant nodes.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import osmium

WILAYA_LEVEL = "4"
COMMUNE_LEVEL = "8"

# Tag keys to copy through, mapping OSM key -> field name.
NAME_FIELDS = {
    "name": "name",
    "name:ar": "name_ar",
    "name:fr": "name_fr",
    "name:ber": "name_ber",
    "name:tzm": "name_ber",
    "name:zgh": "name_ber",
    "name:latin": "name_latin",
    "name:en": "name_en",
}

_LEVELS = {WILAYA_LEVEL, COMMUNE_LEVEL}


def _is_algeria(tags: dict[str, str]) -> bool:
    if tags.get("ISO3166-2") == "DZ":
        return True
    name = (tags.get("name") or "").lower()
    name_ar = (tags.get("name:ar") or "")
    return name == "algeria" or name_ar == "الجزائر"


class _RelationPass(osmium.SimpleHandler):
    """Pass 1: collect the country boundary and candidate provinces/communes."""

    def __init__(self) -> None:
        super().__init__()
        self.country_way_ids: set[int] = set()
        self.candidates: list[dict[str, Any]] = []

    def relation(self, r) -> None:
        tags = dict((t.k, t.v) for t in r.tags)
        level = tags.get("admin_level")
        if level == "2" and _is_algeria(tags):
            for m in r.members:
                if m.type == "w":
                    self.country_way_ids.add(m.ref)
            return
        if level not in _LEVELS:
            return
        way_ids = [m.ref for m in r.members if m.type == "w"]
        self.candidates.append({"id": r.id, "tags": tags, "way_ids": way_ids})


class _WayPass(osmium.SimpleHandler):
    """Pass 2: record the node-id list of every relevant way."""

    def __init__(self, wanted: set[int]) -> None:
        super().__init__()
        self.wanted = wanted
        self.way_nodes: dict[int, list[int]] = {}
        self.node_ids: set[int] = set()

    def way(self, w) -> None:
        if w.id not in self.wanted:
            return
        refs = [n.ref for n in w.nodes]
        self.way_nodes[w.id] = refs
        self.node_ids.update(refs)


class _NodePass(osmium.SimpleHandler):
    """Pass 3: record locations of the needed nodes."""

    def __init__(self, wanted: set[int]) -> None:
        super().__init__()
        self.wanted = wanted
        self.locations: dict[int, tuple[float, float]] = {}

    def node(self, n) -> None:
        if n.id in self.wanted and n.location.valid():
            self.locations[n.id] = (n.location.lon, n.location.lat)


# --- geometry helpers (pure python; no shapely) ---

def _assemble_rings(way_node_lists: list[list[int]]) -> list[list[int]]:
    """Chain way node-id sequences into closed rings (multipolygon assembly)."""
    used_way = [False] * len(way_node_lists)
    adj: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for si, nds in enumerate(way_node_lists):
        if len(nds) < 2:
            continue
        adj[nds[0]].append((si, 0))
        adj[nds[-1]].append((si, 1))

    rings: list[list[int]] = []
    for si in range(len(way_node_lists)):
        if used_way[si] or len(way_node_lists[si]) < 2:
            continue
        ring = list(way_node_lists[si])
        used_way[si] = True

        # extend forward from the end of the ring
        cur = ring[-1]
        while True:
            cands = [c for c in adj.get(cur, []) if not used_way[c[0]]]
            if not cands:
                break
            nsi, ndir = cands[0]
            nxt = way_node_lists[nsi] if ndir == 0 else way_node_lists[nsi][::-1]
            ring.extend(nxt[1:])
            used_way[nsi] = True
            cur = ring[-1]
            if cur == ring[0]:
                break

        # if not closed at the front, extend backward
        if ring[0] != ring[-1]:
            cur = ring[0]
            while True:
                cands = [c for c in adj.get(cur, []) if not used_way[c[0]]]
                if not cands:
                    break
                nsi, ndir = cands[0]
                nxt = way_node_lists[nsi] if ndir == 1 else way_node_lists[nsi][::-1]
                prepend = list(reversed(nxt))
                ring = prepend[:-1] + ring
                used_way[nsi] = True
                cur = ring[0]
                if cur == ring[-1]:
                    break

        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def _coords(ring_ids: list[int], node_locs: dict[int, tuple[float, float]]) -> list[tuple[float, float]]:
    pts = []
    for nid in ring_ids:
        loc = node_locs.get(nid)
        if loc:
            pts.append(loc)
    return pts


def _shoelace(pts: list[tuple[float, float]]) -> float:
    area = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _area_centroid(pts: list[tuple[float, float]]) -> tuple[float, float] | None:
    if len(pts) < 4:
        return None
    area = _shoelace(pts)
    if area == 0:
        return None
    cx = cy = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx / (6.0 * area), cy / (6.0 * area))


def _point_in_rings(pt: tuple[float, float], rings: list[list[tuple[float, float]]]) -> bool:
    """Even-odd ray casting across all rings (handles holes)."""
    x, y = pt
    inside = False
    for ring in rings:
        if len(ring) < 3:
            continue
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


# --- record shaping ---

def _wilaya_identity(tags: dict[str, str]) -> str | None:
    """The code used to decide whether a level-4 relation is an Algerian wilaya.

    Prefers the ISO 3166-2 code; falls back to a plain numeric wilaya number
    (e.g. DZ-63 El Aricha is tagged with a numeric ref rather than an ISO code).
    """
    iso = tags.get("ISO3166-2")
    if iso:
        return iso
    ref = tags.get("ref")
    if ref and str(ref).isdigit():
        return str(ref)
    return None


def _is_wilaya(tags: dict[str, str], inside_algeria: bool) -> bool:
    code = _wilaya_identity(tags)
    if code is None:
        return False
    s = str(code)
    if s.startswith("DZ-"):
        return True
    if s.isdigit() and 1 <= int(s) <= 99 and inside_algeria:
        return True
    return False


def _to_num(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(" ", "").replace(",", "")))
    except (ValueError, TypeError):
        return None


# Leading administrative-entity labels stripped from names so we keep only the clean
# place name (OSM often prefixes e.g. "ولاية أدرار", "Wilaya de X", "X Province").
_AR_ENTITY_PREFIX = re.compile(
    r"^\s*(?:ال)?(?:وِلَايَة|ولا[ي]ة|بَلَدِيَّة|بَلَدِية|بلدية|دَايِرَة|دَائِرَة|دائرة|مُقَاطَعَة|مقاطعة)\s+"
)
_LT_ENTITY_PREFIX = re.compile(
    r"^\s*(?:Wilaya|Province|Commune|Municipalit[eé]|Municipality|District|Da[iï]ra|Daira|"
    r"R[eé]gion|Region|Gouvernorat|Gouvernorate|State|Provincia)\s+(?:de\s+|d['’]\s*)?",
    re.IGNORECASE,
)
_LT_ENTITY_SUFFIX = re.compile(
    r"\s+(?:Wilaya|Province|Commune|Municipality|District|Gouvernorat|Gouvernorate|Region)\s*$",
    re.IGNORECASE,
)


def _clean_name(value: str | None) -> str | None:
    """Remove leading/trailing administrative-entity labels from a place name."""
    if not value:
        return value
    s = value.strip()
    s = _AR_ENTITY_PREFIX.sub("", s)
    s = _LT_ENTITY_PREFIX.sub("", s)
    s = _LT_ENTITY_SUFFIX.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _normalize_tags(tags: dict[str, str]) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "code": None,
        "ref": tags.get("ref"),
        "name": None,
        "name_ar": None,
        "name_fr": None,
        "name_ber": None,
        "name_latin": None,
        "name_en": None,
        "wikidata": tags.get("wikidata"),
        "wikipedia": tags.get("wikipedia"),
        "population": _to_num(tags.get("population")),
    }
    for tag_key, field in NAME_FIELDS.items():
        if tag_key in tags and tags[tag_key] and not rec[field]:
            rec[field] = _clean_name(tags[tag_key])
    code = tags.get("ISO3166-2") or tags.get("ref:ons") or tags.get("ref")
    rec["code"] = code
    if tags.get("ISO3166-2"):
        rec["iso3166_2"] = tags["ISO3166-2"]
    if tags.get("ref:ons"):
        rec["ons_code"] = tags["ref:ons"]
    return rec


def _bbox(coords: list[tuple[float, float]]) -> dict[str, float]:
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return {
        "min_lon": round(min(lons), 6),
        "min_lat": round(min(lats), 6),
        "max_lon": round(max(lons), 6),
        "max_lat": round(max(lats), 6),
    }


def _centroid_of(pts: list[tuple[float, float]], bbox: dict[str, float]) -> dict[str, float]:
    c = _area_centroid(pts)
    if c is None:
        c = ((bbox["min_lon"] + bbox["max_lon"]) / 2.0,
             (bbox["min_lat"] + bbox["max_lat"]) / 2.0)
    return {"lat": round(c[1], 6), "lon": round(c[0], 6)}


def _entity_centroid(
    way_lists: list[list[int]],
    locs: dict[int, tuple[float, float]],
    bbox: dict[str, float],
) -> tuple[float, float]:
    """Best-effort area centroid of the entity's largest assembled ring (or bbox center)."""
    best: list[tuple[float, float]] | None = None
    best_area = 0.0
    for rid in _assemble_rings(way_lists):
        pts = _coords(rid, locs)
        if len(pts) < 4:
            continue
        area = abs(_shoelace(pts))
        if area > best_area:
            best_area = area
            best = pts

    if best is not None:
        c = _area_centroid(best)
        if c is not None:
            return c
    return ((bbox["min_lon"] + bbox["max_lon"]) / 2.0,
            (bbox["min_lat"] + bbox["max_lat"]) / 2.0)


def _geometry_hash(coords: list[tuple[float, float]], precision: int = 6) -> str:
    points = sorted({(round(lo, precision), round(la, precision)) for lo, la in coords})
    blob = ";".join(f"{lo:.{precision}f}|{la:.{precision}f}" for lo, la in points)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def extract_from_pbf(pbf_path: Path) -> dict[str, Any]:
    rel_pass = _RelationPass()
    rel_pass.apply_file(str(pbf_path))

    needed = set(rel_pass.country_way_ids)
    for c in rel_pass.candidates:
        needed.update(c["way_ids"])

    way_pass = _WayPass(needed)
    way_pass.apply_file(str(pbf_path))

    node_pass = _NodePass(way_pass.node_ids)
    node_pass.apply_file(str(pbf_path))

    locs = node_pass.locations

    # Assemble the Algeria country boundary for containment tests.
    country_rings_ids = _assemble_rings(
        [way_pass.way_nodes[w] for w in rel_pass.country_way_ids if w in way_pass.way_nodes]
    )
    country_rings = [_coords(r, locs) for r in country_rings_ids if len(r) >= 4]
    country_rings = [r for r in country_rings if _shoelace(r + [r[0]]) != 0]

    provinces: list[dict[str, Any]] = []
    communes: list[dict[str, Any]] = []

    for cand in rel_pass.candidates:
        tags = cand["tags"]
        way_lists = [way_pass.way_nodes.get(wid) for wid in cand["way_ids"]]
        way_lists = [wl for wl in way_lists if wl]
        if not way_lists:
            continue

        coords = []
        for wl in way_lists:
            for nid in wl:
                loc = locs.get(nid)
                if loc:
                    coords.append(loc)
        if not coords:
            continue

        level = tags["admin_level"]
        bbox = _bbox(coords)
        centroid_lon, centroid_lat = _entity_centroid(way_lists, locs, bbox)
        inside = True
        if country_rings:
            inside = _point_in_rings((centroid_lon, centroid_lat), country_rings)

        if level == WILAYA_LEVEL:
            # Provinces: keep real wilayas (DZ-xx, or a numeric wilaya number inside
            # Algeria like DZ-63 El Aricha). Excludes neighbour-country regions whose
            # centroids happen to touch the (imprecise) Algeria polygon.
            if not _is_wilaya(tags, inside):
                continue
        else:  # COMMUNE_LEVEL
            # Communes: must lie inside Algeria.
            if not inside:
                continue

        rec = _normalize_tags(tags)
        rec["osm_id"] = cand["id"]
        rec["bbox"] = bbox
        rec["centroid"] = {"lat": round(centroid_lat, 6), "lon": round(centroid_lon, 6)}
        rec["geometry_hash"] = _geometry_hash(coords)

        # Normalize wilaya codes that came through as bare numbers (e.g. DZ-63 El Aricha).
        if level == WILAYA_LEVEL and rec["code"] and str(rec["code"]).isdigit():
            rec["code"] = "DZ-" + str(rec["code"]).zfill(2)
            rec["iso3166_2"] = rec["code"]
        if level == WILAYA_LEVEL:
            provinces.append(rec)
        else:
            communes.append(rec)

    provinces.sort(key=lambda r: (str(r["code"] or r["name"] or ""), r["osm_id"]))
    communes.sort(key=lambda r: (str(r["code"] or r["name"] or ""), r["osm_id"]))

    return {"provinces": provinces, "communes": communes}


def write_json(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def extract_pbf(
    pbf_path: Path,
    workdir: Path | None = None,
    out_dir: Path | None = None,
    keep_intermediate: bool = False,
) -> dict[str, Any]:
    """Compatibility wrapper around extract_from_pbf."""
    return extract_from_pbf(pbf_path)
