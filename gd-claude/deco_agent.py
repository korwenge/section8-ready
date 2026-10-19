"""Claude as a GD decoration artist.

One DecoSession per level: the player selects an area, writes a prompt, and
Claude looks at the area (as an image and as object data), builds a proposal
(objects to add / remove, channel colours to change), checks it visually and
explains it. Nothing touches the save file here; the app queues the proposal.
"""

import base64
import json
from collections import Counter

import anthropic

from deco import ChangeSet, grid_to_objects, hex_to_rgb, image_to_grid, render_region
from gdlevel import SPECIAL_CHANNELS, Z_LAYERS, Level

DEFAULT_MODEL = "claude-opus-5-5"
MAX_OBJECTS_PER_CALL = 4000

SYSTEM_PROMPT = """You are GD Claude, a Geometry Dash decoration artist working inside the \
player's level. The player selects an area of their level, tells you what they want (a deco \
style, pixel art, an image to convert, fixing a structure, copying a style across the level) \
and you build it. You work through tools; the player sees your proposal drawn over their level \
and accepts or refines it.

## Coordinates and objects
- GD units: 30 per block. x grows to the right, y grows UP. The top of the ground is y=0, so a \
  block resting on the ground has its centre at y=15. Positions are object centres.
- Each object is a set of GD properties. You set id, x, y, and optionally:
  rotation (key 6, degrees clockwise), flip x/y (4/5), scale (32), scale x/y (128/129), \
  z layer (24), z order (25), no glow (96), don't fade (64), don't enter (67), \
  main/secondary HSV (41/43, 42/44 as "h a s a v a sChecked a vChecked", e.g. "0a1a0.6a0a0").
- Z layers (key 24): B5=-5, B4=-3, B3=-1, B2=1, B1=3, T1=5, T2=7, T3=9, T4=11. Background \
  deco usually goes in B2-B4, details and glow in front of blocks on T1. Z order (25) sorts \
  objects within a layer.
- Colours: give an object `color: "#rrggbb"` and the app gives it a colour channel (reusing \
  identical colours), or `channel: N` to use one of the level's existing channels -- prefer \
  existing channels when matching the level's current style. You can recolour a channel \
  (e.g. 1000 = background, 1001 = ground) with set_channels.
- Find objects with search_objects (about 4000 named objects: glows, outlines, circles, \
  particles, slopes, chains, clouds, ...). Prefer object ids this level already uses when \
  continuing its style. Object 211 is a plain square in its main colour (pixel art, fills).

## Pixel art and images
- Use draw_pixel_art: you write the picture as rows of characters with a palette; the app \
  merges same-coloured areas into few objects. Typical pixel sizes: 3.75 (very detailed), \
  7.5 (detailed), 15 (chunky). Keep the art inside the selection; think about the outline, \
  shading (2-3 tones per colour) and readability at game zoom.
- If the player attached an image to convert, use convert_image (exact conversion done \
  locally) rather than redrawing it by hand.

## How to work
1. Look at the attached picture of the selection and inspect objects (get_objects) to \
   understand the structures, colours and style already there.
2. Plan, then build with propose_changes / draw_pixel_art / clone_area.
3. ALWAYS call look_at_area afterwards to see your result, fix what looks wrong (gaps, \
   clutter, colours that clash, deco covering gameplay), and look again.
4. Finish with a short, friendly summary of what you made and what the player could ask for \
   next. The player will accept it or ask for changes.
- Stay inside the selected area unless asked otherwise. Never move or delete gameplay \
  objects (blocks, spikes, orbs, pads, portals, triggers) unless the player asks you to fix \
  structures. Decoration must not hide hazards or make gameplay unreadable.
- Big levels lag: be generous with detail but avoid pointless object spam.
- When copying a style across the level, inspect the source area first and reuse its exact \
  object ids, colours, scales and layers; clone_area copies objects exactly."""

