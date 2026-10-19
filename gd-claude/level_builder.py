"""Turns Claude's grid-based level designs into real GD objects, and back.

Claude works on a grid: one cell = one block (30 GD units). `col` 0 is where
the player starts, `row` 0 sits on the ground. This module converts those to
GD object strings ("1,8,2,315,3,15") and draws ASCII previews.
"""

CELL = 30

# name -> (object id, y offset in GD units, preview char)
PALETTE = {
    "block": (1, 0, "#"),
    "spike": (8, 0, "^"),
    "small_spike": (39, -8, "v"),
    "saw": (88, 0, "@"),
    "yellow_orb": (36, 0, "o"),
    "pink_orb": (141, 0, "o"),
    "blue_orb": (84, 0, "o"),
    "green_orb": (1022, 0, "o"),
    "black_orb": (1330, 0, "o"),
    "yellow_pad": (35, -12, "_"),
    "pink_pad": (140, -12, "_"),
    "blue_pad": (67, -12, "_"),
    "cube_portal": (12, 0, "C"),
    "ship_portal": (13, 0, "S"),
    "ball_portal": (47, 0, "B"),
    "ufo_portal": (111, 0, "U"),
    "wave_portal": (660, 0, "W"),
    "robot_portal": (745, 0, "R"),
    "spider_portal": (1331, 0, "P"),
    "swing_portal": (1933, 0, "G"),
    "gravity_down_portal": (10, 0, "|"),
    "gravity_up_portal": (11, 0, "|"),
    "mini_portal": (101, 0, "m"),
    "normal_size_portal": (99, 0, "M"),
    "dual_portal": (286, 0, "D"),
    "single_portal": (287, 0, "d"),
    "speed_slow": (200, 0, "<"),
    "speed_normal": (201, 0, ">"),
    "speed_fast": (202, 0, ">"),
    "speed_faster": (203, 0, ">"),
    "speed_fastest": (1334, 0, ">"),
    "user_coin": (1329, 0, "$"),
    "color_trigger": (899, 0, "*"),
}
ID_TO_NAME = {obj_id: name for name, (obj_id, _, _) in PALETTE.items()}

OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": sorted(PALETTE)},
        "col": {"type": "number", "description": "Horizontal cell, 0 = player start. Halves allowed."},
        "row": {"type": "number", "description": "Vertical cell, 0 = resting on the ground. Halves allowed."},
        "rotation": {"type": "number", "description": "Degrees clockwise, e.g. 180 for a ceiling spike."},
        "flip_y": {"type": "boolean"},
        "scale": {"type": "number", "description": "Size multiplier, default 1."},
        "color": {
            "type": "object",
            "description": "color_trigger only: fades a colour channel.",
            "properties": {
                "channel": {"type": "string", "enum": ["background", "ground", "line", "obj"]},
                "r": {"type": "integer"}, "g": {"type": "integer"}, "b": {"type": "integer"},
                "fade_seconds": {"type": "number"},
            },
            "required": ["channel", "r", "g", "b"],
        },
    },
    "required": ["type", "col", "row"],
}
CHANNELS = {"background": 1000, "ground": 1001, "line": 1002, "obj": 1004}


class DesignError(ValueError):
    pass


def validate(objects):
    """Check Claude's object list by hand (tool inputs stream unvalidated)."""
    if not isinstance(objects, list) or not objects:
        raise DesignError("'objects' must be a non-empty list")
    if len(objects) > 5000:
        raise DesignError("too many objects in one go (max 5000); build in sections")
    for i, obj in enumerate(objects):
        if not isinstance(obj, dict):
            raise DesignError(f"objects[{i}] is not an object")
        if obj.get("type") not in PALETTE:
            raise DesignError(f"objects[{i}].type '{obj.get('type')}' is not in the palette")
        for key in ("col", "row"):
            if not isinstance(obj.get(key), (int, float)) or isinstance(obj.get(key), bool):
                raise DesignError(f"objects[{i}].{key} must be a number")
        if obj["col"] < -10 or obj["row"] < -1 or obj["row"] > 80:
            raise DesignError(f"objects[{i}] at col {obj['col']}, row {obj['row']} is out of bounds")
        if obj["type"] == "color_trigger":
            color = obj.get("color")
            if not isinstance(color, dict) or color.get("channel") not in CHANNELS:
                raise DesignError(f"objects[{i}] color_trigger needs a 'color' with a valid channel")
            if not all(isinstance(color.get(c), int) and 0 <= color[c] <= 255 for c in "rgb"):
                raise DesignError(f"objects[{i}] color r/g/b must be integers 0-255")


def _num(value):
    return f"{value:g}"


def to_gd_object(obj, col_offset=0):
    obj_id, y_offset, _ = PALETTE[obj["type"]]
    x = (obj["col"] + col_offset) * CELL + CELL / 2
    y = obj["row"] * CELL + CELL / 2 + y_offset
    parts = [1, obj_id, 2, _num(x), 3, _num(y)]
    if obj.get("rotation"):
        parts += [6, _num(obj["rotation"])]
    if obj.get("flip_y"):
        parts += [5, 1]
    if obj.get("scale") and obj["scale"] != 1:
        parts += [32, _num(obj["scale"])]
    if obj["type"] == "color_trigger":
        c = obj["color"]
        parts += [7, c["r"], 8, c["g"], 9, c["b"], 10, _num(c.get("fade_seconds", 0.5)),
                  23, CHANNELS[c["channel"]]]
    return ",".join(str(p) for p in parts)


def from_gd_object(raw):
    """GD object dict ({'1': '8', '2': '315', ...}) -> grid object for Claude."""
    obj_id = int(raw.get("1", 0))
    name = ID_TO_NAME.get(obj_id, f"object_{obj_id}")
    y_offset = PALETTE[name][1] if name in PALETTE else 0
    col = (float(raw.get("2", 0)) - CELL / 2) / CELL
    row = (float(raw.get("3", 0)) - CELL / 2 - y_offset) / CELL
    obj = {"type": name, "col": round(col * 2) / 2, "row": round(row * 2) / 2}
    if raw.get("6") not in (None, "0"):
        obj["rotation"] = float(raw["6"])
    return obj


def preview(objects, col_offset=0, max_cols=120):
    """ASCII drawing of a design, ground at the bottom. Wide designs are cropped."""
    if not objects:
        return "(empty)"
    cells = {}
    for obj in objects:
        char = PALETTE[obj["type"]][2] if obj["type"] in PALETTE else "?"
        if obj.get("rotation") in (180, -180) and char == "^":
            char = "v"
        cells[(int(obj["row"]), int(obj["col"] + col_offset))] = char
    min_col = min(c for _, c in cells)
    max_col = min(max(c for _, c in cells), min_col + max_cols - 1)
    max_row = max(max(r for r, _ in cells), 3)
    lines = []
    for row in range(max_row, -1, -1):
        line = "".join(cells.get((row, col), " ") for col in range(min_col, max_col + 1))
        lines.append(f"{row:>3} |{line}")
    lines.append("    +" + "=" * (max_col - min_col + 1))
    lines.append(f"     col {min_col} -> {max_col}"
                 + ("  (cropped)" if max(c for _, c in cells) > max_col else ""))
    return "\n".join(lines)
