"""Names (and rough sizes) of GD objects, so Claude can pick real deco.

The full list (~4000 objects) comes from the gd-info-explorer project and is
downloaded on first use, then cached. Without internet we fall back to a
small built-in list, and objects the player already uses always count too.
"""

import csv
import io
import re
import urllib.request
from pathlib import Path

CSV_URL = "https://raw.githubusercontent.com/flowvix/gd-info-explorer/main/src/lib/objects.csv"

BUILTIN = {
    1: "Black Gradient Square", 8: "Black Gradient Spike", 39: "Small Spike", 211: "Colored Inner Square",
    917: "One-Sixteenth Colored Block", 503: "Straight Medium Glow", 1888: "Large Glow Orb",
    1764: "Tiny Circle", 1765: "Tiny Cross", 1766: "Tiny Triangle", 1767: "Tiny Rectangle", 1768: "Tiny Arrow",
    899: "Color Trigger", 901: "Move Trigger", 1007: "Alpha Trigger", 1006: "Pulse Trigger",
    1340: "Thin Invisible Outline", 1343: "Thick Invisible Outline",
}

# Object kinds by id, for drawing. Everything else counts as decoration.
SOLID_IDS = {1, 2, 3, 4, 5, 6, 7, 40, 62, 63, 64, 65, 66, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78,
             80, 81, 82, 83, 90, 91, 92, 93, 94, 95, 96, 116, 117, 118, 119, 120, 121, 122, 146, 160,
             161, 162}
HAZARD_IDS = {8, 39, 88, 89, 98, 103}  # plus anything named like a hazard, see kind()
PIXEL_IDS = {211, 917}


class Catalog:
    def __init__(self, cache_dir=None, download=True):
        self.names = dict(BUILTIN)
        self.source = "built-in list"
        cache = Path(cache_dir) / "objects.csv" if cache_dir else None
        text = None
        if cache and cache.exists():
            text = cache.read_text(encoding="utf-8")
        elif download:
            try:
                with urllib.request.urlopen(CSV_URL, timeout=8) as resp:
                    text = resp.read().decode("utf-8")
                if cache:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(text, encoding="utf-8")
            except OSError:
                text = None
        if text:
            for row in csv.reader(io.StringIO(text)):
                if len(row) >= 2 and row[0].strip().isdigit():
                    self.names[int(row[0])] = row[1].strip()
            self.source = f"{len(self.names)} objects"

    def name(self, obj_id):
        return self.names.get(obj_id, f"object {obj_id}")

    def kind(self, obj_id):
        if obj_id in SOLID_IDS:
            return "solid"
        if obj_id in HAZARD_IDS:
            return "hazard"
        if obj_id in PIXEL_IDS:
            return "pixel"
        name = self.names.get(obj_id, "")
        if any(w in name for w in ("Spike", "Saw", "Hazard")) and not any(
                w in name for w in ("Decorative", "Fake")):
            return "hazard"
        if "Trigger" in name:
            return "trigger"
        if "Portal" in name or "Orb" in name and "Glow" not in name or "Pad" in name:
            return "gameplay"
        return "deco"

    def size(self, obj_id):
        """Rough unscaled (width, height) in GD units, guessed from the name."""
        name = self.names.get(obj_id, "")
        if obj_id == 917 or "Sixteenth" in name:
            return 7.5, 7.5
        if "Tiny" in name:
            return 10, 10
        if "Quarter" in name or "Small" in name:
            return 15, 15
        if "Half" in name or "Slab" in name:
            return 30, 15
        if "Wide Slope" in name:
            return 60, 30
        if "Large" in name or "Big" in name:
            return 60, 60
        return 30, 30

    def search(self, query, limit=40):
        """Objects whose names contain all the words in the query."""
        words = [w for w in re.split(r"\W+", query.lower()) if w]
        hits = [(i, n) for i, n in self.names.items() if all(w in n.lower() for w in words)]
        if not hits and words:  # fall back to any word matching
            hits = [(i, n) for i, n in self.names.items() if any(w in n.lower() for w in words)]
        return [{"id": i, "name": n} for i, n in sorted(hits)[:limit]]