MEMORY_PROMPT = """

## The player's notes (Obsidian vault)
Saved deco styles and preferences. Follow them; when the player describes a style they like \
or gives feedback, save it with save_note (e.g. under "Deco Styles").
<vault>
{notes}
</vault>"""


def _tool(name, description, properties, required=()):
    return {"name": name, "description": description, "eager_input_streaming": True,
            "input_schema": {"type": "object", "properties": properties, "required": list(required)}}


RECT = {"x0": {"type": "number"}, "y0": {"type": "number"}, "x1": {"type": "number"}, "y1": {"type": "number"}}
NEW_OBJECT = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"},
        "color": {"type": "string", "description": "#rrggbb main colour"},
        "opacity": {"type": "number", "description": "0-1, only with color"},
        "channel": {"type": "integer", "description": "existing colour channel instead of color"},
        "props": {"type": "object", "description": "extra GD properties by key, e.g. {\"6\": 45, \"24\": -1, \"128\": 2}"},
    },
    "required": ["id", "x", "y"],
}

TOOLS = [
    _tool("level_overview", "Size of the level, colour channels, and the most used objects.", {}),
    _tool("get_objects", "Objects inside a rectangle (GD units), with ref numbers you can use to remove them.",
          {**RECT, "limit": {"type": "integer", "description": "default 400"}}, ["x0", "y0", "x1", "y1"]),
    _tool("look_at_area", "See a picture of an area of the level with your current proposal drawn in.",
          {**RECT, "with_proposal": {"type": "boolean", "description": "default true"}}, ["x0", "y0", "x1", "y1"]),
    _tool("search_objects", "Find GD objects by name words, e.g. 'glow corner', 'cloud', 'chain'.",
          {"query": {"type": "string"}}, ["query"]),
    _tool("propose_changes", "Add objects, remove objects (by ref) and/or recolour channels in your proposal.",
          {"add": {"type": "array", "items": NEW_OBJECT},
           "remove_refs": {"type": "array", "items": {"type": "integer"}},
           "set_channels": {"type": "array", "items": {"type": "object", "properties": {
               "channel": {"type": "integer"}, "color": {"type": "string"}, "opacity": {"type": "number"}},
               "required": ["channel", "color"]}},
           "summary": {"type": "string", "description": "one line describing this step"}}),
    _tool("draw_pixel_art", "Draw pixel art into the proposal. rows: equal-length strings, one char per pixel; "
          "chars missing from the palette are transparent ('.' recommended).",
          {"rows": {"type": "array", "items": {"type": "string"}},
           "palette": {"type": "object", "description": "char -> '#rrggbb'"},
           "left": {"type": "number"}, "top": {"type": "number"},
           "pixel_size": {"type": "number", "description": "GD units per pixel (default 7.5)"},
           "z_layer": {"type": "integer"}, "z_order": {"type": "integer"}, "summary": {"type": "string"}},
          ["rows", "palette", "left", "top"]),
    _tool("convert_image", "Convert the image the player attached into pixel art inside a box.",
          {"left": {"type": "number"}, "top": {"type": "number"}, "width": {"type": "number"},
           "height": {"type": "number"}, "pixel_size": {"type": "number", "description": "default 7.5"},
           "colours": {"type": "integer", "description": "max colours, default 24, max 64"},
           "z_layer": {"type": "integer"}}, ["left", "top", "width", "height"]),
    _tool("clone_area", "Copy the existing objects in a rectangle to other places (dx/dy offsets). "
          "Gameplay objects are skipped unless include_gameplay is true.",
          {**RECT, "offsets": {"type": "array", "items": {"type": "object", "properties": {
              "dx": {"type": "number"}, "dy": {"type": "number"}}, "required": ["dx", "dy"]}},
           "include_gameplay": {"type": "boolean"}, "keep_groups": {"type": "boolean"}},
          ["x0", "y0", "x1", "y1", "offsets"]),
    _tool("clear_proposal", "Throw away the current proposal and start over.", {}),
]
NOTE_TOOLS = [
    _tool("read_note", "Read a note from the player's vault.", {"title": {"type": "string"}}, ["title"]),
    _tool("save_note", "Save to the player's vault ('append' or 'replace').",
          {"title": {"type": "string"}, "content": {"type": "string"},
           "mode": {"type": "string", "enum": ["append", "replace"]}}, ["title", "content"]),
]


