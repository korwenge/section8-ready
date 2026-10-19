#!/usr/bin/env python3
"""GD Claude -- an AI level-building assistant for the Geometry Dash Create tab.

Chat with Claude about your levels. It can read the levels in your Create tab,
review them, and build new levels or sections directly into your save file
(after showing you a preview and asking first).

    python gdclaude.py                      # chat
    python gdclaude.py "make a short easy cube level called Sunrise"
    python gdclaude.py --list               # just list your levels
"""

import argparse
import json
import sys

import anthropic

import gd_api
from level_builder import OBJECT_SCHEMA, PALETTE, DesignError, from_gd_object, preview, to_gd_object, validate
from save_file import LocalLevels

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = f"""You are GD Claude, a level-design assistant built into a Geometry Dash \
player's Create tab. You help them design, review and build levels.

## How you place objects
You design on a grid. One cell is one block (30 GD units). `col` 0 is where the player \
spawns and the level scrolls right; `row` 0 rests on the ground, row 1 is one block up. \
Half cells (e.g. col 12.5) are allowed. Rotate 180 for ceiling spikes. Available object \
types: {", ".join(sorted(PALETTE))}.

## Gameplay reference (normal 1x speed unless noted)
- The player moves about 10.4 cells per second at 1x (slow ~8.4, fast ~12.9, faster ~15.6, \
fastest ~19.2). A 60-second level at 1x is roughly 620 columns.
- A cube jump rises ~2.2 cells and travels ~3.5-4 cells horizontally. One spike is easy, two \
adjacent spikes need a precise jump, three in a row is the maximum (hard) at 1x.
- A cube can jump onto a 1-cell-high block from the ground, and up 2 cells only with a pad \
or orb. A yellow pad/orb launches ~4 cells high; pink is a smaller hop; blue flips gravity.
- Ship, UFO, wave and swing sections need a ceiling and a floor (a corridor of blocks); \
keep corridors at least 3 cells tall for easy, 2 for hard. Wave at mini size is very hard.
- Give players breathing room: a few empty columns after a portal, before a gamemode change, \
and at the start (begin obstacles around col 8-10).
- Every jump must be possible. Before building, mentally play through the design.

## How you work
- Read the player's existing levels with the tools before commenting on them.
- When building, prefer readable, well-paced gameplay over object spam; use blocks to make \
platforms and structures, not only spikes. Use color triggers for atmosphere.
- Build large levels in sections of up to a few hundred objects (create_level for the first \
section, then add_to_level for the rest).
- The player sees an ASCII preview and must approve every change; if they decline, adjust.
- Remind them to open the level in the editor and playtest -- you can't run the game.
- Keep chat replies short and friendly; GD slang is fine."""

TOOLS = [
    {
        "name": "list_my_levels",
        "description": "List the levels in the player's Create tab (name, description, object count, verified).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "inspect_my_level",
        "description": ("Read one of the player's levels: stats (object count, gamemodes, triggers, "
                        "length) plus the objects in a column window, in your grid format, and an "
                        "ASCII preview. Use start_col/end_col to page through long levels."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Level name (partial match ok)."},
                "start_col": {"type": "number", "description": "Default 0."},
                "end_col": {"type": "number", "description": "Default start_col + 150."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_level",
        "description": "Create a new level at the top of the Create tab containing the given objects.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "objects": {"type": "array", "items": OBJECT_SCHEMA},
            },
            "required": ["name", "objects"],
        },
    },
    {
        "name": "add_to_level",
        "description": ("Add objects to one of the player's existing levels. With placement "
                        "'after_end' (default), col 0 of your objects lands 4 cells after the last "
                        "existing object; with 'absolute', cols are used as-is."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "objects": {"type": "array", "items": OBJECT_SCHEMA},
                "placement": {"type": "string", "enum": ["after_end", "absolute"]},
            },
            "required": ["name", "objects"],
        },
    },
    {
        "name": "search_online_levels",
        "description": ("Search the official Geometry Dash servers for inspiration. search_type: "
                        "search, most_downloaded, most_liked, trending, recent, featured, awarded, "
                        "hall_of_fame. May fail if the servers are unreachable."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "search_type": {"type": "string", "enum": sorted(gd_api.SEARCH_TYPES)},
            },
        },
    },
    {
        "name": "get_online_level",
        "description": ("Download an online level by ID and get its stats (gamemodes, triggers, "
                        "most used objects). Use -1 for today's daily, -2 for the weekly demon."),
        "input_schema": {
            "type": "object",
            "properties": {"level_id": {"type": "integer"}},
            "required": ["level_id"],
        },
    },
]
# Requests are streamed, so let big object lists stream as they're written (we validate ourselves).
for _tool in TOOLS:
    _tool["eager_input_streaming"] = True


