"""Decoration toolkit: change sets, pixel art, image conversion and rendering.

A ChangeSet is what Claude proposes: objects to add, objects to remove and
colours to use. It can be previewed, queued, and applied to a level later
(even after the player has edited the level in GD in the meantime).
"""

import io
import math
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from gdlevel import GDObject, Level

PIXEL_OBJECT = 211  # "Colored Inner Square": a plain 30x30 square in its main colour


def hex_to_rgb(value):
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        raise ValueError(f"bad colour '{value}', use #rrggbb")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb[:3])


@dataclass
class ChangeSet:
    """Objects are {key: value} dicts in GD format; '_hex' is a colour still to be given a channel."""
    add: list = field(default_factory=list)
    remove: list = field(default_factory=list)  # raw object strings
    summary: str = ""
    channels: dict = field(default_factory=dict)  # channel -> [hex, opacity] to recolour

    def __bool__(self):
        return bool(self.add or self.remove or self.channels)

    def extend(self, other):
        self.add += other.add
        self.remove += [r for r in other.remove if r not in self.remove]
        self.channels.update(other.channels)
        self.summary = "; ".join(s for s in (self.summary, other.summary) if s)

    def to_json(self):
        return {"add": self.add, "remove": self.remove, "summary": self.summary,
                "channels": {str(k): list(v) for k, v in self.channels.items()}}

    @classmethod
    def from_json(cls, data):
        return cls(list(data.get("add", [])), list(data.get("remove", [])), data.get("summary", ""),
                   {int(k): list(v) for k, v in data.get("channels", {}).items()})

    def recolour(self, level):
        for channel, (hex_color, opacity) in self.channels.items():
            level.set_color(int(channel), hex_to_rgb(hex_color), float(opacity))

    def apply(self, level):
        """Apply to a Level. Colours get real channels now. Returns a small report."""
        self.recolour(level)
        removed = sum(level.remove_raw(raw) for raw in self.remove)
        channels = {}
        for props in self.add:
            props = dict(props)
            hex_color = props.pop("_hex", None)
            opacity = float(props.pop("_opacity", 1.0))
            if hex_color:
                key = (hex_color.lower(), opacity)
                if key not in channels:
                    channels[key] = level.channel_for(hex_to_rgb(hex_color), opacity)
                props["21"] = channels[key]
            level.add(props)
        return {"added": len(self.add), "removed": removed, "new_colours": len(channels),
                "missing": len(self.remove) - removed}


# --- pixel art ------------------------------------------------------------------

def grid_to_objects(rows, palette, left, top, pixel=7.5, z_layer=None, z_order=None, obj_id=PIXEL_OBJECT):
    """Pixel grid -> as few square objects as possible (merging same-colour rectangles).

    rows: list of strings, one char per pixel; palette: {char: '#rrggbb'}; any char not in
    the palette is transparent. (left, top) is the top-left corner in GD units.
    """
    runs = []  # (row, start, length, colour)
    for r, row in enumerate(rows):
        c = 0
        while c < len(row):
            colour = palette.get(row[c])
            if not colour:
                c += 1
                continue
            start = c
            while c < len(row) and palette.get(row[c]) == colour:
                c += 1
            runs.append((r, start, c - start, colour))

    # Merge runs with the same start/length/colour in consecutive rows into rectangles.
    rects = []
    open_rects = {}  # (start, length, colour) -> rect dict
    for r, start, length, colour in runs:
        key = (start, length, colour)
        rect = open_rects.get(key)
        if rect and rect["row"] + rect["height"] == r:
            rect["height"] += 1
        else:
            rect = {"row": r, "start": start, "length": length, "height": 1, "colour": colour}
            rects.append(rect)
            open_rects[key] = rect

    objects = []
    for rect in rects:
        props = {
            "1": obj_id,
            "2": round(left + (rect["start"] + rect["length"] / 2) * pixel, 3),
            "3": round(top - (rect["row"] + rect["height"] / 2) * pixel, 3),
            "128": round(rect["length"] * pixel / 30, 4),
            "129": round(rect["height"] * pixel / 30, 4),
            "_hex": rect["colour"].lower(),
        }
        if z_layer is not None:
            props["24"] = z_layer
        if z_order is not None:
            props["25"] = z_order
        objects.append(props)
    return objects


