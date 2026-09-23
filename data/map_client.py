"""
Map / elevation data client.

Sources (in priority order):
  1. Open-Elevation API (free, no key, self-hostable)
  2. GPX file provided by user (most accurate for a specific course)

The primary use-case is taking a list of lat/lon points (e.g. from a GPX
or from a course URL) and returning enriched GeoPoints with elevation.
"""

import math
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import requests

from data.models import GeoPoint, CourseSegment, SegmentType

OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
BATCH_SIZE = 100      # open-elevation max per request
EARTH_R_KM = 6371.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


class MapClient:

    # ─────────────────────────────────────────────
    # Elevation lookup
    # ─────────────────────────────────────────────

    @staticmethod
    def enrich_elevation(
        points: list[tuple[float, float]],  # [(lat, lon), ...]
        source: str = "open-elevation",
    ) -> list[float]:
        """
        Fetch elevation for a list of (lat, lon) pairs.
        Returns a parallel list of elevations in metres.
        Falls back to 0.0 on error for any individual point.
        """
        elevations: list[float] = []

        for i in range(0, len(points), BATCH_SIZE):
            batch = points[i : i + BATCH_SIZE]
            payload = {"locations": [{"latitude": lat, "longitude": lon}
                                     for lat, lon in batch]}
            try:
                resp = requests.post(
                    OPEN_ELEVATION_URL,
                    json=payload,
                    timeout=10,
                )
                resp.raise_for_status()
                results = resp.json()["results"]
                elevations.extend(r["elevation"] for r in results)
            except Exception as exc:
                print(f"[map] elevation fetch failed for batch {i}: {exc}")
                elevations.extend([0.0] * len(batch))
            time.sleep(0.3)

        return elevations

    # ─────────────────────────────────────────────
    # GPX parsing
    # ─────────────────────────────────────────────

    @staticmethod
    def load_gpx(path: Path) -> list[GeoPoint]:
        """
        Parse a standard GPX file into GeoPoints.
        Elevation is taken from <ele> tags if present; otherwise fetched.
        Distance is computed cumulatively from successive points.
        """
        ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
        tree = ET.parse(path)
        root = tree.getroot()

        # Support both <trkpt> (track) and <rtept> (route)
        trkpts = root.findall(".//gpx:trkpt", ns) or root.findall(".//gpx:rtept", ns)

        raw: list[tuple[float, float, Optional[float]]] = []
        for pt in trkpts:
            lat = float(pt.attrib["lat"])
            lon = float(pt.attrib["lon"])
            ele_el = pt.find("gpx:ele", ns)
            ele = float(ele_el.text) if ele_el is not None else None
            raw.append((lat, lon, ele))

        # Fetch missing elevations in one batch
        missing_idxs = [i for i, (_, _, e) in enumerate(raw) if e is None]
        if missing_idxs:
            fetched = MapClient.enrich_elevation(
                [(raw[i][0], raw[i][1]) for i in missing_idxs]
            )
            for idx, elev in zip(missing_idxs, fetched):
                raw[idx] = (raw[idx][0], raw[idx][1], elev)

        # Build GeoPoints with cumulative distance
        geopoints: list[GeoPoint] = []
        cum_dist = 0.0
        for i, (lat, lon, ele) in enumerate(raw):
            if i > 0:
                prev = raw[i - 1]
                cum_dist += _haversine_km(prev[0], prev[1], lat, lon)
            geopoints.append(GeoPoint(
                lat=lat, lon=lon,
                elevation=ele or 0.0,
                distance=cum_dist,
            ))

        return geopoints

    # ─────────────────────────────────────────────
    # Course segmentation
    # ─────────────────────────────────────────────

    @staticmethod
    def segment_course(
        points: list[GeoPoint],
        window_km: float = 0.5,
    ) -> list[CourseSegment]:
        """
        Slide a window over the course and classify each stretch by gradient.
        Returns non-overlapping CourseSegments covering the full distance.
        """
        if not points:
            return []

        def _classify(grad: float) -> SegmentType:
            if grad > 10:    return SegmentType.WALL
            if grad > 5:     return SegmentType.STEEP
            if grad > 2:     return SegmentType.MODERATE
            if grad < -1:    return SegmentType.DESCENT
            return SegmentType.FLAT

        segments: list[CourseSegment] = []
        total_km = points[-1].distance

        km = 0.0
        while km < total_km:
            window_end = min(km + window_km, total_km)

            in_window = [p for p in points if km <= p.distance < window_end]
            if not in_window:
                km = window_end
                continue

            elev_start = in_window[0].elevation
            elev_end   = in_window[-1].elevation
            dist_m     = (window_end - km) * 1000
            gradient   = ((elev_end - elev_start) / dist_m * 100) if dist_m > 10 else 0.0
            avg_elev   = sum(p.elevation for p in in_window) / len(in_window)

            segments.append(CourseSegment(
                start_km=round(km, 3),
                end_km=round(window_end, 3),
                type=_classify(gradient),
                avg_gradient=round(gradient, 2),
                avg_elevation=round(avg_elev, 1),
            ))
            km = window_end

        # Merge consecutive segments of the same type (cleaner output)
        return _merge_segments(segments)


def _merge_segments(segs: list[CourseSegment]) -> list[CourseSegment]:
    if not segs:
        return []
    merged = [segs[0]]
    for s in segs[1:]:
        last = merged[-1]
        if s.type == last.type:
            merged[-1] = CourseSegment(
                start_km=last.start_km,
                end_km=s.end_km,
                type=last.type,
                avg_gradient=round((last.avg_gradient + s.avg_gradient) / 2, 2),
                avg_elevation=round((last.avg_elevation + s.avg_elevation) / 2, 1),
            )
        else:
            merged.append(s)
    return merged