class Assistant:
    def __init__(self, save_path=None, auto_yes=False):
        self.client = anthropic.Anthropic()
        self.save_path = save_path
        self.auto_yes = auto_yes
        self._levels = None
        self.messages = []

    @property
    def levels(self):
        if self._levels is None:
            self._levels = LocalLevels(self.save_path)
        return self._levels

    # --- tools ---------------------------------------------------------------

    def _require_str(self, args, key):
        if not isinstance(args.get(key), str) or not args[key].strip():
            raise DesignError(f"'{key}' must be a non-empty string")
        return args[key]

    def _confirm(self, title, objects, col_offset=0):
        print(f"\n--- {title} ({len(objects)} objects) ---")
        print(preview(objects, col_offset))
        if self.auto_yes:
            return True, ""
        answer = input("Write this to your save? [y = yes / n = no / or type feedback] ").strip()
        if answer.lower() in ("y", "yes"):
            return True, ""
        return False, "" if answer.lower() in ("", "n", "no") else answer

    def tool_list_my_levels(self, args):
        return self.levels.summaries()

    def tool_inspect_my_level(self, args):
        level = self.levels.find(self._require_str(args, "name"))
        level_string = self.levels.level_string(level)
        start = args.get("start_col", 0) if isinstance(args.get("start_col"), (int, float)) else 0
        end = args.get("end_col") if isinstance(args.get("end_col"), (int, float)) else start + 150
        grid = [from_gd_object(gd_api.parse_kv(o, ","))
                for o in level_string.split(";")[1:] if o]
        window = sorted((o for o in grid if start <= o["col"] <= end), key=lambda o: (o["col"], o["row"]))
        drawable = [o for o in window if o["type"] in PALETTE]
        return {
            "name": level.get("k2"),
            "stats": gd_api.analyze_level_string(level_string),
            "last_col": max((o["col"] for o in grid), default=0),
            "window": [start, end],
            "objects_in_window": window[:800],
            "truncated": len(window) > 800,
            "preview": preview(drawable),
        }

    def tool_create_level(self, args):
        name = self._require_str(args, "name")
        objects = args.get("objects")
        validate(objects)
        ok, feedback = self._confirm(f"New level '{name}'", objects)
        if not ok:
            return {"written": False, "player_feedback": feedback or "Player declined."}
        self.levels.create_level(name, str(args.get("description") or ""),
                                 [to_gd_object(o) for o in objects])
        self.levels.save()
        return {"written": True, "level": name, "objects": len(objects)}

    def tool_add_to_level(self, args):
        level = self.levels.find(self._require_str(args, "name"))
        objects = args.get("objects")
        validate(objects)
        offset = 0
        if args.get("placement", "after_end") == "after_end":
            existing = [from_gd_object(gd_api.parse_kv(o, ","))
                        for o in self.levels.level_string(level).split(";")[1:] if o]
            offset = int(max((o["col"] for o in existing), default=-4)) + 4
        ok, feedback = self._confirm(f"Add to '{level.get('k2')}' starting at col {offset}",
                                     objects, offset)
        if not ok:
            return {"written": False, "player_feedback": feedback or "Player declined."}
        self.levels.append_objects(level, [to_gd_object(o, offset) for o in objects])
        self.levels.save()
        return {"written": True, "level": level.get("k2"), "objects": len(objects),
                "placed_from_col": offset}

    def tool_search_online_levels(self, args):
        levels = gd_api.search_levels(str(args.get("query") or ""),
                                      str(args.get("search_type") or "search"))
        return levels[:10]

    def tool_get_online_level(self, args):
        if not isinstance(args.get("level_id"), int):
            raise DesignError("'level_id' must be an integer")
        return gd_api.get_level(args["level_id"])

    def run_tool(self, block):
        handler = getattr(self, f"tool_{block.name}", None)
        if handler is None:
            return f"Unknown tool {block.name}", True
        args = block.input if isinstance(block.input, dict) else {}
        try:
            return json.dumps(handler(args), ensure_ascii=False), False
        except FileNotFoundError as e:
            return f"{e}. The player can pass --save <path to CCLocalLevels.dat>.", True
        except (DesignError, KeyError, gd_api.GDError) as e:
            return str(e), True
        except Exception as e:  # network errors from the GD servers etc.
            return f"{type(e).__name__}: {e}", True

    # --- conversation --------------------------------------------------------

    def _call_claude(self):
        with self.client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=self.messages,
            output_config={"effort": "high"},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
            return stream.get_final_message()

    def ask(self, user_text):
        start = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        while True:
            try:
                response = self._call_claude()
            except ValueError as e:  # tool input JSON that couldn't be parsed at all
                print(f"\n[Claude sent malformed tool input ({e}); try rephrasing]")
                del self.messages[start:]
                return
            print()

            if response.stop_reason == "refusal":
                print("[Claude declined this request.]")
                del self.messages[start:]
                return
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "max_tokens" and tool_uses:
                print("[That design was too big for one reply -- ask for it in smaller sections.]")
                del self.messages[start:]
                return

            self.messages.append({"role": "assistant", "content": response.content})
            if not tool_uses:
                return

            results = []
            for block in tool_uses:
                print(f"  [{block.name.replace('_', ' ')}...]")
                content, is_error = self.run_tool(block)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})


def main():
    parser = argparse.ArgumentParser(description="AI level-building assistant for the GD Create tab")
    parser.add_argument("prompt", nargs="*", help="first message (otherwise starts a chat)")
    parser.add_argument("--save", help="path to CCLocalLevels.dat (auto-detected by default)")
    parser.add_argument("--yes", action="store_true", help="write changes without asking")
    parser.add_argument("--list", action="store_true", help="list your Create tab levels and exit")
    args = parser.parse_args()

    if args.list:
        for lvl in LocalLevels(args.save).summaries():
            print(f"- {lvl['name']}  ({lvl['objects']} objects{', verified' if lvl['verified'] else ''})")
        return

    print("GD Claude -- level-building assistant.  CLOSE GEOMETRY DASH before letting me save,")
    print("or the game will overwrite my changes when it exits.  Type 'quit' to leave.\n")
    assistant = Assistant(args.save, args.yes)
    first = " ".join(args.prompt).strip()
    try:
        if first:
            print(f"you> {first}")
            assistant.ask(first)
        while True:
            text = input("\nyou> ").strip()
            if text.lower() in ("quit", "exit", "q"):
                break
            if text:
                assistant.ask(text)
    except (KeyboardInterrupt, EOFError):
        print()
    except anthropic.AuthenticationError:
        sys.exit("No valid Anthropic API key. Set ANTHROPIC_API_KEY (get one at console.anthropic.com).")


if __name__ == "__main__":
    main()
