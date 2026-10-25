"""Small geographic helpers. Pure module: no SPADE imports."""
from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088  # IUGG mean Earth radius

# Modelling assumption: one resource depot at Anuradhapura city (OpenStreetMap place node).
# See docs/data_assumptions.md.
DEPOT_NAME = "Anuradhapura"
DEPOT_LAT = 8.3350
DEPOT_LON = 80.4106


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (straight-line) distance in km. Not a road distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def distance_to_depot_km(lat: float, lon: float) -> float:
    return haversine_km(lat, lon, DEPOT_LAT, DEPOT_LON)
