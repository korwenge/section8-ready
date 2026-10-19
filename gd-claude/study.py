"""Study mode: measure how real (rated) levels are built.

Downloads levels from the GD servers and turns each into design numbers:
how long its sections are, how dense the hazards are, how big the breaks
between obstacles are, and so on. Claude reads these and writes what it
learns into the "Design Rules" note in the player's vault.
"""

import statistics
import time
from collections import Counter

import gd_api
from physics import (HEADER_MODES, HEADER_SPEEDS, MODE_PORTALS, ORBS, PADS, SAWS, SOLIDS,
                     SPEED_PORTALS, SPEEDS, SPIKES)

CELL = 30
GAMEPLAY_IDS = (SOLIDS | set(SPIKES) | set(SAWS) | set(PADS) | set(ORBS) | set(MODE_PORTALS)
                | set(SPEED_PORTALS) | {10, 11, 99, 101, 286, 287})
TRIGGER_IDS = {i for i, name in gd_api.OBJECT_NAMES.items() if "trigger" in name}


def _col(x):
    return (x - 15) / CELL


def design_metrics(level_string):
    header_text, *object_texts = level_string.split(";")
    header = gd_api.parse_kv(header_text, ",")
    objs = []
    for text in object_texts:
        raw = gd_api.parse_kv(text, ",")
        try:
            objs.append((int(raw.get("1", 0)), float(raw.get("2", 0)), float(raw.get("3", 0))))
        except ValueError:
            continue
    if not objs:
        return {"error": "empty level"}

    gameplay = [o for o in objs if o[0] in GAMEPLAY_IDS]
    hazards = [o for o in objs if o[0] in SPIKES or o[0] in SAWS]
    length_cols = max((_col(x) for _, x, _ in gameplay), default=0)

    # Time to play through, following speed portals.
    speed = HEADER_SPEEDS.get(int(header.get("kA4") or 0), 1)
    seconds, last_x = 0.0, 0.0
    for oid, x, _ in sorted((o for o in objs if o[0] in SPEED_PORTALS), key=lambda o: o[1]):
        seconds += (x - last_x) / SPEEDS[speed]
        speed, last_x = SPEED_PORTALS[oid], x
    seconds += max(0.0, length_cols * CELL - last_x) / SPEEDS[speed]

    # Gamemode sections.
    sections, mode, start = [], HEADER_MODES.get(int(header.get("kA2") or 0), "cube"), 0.0
    for oid, x, _ in sorted((o for o in objs if o[0] in MODE_PORTALS), key=lambda o: o[1]):
        if MODE_PORTALS[oid] != mode:
            sections.append((mode, start, _col(x)))
            mode, start = MODE_PORTALS[oid], _col(x)
    sections.append((mode, start, length_cols))
    section_lengths = {}
    for m, a, b in sections:
        section_lengths.setdefault(m, []).append(round(b - a))

    # Spike runs (adjacent hazards on the same row) and breaks between hazards.
    rows = {}
    for oid, x, y in hazards:
        if oid in SPIKES:
            rows.setdefault(round(_col(y)), set()).add(round(_col(x)))
    runs = []
    for cols in rows.values():
        ordered = sorted(cols)
        run = 1
        for a, b in zip(ordered, ordered[1:]):
            if b - a == 1:
                run += 1
            else:
                runs.append(run)
                run = 1
        runs.append(run)
    hazard_cols = sorted({round(_col(x)) for _, x, _ in hazards})
    gaps = [b - a for a, b in zip(hazard_cols, hazard_cols[1:]) if b - a > 1]
    per_window = Counter(c // 10 for c in hazard_cols)

    per100 = 100 / max(length_cols, 1)
    return {
        "length_cols": round(length_cols),
        "play_time_seconds": round(seconds, 1),
        "total_objects": len(objs),
        "decoration_share": round(1 - len(gameplay) / len(objs), 2),
        "triggers": sum(1 for o in objs if o[0] in TRIGGER_IDS),
        "hazard_columns_per_100_cols": round(len(hazard_cols) * per100, 1),
        "busiest_10_cols_hazard_columns": max(per_window.values(), default=0),
        "longest_spike_run": max(runs, default=0),
        "spike_runs_of_3_plus": sum(1 for r in runs if r >= 3),
        "median_break_between_hazards_cols": statistics.median(gaps) if gaps else None,
        "orbs_per_100_cols": round(sum(1 for o in objs if o[0] in ORBS) * per100, 1),
        "pads_per_100_cols": round(sum(1 for o in objs if o[0] in PADS) * per100, 1),
        "gamemode_sections": [{"mode": m, "from_col": round(a), "cols": round(b - a)} for m, a, b in sections][:30],
        "section_lengths_by_mode": section_lengths,
        "speed_portals": sum(1 for o in objs if o[0] in SPEED_PORTALS),
    }


AGGREGATE_KEYS = ["length_cols", "play_time_seconds", "decoration_share", "hazard_columns_per_100_cols",
                  "busiest_10_cols_hazard_columns", "longest_spike_run", "median_break_between_hazards_cols",
                  "orbs_per_100_cols", "pads_per_100_cols", "triggers"]


def study(search_type="awarded", difficulty=None, count=5, query="", pause=1.0):
    """Download `count` levels and measure them. `pause` keeps us polite to RobTop's servers."""
    found = gd_api.search_levels(query, search_type, difficulty=difficulty)[:count]
    studied = []
    for i, level in enumerate(found):
        if i:
            time.sleep(pause)
        try:
            summary, level_string = gd_api.download_level(level["id"])
        except Exception as e:  # one broken level shouldn't stop the study
            studied.append({"name": level["name"], "error": str(e)})
            continue
        entry = {k: summary.get(k) or level.get(k) for k in
                 ("id", "name", "creator", "difficulty", "stars", "likes", "rating", "length")}
        entry["creator"] = level.get("creator", entry["creator"])
        entry["metrics"] = design_metrics(level_string) if level_string else {"error": "no level data"}
        studied.append(entry)

    medians = {}
    for key in AGGREGATE_KEYS:
        values = [s["metrics"][key] for s in studied
                  if isinstance(s.get("metrics", {}).get(key), (int, float))]
        if values:
            medians[key] = round(statistics.median(values), 2)
    return {"searched": {"type": search_type, "difficulty": difficulty, "query": query},
            "levels": studied, "medians": medians}