def image_to_grid(image_bytes, max_width, max_height, colours=24):
    """Image -> (rows, palette) fitting inside max_width x max_height pixels."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    scale = min(max_width / img.width, max_height / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    img = img.resize(size, Image.LANCZOS if scale < 1 else Image.NEAREST)
    alpha = img.getchannel("A")
    rgb = Image.new("RGB", img.size, (0, 0, 0))
    rgb.paste(img, mask=alpha)
    quant = rgb.quantize(colors=max(2, min(colours, 64)), method=Image.Quantize.MEDIANCUT)
    flat = quant.getpalette()
    symbols = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*()"
    palette, used = {}, {}
    rows = []
    for y in range(quant.height):
        row = []
        for x in range(quant.width):
            if alpha.getpixel((x, y)) < 128:
                row.append(".")
                continue
            idx = quant.getpixel((x, y))
            if idx not in used:
                sym = symbols[len(used)]
                used[idx] = sym
                palette[sym] = rgb_to_hex(flat[idx * 3: idx * 3 + 3])
            row.append(used[idx])
        rows.append("".join(row))
    return rows, palette


# --- geometry & rendering ----------------------------------------------------

DEFAULT_KIND_COLOURS = {
    "solid": (20, 20, 28, 1.0), "hazard": (230, 60, 70, 1.0), "gameplay": (255, 200, 40, 1.0),
    "deco": (150, 150, 165, 0.7), "pixel": (255, 255, 255, 1.0), "trigger": (120, 220, 255, 0.6),
}


def object_shape(obj, level, catalog, hex_override=None):
    """(x, y, w, h, rotation, (r, g, b, a), kind) for drawing an object."""
    props = obj.props if isinstance(obj, GDObject) else obj

    def get(key, default=0.0):
        try:
            return float(props.get(key, default))
        except (TypeError, ValueError):
            return default

    obj_id = int(get("1"))
    kind = catalog.kind(obj_id)
    w, h = catalog.size(obj_id)
    scale = get("32", 1.0) or 1.0
    w *= scale * (get("128", 1.0) or 1.0)
    h *= scale * (get("129", 1.0) or 1.0)
    colour = None
    hex_color = hex_override or props.get("_hex")
    if hex_color:
        colour = (*hex_to_rgb(hex_color), 1.0)
    elif str(props.get("21", "")).isdigit():
        colour = level.color_rgb(int(props["21"]))
    if colour is None:
        colour = DEFAULT_KIND_COLOURS[kind]
        if kind in ("deco", "pixel"):
            colour = level.color_rgb(1004) or colour
    return get("2"), get("3"), w, h, get("6"), colour, kind


def shape_row(obj, level, catalog):
    """Compact shape for the UI: [x, y, w, h, rot, '#rrggbb', alpha, kind, z, channel]."""
    props = obj.props if isinstance(obj, GDObject) else obj
    x, y, w, h, rot, (r, g, b, a), kind = object_shape(obj, level, catalog)
    try:
        z = int(float(props.get("24", 0) or 0))
    except ValueError:
        z = 0
    channel = props.get("21")
    return [round(x, 2), round(y, 2), round(w, 2), round(h, 2), rot, rgb_to_hex((r, g, b)), round(a, 2),
            kind, z, int(channel) if str(channel).isdigit() and "_hex" not in props else 0]


def level_shapes(level, catalog):
    return [shape_row(o, level, catalog) for o in level.objects]


def render_region(level, catalog, x0, y0, x1, y1, extra=(), removed=(), max_px=900):
    """PNG of a region of the level as the player would roughly see it (y grows upward).

    extra: proposed objects to draw on top (outlined in green); removed: raw strings to hide.
    """
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    width_units, height_units = max(x1 - x0, 30), max(y1 - y0, 30)
    k = min(max_px / width_units, max_px / height_units, 8)
    img_w, img_h = max(1, int(width_units * k)), max(1, int(height_units * k))
    bg = level.color_rgb(1000) or (40, 125, 255, 1)
    img = Image.new("RGBA", (img_w, img_h), (*bg[:3], 255))
    draw = ImageDraw.Draw(img, "RGBA")

    def to_px(x, y):
        return (x - x0) * k, img_h - (y - y0) * k

    if y0 <= 0 <= y1:  # ground
        g = level.color_rgb(1001) or (0, 102, 255, 1)
        _, gy = to_px(0, 0)
        draw.rectangle([0, gy, img_w, img_h], fill=(*g[:3], 255))

    removed = set(removed)
    items = [(o, False) for o in level.objects if o.raw not in removed] + [(p, True) for p in extra]
    margin = 60
    drawn = []
    for obj, is_new in items:
        x, y, w, h, rot, (r, g, b, a), kind = object_shape(obj, level, catalog)
        if kind == "trigger" or not (x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin):
            continue
        z = int(float((obj.props if isinstance(obj, GDObject) else obj).get("24", 0) or 0))
        drawn.append((z, is_new, x, y, w, h, rot, (r, g, b, a), kind))
    drawn.sort(key=lambda d: (d[0], d[1]))
    for z, is_new, x, y, w, h, rot, (r, g, b, a), kind in drawn:
        corners = []
        theta = math.radians(rot)
        for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
            # GD rotation is clockwise; with y up that is a negative angle.
            rx = dx * math.cos(theta) + dy * math.sin(theta)
            ry = -dx * math.sin(theta) + dy * math.cos(theta)
            corners.append(to_px(x + rx, y + ry))
        fill = (r, g, b, int(255 * max(0.15, min(a, 1))))
        if kind == "hazard":
            draw.polygon([corners[0], corners[1], ((corners[2][0] + corners[3][0]) / 2,
                                                   (corners[2][1] + corners[3][1]) / 2)], fill=fill)
        else:
            outline = (255, 255, 255, 90) if kind == "solid" else None
            draw.polygon(corners, fill=fill, outline=outline)
        if is_new and kind != "pixel":
            draw.polygon(corners, outline=(80, 255, 120, 200))
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()
