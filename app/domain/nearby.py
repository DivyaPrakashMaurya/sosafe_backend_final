"""Nearby safe places: what is looked for, how the question is asked of OpenStreetMap, and how the answer is
read. No network here, so all of it is tested exactly.

Privacy: OpenStreetMap's server is asked about a ~1 km square around the person, named by its centre. It never
gets the person's own position, and it cannot tell one person in the square from another. The person's exact
position is only used here, to measure how far each place is.

Cost: nothing here is paid. One question per square and GROUP of places (all the emergency services in one go, all
the roadside ones in another) fills every category of the group, so tapping Hospital, Police and Fire in the same
streets is one lookup, shared by everyone in the square.
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

# What each kind of place is on the map: (key, value) pairs; a value starting with "~" is a regular expression.
CATEGORIES: dict[str, tuple[tuple[str, str], ...]] = {
    "hospital": (("amenity", "hospital"), ("healthcare", "hospital")),
    "police": (("amenity", "police"),),
    "fire_station": (("amenity", "fire_station"),),
    "fuel": (("amenity", "fuel"),),
    "mechanic": (("shop", "~car_repair|motorcycle_repair"),),
    "toll": (("barrier", "toll_booth"),),
    "parking": (("amenity", "parking"),),
    "atm": (("amenity", "atm"),),
    "restaurant": (("amenity", "restaurant"),),
}

CATEGORY_NAMES = tuple(CATEGORIES)

# Asked together: one question per square and group. Toll booths are on their own because they are wanted from
# far away (a highway is not at the person's door) and would otherwise make the question heavy for everyone.
GROUPS: dict[str, tuple[str, ...]] = {
    "emergency": ("hospital", "police", "fire_station"),
    "roadside": ("fuel", "mechanic", "parking", "atm", "restaurant"),
    "highway": ("toll",),
}


# How far each group may be asked for. A toll booth is on a highway, so it is looked for from far away; asking for
# 30 km of restaurants would be a very heavy question for a free public server.
MAX_RADIUS_M = {"emergency": 10_000, "roadside": 10_000, "highway": 30_000}


def group_of(category: str) -> str:
    for group, members in GROUPS.items():
        if category in members:
            return group
    raise KeyError(category)


# The area asked about is a square this many degrees wide (about 1.1 km): everyone inside it shares one answer,
# which is what makes one lookup serve many people and keeps the exact position off the wire.
CELL_DEGREES = 0.01

# From the centre of a cell to its farthest corner is under 0.8 km (between 8 and 37 degrees north); the
# question is asked this much wider than the radius, so a person anywhere in the cell has the whole radius covered.
CELL_MARGIN_M = 900

# What one answer may hold per kind of place (the busy kinds are capped harder, so they cannot crowd out a rare
# one in the same group), and what is sent back.
FETCH_LIMIT = {"parking": 50, "restaurant": 50, "atm": 50}
DEFAULT_FETCH_LIMIT = 40
RESULT_LIMIT = 20

EARTH_RADIUS_M = 6_371_000.0


def cell_of(lat: float, lng: float) -> tuple[float, float]:
    """The centre of the ~1.1 km square the position is in (3 decimals)."""
    return (
        round(math.floor(lat / CELL_DEGREES) * CELL_DEGREES + CELL_DEGREES / 2, 3),
        round(math.floor(lng / CELL_DEGREES) * CELL_DEGREES + CELL_DEGREES / 2, 3),
    )


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_lat, d_lng = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _tag_filter(key: str, value: str) -> str:
    return f'["{key}"~"{value[1:]}"]' if value.startswith("~") else f'["{key}"="{value}"]'


def build_group_query(group: str, cell_lat: float, cell_lng: float, radius_m: int, timeout_s: int = 7) -> str:
    """The Overpass QL question for every kind of place in `group` within `radius_m` + the cell margin of the cell
    centre. Each kind has its own output statement (and its own limit)."""
    around = f"(around:{int(radius_m) + CELL_MARGIN_M},{cell_lat:.3f},{cell_lng:.3f})"
    parts = [f"[out:json][timeout:{timeout_s}];"]
    for category in GROUPS[group]:
        union = "".join(f"nwr{_tag_filter(k, v)}{around};" for k, v in CATEGORIES[category])
        limit = FETCH_LIMIT.get(category, DEFAULT_FETCH_LIMIT)
        parts.append(f"({union})->.{category};.{category} out tags center qt {limit};")
    return "".join(parts)


def _matches(tags: dict[str, Any], category: str) -> bool:
    for key, value in CATEGORIES[category]:
        have = tags.get(key)
        if not isinstance(have, str):
            continue
        if value.startswith("~"):
            if re.fullmatch(value[1:], have):
                return True
        elif have == value:
            return True
    return False


def categorize(tags: dict[str, Any], group: str) -> Optional[str]:
    """Which kind of the group this is on the map (the first that fits), or None."""
    for category in GROUPS[group]:
        if _matches(tags, category):
            return category
    return None


def _first_phone(tags: dict[str, Any]) -> Optional[str]:
    for key in ("phone", "contact:phone", "contact:mobile", "emergency:phone"):
        raw = tags.get(key)
        if not isinstance(raw, str):
            continue
        # "+91 11 2345 6789; +91 98100 00000" is two numbers: the first one is the one to dial.
        first = raw.split(";")[0].strip()
        digits = "".join(ch for ch in first if ch.isdigit() or ch == "+")
        if sum(ch.isdigit() for ch in digits) >= 3:
            return first
    return None


def _address(tags: dict[str, Any]) -> Optional[str]:
    full = tags.get("addr:full")
    if isinstance(full, str) and full.strip():
        return full.strip()
    street = " ".join(str(tags[k]).strip() for k in ("addr:housenumber", "addr:street") if tags.get(k))
    area = tags.get("addr:suburb") or tags.get("addr:neighbourhood") or tags.get("addr:place")
    city = tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village")
    parts = [str(p).strip() for p in (street, area, city) if p and str(p).strip()]
    return ", ".join(parts) or None


def _name(tags: dict[str, Any]) -> str:
    for key in ("name", "name:en", "official_name", "operator", "brand"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return ""


def _position(element: dict[str, Any]) -> Optional[tuple[float, float]]:
    lat, lng = element.get("lat"), element.get("lon")
    if lat is None or lng is None:
        center = element.get("center")
        if isinstance(center, dict):
            lat, lng = center.get("lat"), center.get("lon")
    if isinstance(lat, bool) or isinstance(lng, bool):
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return float(lat), float(lng)


def _has_cng(tags: dict[str, Any], name: str) -> bool:
    # A pump is a CNG pump when the map says it sells CNG, or it is called one.
    return tags.get("fuel:cng") == "yes" or "cng" in name.lower()


def parse_elements(elements: list[Any], group: str) -> dict[str, list[dict[str, Any]]]:
    """The places in an Overpass answer, by kind, in the shape the app reads (without distances: those are the
    person's own and are worked out later). Anything without a position, or not one of the group's kinds, is
    dropped; the same place seen twice (as a point and as a building, or in two statements) is kept once."""
    out: dict[str, list[dict[str, Any]]] = {category: [] for category in GROUPS[group]}
    seen: set[tuple[str, str, float, float]] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        at = _position(element)
        if at is None:
            continue
        tags = element.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        category = categorize(tags, group)
        if category is None:
            continue
        name = _name(tags)
        key = (category, name.lower(), round(at[0], 4), round(at[1], 4))
        if key in seen:
            continue
        seen.add(key)
        hours = tags.get("opening_hours")
        place: dict[str, Any] = {
            "name": name,
            "category": category,
            "lat": round(at[0], 6),
            "lng": round(at[1], 6),
            "phone": _first_phone(tags),
            "address": _address(tags),
            "open_24h": True if isinstance(hours, str) and hours.strip() == "24/7" else None,
        }
        if category == "hospital" and tags.get("emergency") == "yes":
            place["emergency"] = True
        if category == "fuel" and _has_cng(tags, name):
            place["cng"] = True
        out[category].append(place)
    return out


def nearest(places: list[dict[str, Any]], lat: float, lng: float, radius_m: int, limit: int = RESULT_LIMIT) -> list[dict[str, Any]]:
    """The places within `radius_m` of the person's own position, nearest first, each with its distance."""
    measured = []
    for place in places:
        d = distance_m(lat, lng, place["lat"], place["lng"])
        if d <= radius_m:
            measured.append({**place, "distance_m": int(round(d))})
    measured.sort(key=lambda p: (p["distance_m"], p["name"]))
    return measured[:limit]
