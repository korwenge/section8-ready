"""Lossless editing of a GD level string: header, colour channels and objects.

Objects we don't touch are kept as their exact original text, so editing a
level never loses properties this code doesn't understand (2.2 has hundreds).
Property keys follow the GD level format (1 = id, 2 = x, 3 = y, 21 = main
colour channel, 24 = Z layer, 128/129 = scale X/Y, ...).
"""

import math
from dataclasses import dataclass, field

# Default colour channel entry layout, copied from what GD itself writes.
_COLOR_TEMPLATE = "1_{r}_2_{g}_3_{b}_11_255_12_255_13_255_4_-1_6_{ch}_7_{a}_15_1_18_0_8_1{blend}"
SPECIAL_CHANNELS = {1000: "BG", 1001: "G1", 1002: "Line", 1003: "3DL", 1004: "Obj", 1005: "P1",
                    1006: "P2", 1007: "LBG", 1009: "G2", 1010: "Black", 1011: "White",
                    1012: "Lighter", 1013: "MG", 1014: "MG2"}
FIXED_COLORS = {1010: (0, 0, 0), 1011: (255, 255, 255), 1005: (125, 255, 0), 1006: (0, 255, 255)}
Z_LAYERS = {"B5": -5, "B4": -3, "B3": -1, "B2": 1, "B1": 3, "T1": 5, "T2": 7, "T3": 9, "T4": 11}


def fmt_num(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return str(int(value))
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def parse_pairs(text, sep):
    parts = text.split(sep)
    return dict(zip(parts[0::2], parts[1::2]))


@dataclass(eq=False)
class GDObject:
    raw: str
    props: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, raw):
        return cls(raw, parse_pairs(raw, ","))

    @classmethod
    def build(cls, props):
        """Make a new object from {key: value}; keys 1, 2, 3 first like GD writes them."""
        props = {str(k): fmt_num(v) for k, v in props.items() if v is not None}
        order = sorted(props, key=lambda k: (k not in ("1", "2", "3"), int(k) if k.isdigit() else 10**9, k))
        raw = ",".join(f"{k},{props[k]}" for k in order)
        return cls(raw, {k: props[k] for k in order})

    def num(self, key, default=0.0):
        try:
            return float(self.props.get(key, default))
        except ValueError:
            return default

    @property
    def id(self):
        return int(self.num("1"))

    @property
    def x(self):
        return self.num("2")

    @property
    def y(self):
        return self.num("3")


class Level:
    def __init__(self, level_string):
        header, _, rest = level_string.partition(";")
        self.header = parse_pairs(header, ",")
        self._header_order = header.split(",")[0::2]
        self.objects = [GDObject.parse(o) for o in rest.split(";") if o]
        self.colors = {}
        self._colors_changed = False
        for entry in self.header.get("kS38", "").split("|"):
            if entry:
                props = parse_pairs(entry, "_")
                if props.get("6", "").lstrip("-").isdigit():
                    self.colors[int(props["6"])] = props

    # -- colours --

    def color_rgb(self, channel):
        """(r, g, b, opacity) for a channel, or None if the level doesn't define it."""
        if channel in FIXED_COLORS:
            return (*FIXED_COLORS[channel], 1.0)
        c = self.colors.get(channel)
        if not c:
            return None
        try:
            return int(c.get("1", 255)), int(c.get("2", 255)), int(c.get("3", 255)), float(c.get("7", 1))
        except ValueError:
            return None

    def used_channels(self):
        used = set(self.colors)
        for o in self.objects:
            for key in ("21", "22", "23", "50"):
                if o.props.get(key, "").isdigit():
                    used.add(int(o.props[key]))
        return used

    def channel_for(self, rgb, opacity=1.0, blending=False):
        """Reuse a custom channel with exactly this colour, or create a new one."""
        for ch, c in sorted(self.colors.items()):
            if 1 <= ch <= 999 and self.color_rgb(ch) == (*rgb, float(opacity)) \
                    and (c.get("5") == "1") == blending and "9" not in c and c.get("4", "-1") == "-1":
                return ch
        used = self.used_channels()
        for ch in range(1, 1000):
            if ch not in used:
                self.set_color(ch, rgb, opacity, blending)
                return ch
        raise ValueError("all 999 colour channels are in use")

    def set_color(self, channel, rgb, opacity=1.0, blending=False):
        r, g, b = (max(0, min(255, int(v))) for v in rgb)
        entry = _COLOR_TEMPLATE.format(r=r, g=g, b=b, ch=channel, a=fmt_num(float(opacity)),
                                       blend="_5_1" if blending else "")
        self.colors[channel] = parse_pairs(entry, "_")
        self._colors_changed = True

    # -- objects --

    def add(self, props):
        obj = GDObject.build(props)
        self.objects.append(obj)
        return obj

    def remove_raw(self, raw):
        for i, o in enumerate(self.objects):
            if o.raw == raw:
                del self.objects[i]
                return True
        return False

    def in_rect(self, x0, y0, x1, y1):
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        return [(i, o) for i, o in enumerate(self.objects) if x0 <= o.x <= x1 and y0 <= o.y <= y1]

    def bounds(self):
        if not self.objects:
            return 0, 0, 300, 300
        xs = [o.x for o in self.objects]
        ys = [o.y for o in self.objects]
        return min(xs), min(ys), max(xs), max(ys)

    # -- output --

    def to_string(self):
        header = dict(self.header)
        order = list(self._header_order)
        if self._colors_changed:
            header["kS38"] = "|".join(
                "_".join(f"{k}_{v}" for k, v in props.items()) for _, props in sorted(self.colors.items())
            ) + "|"
            if "kS38" not in order:
                order.insert(0, "kS38")
        text = ",".join(f"{k},{header[k]}" for k in order if k in header)
        return text + ";" + "".join(o.raw + ";" for o in self.objects)