class ToolError(ValueError):
    pass


def _num(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"'{name}' must be a number")
    return float(value)


def _rect(args):
    x0, y0, x1, y1 = (_num(args.get(k), k) for k in ("x0", "y0", "x1", "y1"))
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _png_block(png):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(png).decode()}}


class DecoSession:
    def __init__(self, level, catalog, client=None, vault=None, model=DEFAULT_MODEL, on_event=None):
        self.level = level
        self.catalog = catalog
        self.client = client  # created lazily, so local-only features work without an API key
        self.vault = vault
        self.model = model
        self.on_event = on_event or (lambda event: None)
        self.messages = []
        self.proposal = ChangeSet()
        self.event_note = ""  # what happened to the last proposal, told to Claude next turn
        self.image = None  # bytes of the image the player attached last
        self.cancelled = False
        notes = MEMORY_PROMPT.format(notes=vault.context()) if vault else ""
        self.system = SYSTEM_PROMPT + notes
        self.tools = TOOLS + (NOTE_TOOLS if vault else [])

    # --- proposal helpers ---------------------------------------------------

    def _gameplay(self, obj):
        return self.catalog.kind(obj.id) in ("solid", "hazard", "gameplay", "trigger")

    def _new_object(self, spec, i):
        if not isinstance(spec, dict):
            raise ToolError(f"add[{i}] must be an object")
        obj_id = spec.get("id")
        if not isinstance(obj_id, int) or obj_id <= 0:
            raise ToolError(f"add[{i}].id must be a positive integer")
        props = {"1": obj_id, "2": round(_num(spec.get("x"), f"add[{i}].x"), 3),
                 "3": round(_num(spec.get("y"), f"add[{i}].y"), 3)}
        extra = spec.get("props") or {}
        if not isinstance(extra, dict):
            raise ToolError(f"add[{i}].props must be an object")
        for key, value in extra.items():
            key = str(key)
            if not key.isdigit() or key in ("1", "2", "3"):
                raise ToolError(f"add[{i}].props key '{key}' must be a GD property number (not 1/2/3)")
            if isinstance(value, bool):
                value = int(value)
            if not isinstance(value, (int, float, str)) or any(c in str(value) for c in ",;|"):
                raise ToolError(f"add[{i}].props['{key}'] has an invalid value")
            props[key] = value
        if spec.get("color"):
            hex_to_rgb(str(spec["color"]))  # validates
            props["_hex"] = str(spec["color"]).lower()
            if spec.get("opacity") is not None:
                props["_opacity"] = max(0.0, min(1.0, _num(spec["opacity"], "opacity")))
            props.pop("21", None)
        elif spec.get("channel") is not None:
            channel = spec["channel"]
            if not isinstance(channel, int) or not 1 <= channel <= 1101:
                raise ToolError(f"add[{i}].channel must be an existing channel number")
            props["21"] = channel
        return props

    def _stats(self, note=""):
        ids = Counter(p["1"] for p in self.proposal.add)
        return {"proposal_adds": len(self.proposal.add), "proposal_removes": len(self.proposal.remove),
                "channel_changes": len(self.proposal.channels),
                "top_added_objects": [f"{self.catalog.name(i)} ({i}) x{n}" for i, n in ids.most_common(8)],
                **({"note": note} if note else {})}

    # --- tools ----------------------------------------------------------------

    def tool_level_overview(self, args):
        x0, y0, x1, y1 = self.level.bounds()
        usage = Counter(o.id for o in self.level.objects)
        channels = {}
        for ch in sorted(self.level.colors):
            rgb = self.level.color_rgb(ch)
            if rgb:
                channels[str(ch)] = {"color": "#%02x%02x%02x" % rgb[:3], "opacity": rgb[3],
                                     **({"name": SPECIAL_CHANNELS[ch]} if ch in SPECIAL_CHANNELS else {})}
        layers = Counter(int(o.num("24")) for o in self.level.objects if not self._gameplay(o))
        return {"objects": len(self.level.objects), "bounds": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                "colour_channels": channels,
                "most_used_objects": [{"id": i, "name": self.catalog.name(i), "count": n}
                                      for i, n in usage.most_common(40)],
                "deco_z_layers_used": {str(k): v for k, v in layers.most_common()},
                "z_layer_values": Z_LAYERS}

    def tool_get_objects(self, args):
        x0, y0, x1, y1 = _rect(args)
        limit = args.get("limit") if isinstance(args.get("limit"), int) else 400
        found = self.level.in_rect(x0, y0, x1, y1)
        out = []
        for ref, o in found[:max(1, min(limit, 2000))]:
            extra = {k: v for k, v in o.props.items() if k not in ("1", "2", "3")}
            out.append({"ref": ref, "id": o.id, "name": self.catalog.name(o.id),
                        "kind": self.catalog.kind(o.id), "x": o.x, "y": o.y, **({"props": extra} if extra else {})})
        return {"count": len(found), "shown": len(out), "objects": out}

    def tool_look_at_area(self, args):
        x0, y0, x1, y1 = _rect(args)
        with_proposal = args.get("with_proposal", True) is not False
        png = self.render(x0, y0, x1, y1, with_proposal)
        return [_png_block(png), {"type": "text", "text": json.dumps(
            {"area": [x0, y0, x1, y1], "with_proposal": with_proposal, **self._stats()})}]

    def render(self, x0, y0, x1, y1, with_proposal=True):
        if not with_proposal:
            return render_region(self.level, self.catalog, x0, y0, x1, y1)
        level = self.level
        if self.proposal.channels:
            level = Level(self.level.to_string())
            self.proposal.recolour(level)
        return render_region(level, self.catalog, x0, y0, x1, y1,
                             extra=self.proposal.add, removed=self.proposal.remove)

    def tool_search_objects(self, args):
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("'query' is required")
        used = Counter(o.id for o in self.level.objects)
        hits = self.catalog.search(query)
        for hit in hits:
            if used.get(hit["id"]):
                hit["used_in_level"] = used[hit["id"]]
        return {"results": hits, "catalog": self.catalog.source}

    def tool_propose_changes(self, args):
        adds = args.get("add") or []
        refs = args.get("remove_refs") or []
        channels = args.get("set_channels") or []
        if not isinstance(adds, list) or not isinstance(refs, list) or not isinstance(channels, list):
            raise ToolError("add, remove_refs and set_channels must be lists")
        if len(adds) > MAX_OBJECTS_PER_CALL:
            raise ToolError(f"at most {MAX_OBJECTS_PER_CALL} objects per call")
        new = [self._new_object(spec, i) for i, spec in enumerate(adds)]
        removes = []
        for ref in refs:
            if not isinstance(ref, int) or not 0 <= ref < len(self.level.objects):
                raise ToolError(f"remove ref {ref} doesn't exist (use refs from get_objects)")
            removes.append(self.level.objects[ref].raw)
        for c in channels:
            if not isinstance(c, dict) or not isinstance(c.get("channel"), int) or not 1 <= c["channel"] <= 1014:
                raise ToolError("set_channels entries need a channel number 1-999 or 1000-1014")
            hex_to_rgb(str(c.get("color", "")))
        recolour = {c["channel"]: [str(c["color"]).lower(), max(0.0, min(1.0, float(c.get("opacity", 1.0))))]
                    for c in channels}
        self.proposal.extend(ChangeSet(new, removes, str(args.get("summary") or ""), recolour))
        return self._stats()

    def tool_draw_pixel_art(self, args):
        rows, palette = args.get("rows"), args.get("palette")
        if not isinstance(rows, list) or not rows or not all(isinstance(r, str) for r in rows):
            raise ToolError("'rows' must be a list of strings")
        if len(rows) > 256 or max(len(r) for r in rows) > 256:
            raise ToolError("pixel art can be at most 256 x 256")
        if not isinstance(palette, dict) or not palette:
            raise ToolError("'palette' must map characters to '#rrggbb'")
        for char, colour in palette.items():
            if len(char) != 1:
                raise ToolError(f"palette key '{char}' must be one character")
            hex_to_rgb(str(colour))
        pixel = _num(args.get("pixel_size", 7.5), "pixel_size")
        if not 1 <= pixel <= 60:
            raise ToolError("pixel_size must be between 1 and 60")
        objects = grid_to_objects(rows, palette, _num(args.get("left"), "left"), _num(args.get("top"), "top"),
                                  pixel, args.get("z_layer"), args.get("z_order"))
        self.proposal.extend(ChangeSet(objects, [], str(args.get("summary") or "pixel art")))
        return self._stats(f"pixel art: {len(objects)} objects for {sum(len(r) for r in rows)} pixels")

    def tool_convert_image(self, args):
        if not self.image:
            raise ToolError("the player hasn't attached an image")
        pixel = _num(args.get("pixel_size", 7.5), "pixel_size")
        width, height = _num(args.get("width"), "width"), _num(args.get("height"), "height")
        if pixel <= 0 or width <= 0 or height <= 0:
            raise ToolError("width, height and pixel_size must be positive")
        cols, rows_max = int(width // pixel), int(height // pixel)
        if cols * rows_max > 256 * 256:
            raise ToolError("too many pixels: use a bigger pixel_size")
        colours = args.get("colours") if isinstance(args.get("colours"), int) else 24
        rows, palette = image_to_grid(self.image, max(1, cols), max(1, rows_max), colours)
        objects = grid_to_objects(rows, palette, _num(args.get("left"), "left"), _num(args.get("top"), "top"),
                                  pixel, args.get("z_layer"))
        self.proposal.extend(ChangeSet(objects, [], "converted image"))
        return self._stats(f"image converted to {len(rows[0])}x{len(rows)} pixels, "
                           f"{len(palette)} colours, {len(objects)} objects")

    def tool_clone_area(self, args):
        x0, y0, x1, y1 = _rect(args)
        offsets = args.get("offsets")
        if not isinstance(offsets, list) or not offsets or len(offsets) > 200:
            raise ToolError("'offsets' must be a list of 1-200 {dx, dy}")
        source = [o for _, o in self.level.in_rect(x0, y0, x1, y1)
                  if args.get("include_gameplay") or not self._gameplay(o)]
        copies = []
        for off in offsets:
            dx, dy = _num(off.get("dx"), "dx"), _num(off.get("dy"), "dy")
            for o in source:
                props = dict(o.props)
                props["2"] = round(o.x + dx, 3)
                props["3"] = round(o.y + dy, 3)
                if not args.get("keep_groups"):
                    props.pop("57", None)
                    props.pop("33", None)
                copies.append(props)
        if len(copies) > MAX_OBJECTS_PER_CALL * 3:
            raise ToolError("that would copy too many objects; use fewer offsets or a smaller area")
        self.proposal.extend(ChangeSet(copies, [], f"cloned {len(source)} objects x{len(offsets)}"))
        return self._stats(f"copied {len(source)} objects to {len(offsets)} places")

    def tool_clear_proposal(self, args):
        self.proposal = ChangeSet()
        return self._stats("proposal cleared")

    def tool_read_note(self, args):
        return self.vault.read(str(args.get("title") or ""))

    def tool_save_note(self, args):
        mode = "replace" if args.get("mode") == "replace" else "append"
        saved = self.vault.write(str(args.get("title") or ""), str(args.get("content") or ""), mode)
        self.on_event({"type": "note", "title": saved})
        return {"saved": saved}

    def run_tool(self, block):
        handler = getattr(self, f"tool_{block.name}", None)
        if handler is None or (block.name in ("read_note", "save_note") and not self.vault):
            return f"unknown tool {block.name}", True
        try:
            result = handler(block.input if isinstance(block.input, dict) else {})
        except (ToolError, ValueError, KeyError) as e:
            return str(e), True
        if isinstance(result, list):  # already content blocks (images)
            return result, False
        return json.dumps(result, ensure_ascii=False), False

    # --- conversation ------------------------------------------------------------

    def _call(self):
        if self.client is None:
            self.client = anthropic.Anthropic()
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "high"},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            for text in stream.text_stream:
                self.on_event({"type": "text", "text": text})
            return stream.get_final_message()

    def ask(self, prompt, selection, image=None, image_type="image/png", mode=None):
        """selection: (x0, y0, x1, y1) in GD units. Returns the final reply text."""
        self.cancelled = False
        if image:
            self.image = image
        x0, y0, x1, y1 = (round(v, 1) for v in selection)
        content = [_png_block(self.render(x0, y0, x1, y1))]
        if image:
            content.append({"type": "image", "source": {"type": "base64", "media_type": image_type,
                                                        "data": base64.b64encode(image).decode()}})
        details = [f"Selected area: x {x0} to {x1}, y {y0} to {y1} (GD units, "
                   f"{(x1 - x0) / 30:g} x {(y1 - y0) / 30:g} blocks). The first picture shows it"
                   + (" with your current proposal." if self.proposal.add or self.proposal.remove else ".")]
        if image:
            details.append("The second picture is an image the player attached (convert_image can convert it).")
        if mode:
            details.append(f"Mode the player picked: {mode}.")
        if self.event_note:
            details.insert(0, self.event_note)
            self.event_note = ""
        content.append({"type": "text", "text": " ".join(details) + f"\n\nPlayer: {prompt}"})

        start = len(self.messages)
        self.messages.append({"role": "user", "content": content})
        reply = []
        while True:
            if self.cancelled:
                self.on_event({"type": "status", "text": "stopped"})
                break
            try:
                response = self._call()
            except ValueError as e:  # tool input JSON the SDK couldn't parse
                del self.messages[start:]
                raise RuntimeError(f"Claude sent a broken tool call ({e}); please try again") from e
            if response.stop_reason == "refusal":
                del self.messages[start:]
                raise RuntimeError("Claude declined this request.")
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "max_tokens" and tool_uses:
                del self.messages[start:]
                raise RuntimeError("That was too big for one reply; try a smaller area or fewer details.")
            reply += [b.text for b in response.content if b.type == "text"]
            self.messages.append({"role": "assistant", "content": response.content})
            if not tool_uses:
                break
            results = []
            for block in tool_uses:
                self.on_event({"type": "tool", "name": block.name})
                content, is_error = self.run_tool(block)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
                if block.name in ("propose_changes", "draw_pixel_art", "convert_image",
                                  "clone_area", "clear_proposal") and not is_error:
                    self.on_event({"type": "proposal"})
            self.messages.append({"role": "user", "content": results})
        return "".join(reply)

    def finish_proposal(self, accepted):
        """The player accepted (queued) or discarded the proposal; start a fresh one."""
        if self.proposal:
            self.event_note = ("The player ACCEPTED your last proposal; it is queued and will be in the level. "
                               "Start a new proposal for the next request." if accepted else
                               "The player DISCARDED your last proposal; it is gone.")
        self.proposal = ChangeSet()
