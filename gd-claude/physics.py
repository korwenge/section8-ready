"""Jump checker: a simplified Geometry Dash physics simulation.

Plays a level by trying every input timing at once: each frame, every
surviving player state branches into "click" and "don't click", and states
that end up in the same place are merged. If any state reaches the end, the
level is beatable and we return one input sequence that does it. If none do,
we report how far the best attempt got and what killed it.

The numbers are approximations of the real game (60 steps per second, cube
values from community research, ship/UFO/ball/wave tuned to feel right), and
only common objects are understood, so treat the result as a strong hint and
still playtest in the editor.
"""

import time
from dataclasses import dataclass, replace

FPS = 60
CELL = 30
SPEEDS = {0.5: 251.16, 1: 311.58, 2: 387.42, 3: 468.0, 4: 576.0}  # GD units per second
SPEED_PORTALS = {200: 0.5, 201: 1, 202: 2, 203: 3, 1334: 4}
HEADER_SPEEDS = {0: 1, 1: 0.5, 2: 2, 3: 3, 4: 4}
MODE_PORTALS = {12: "cube", 13: "ship", 47: "ball", 111: "ufo", 660: "wave",
                745: "robot", 1331: "spider", 1933: "swing"}
HEADER_MODES = {0: "cube", 1: "ship", 2: "ball", 3: "ufo", 4: "wave", 5: "robot",
                6: "spider", 7: "swing"}
SUPPORTED_MODES = {"cube", "ship", "ball", "ufo", "wave", "robot"}  # robot is checked as a cube
BOUNDS_HEIGHT = {"ship": 300, "ufo": 300, "wave": 300, "ball": 240}

GRAVITY = 0.958199  # cube gravity, units/frame^2
JUMP = 11.180032    # cube jump velocity, units/frame
TERMINAL = 15.0
MINI_JUMP = 0.8

SOLIDS = {1, 2, 3, 4, 5, 6, 7, 83}
SPIKES = {8: (6, 12), 39: (6, 6), 103: (4, 8)}
SAWS = {88: 32, 89: 21.6, 98: 12}
PADS = {35: "yellow", 140: "pink", 67: "blue", 1332: "red"}
ORBS = {36: "yellow", 141: "pink", 84: "blue", 1022: "green", 1330: "black"}
PAD_POWER = {"yellow": 16.0, "pink": 10.4, "red": 20.0}
ORB_POWER = {"yellow": 11.2, "pink": 8.0, "black": -15.0}
NAMES = {1: "block", 8: "spike", 39: "small spike", 103: "spike", 88: "saw", 89: "saw", 98: "saw"}


@dataclass(frozen=True, eq=False)
class Obj:
    kind: str       # solid / spike / saw / pad / orb / portal
    oid: int
    x: float
    y: float
    w: float = 0
    h: float = 0
    r: float = 0
    index: int = 0

    def where(self):
        return f"col {round((self.x - 15) / CELL * 2) / 2:g}, row {round((self.y - 15) / CELL * 2) / 2:g}"


def build_objects(raw_objects):
    """raw_objects: dicts in GD format ({'1': '8', '2': '315', '3': '15', '6': '180', ...})."""
    objs = []
    for i, raw in enumerate(raw_objects):
        try:
            oid = int(raw.get("1", 0))
            x, y = float(raw.get("2", 0)), float(raw.get("3", 0))
            rot = float(raw.get("6", 0) or 0)
            scale = float(raw.get("32", 1) or 1)
        except ValueError:
            continue
        sideways = round(rot) % 180 == 90

        def box(w, h, kind):
            if sideways:
                w, h = h, w
            objs.append(Obj(kind, oid, x, y, w * scale, h * scale, index=i))

        if oid in SOLIDS:
            box(30, 30, "solid")
        elif oid in SPIKES:
            box(*SPIKES[oid], "spike")
        elif oid in SAWS:
            objs.append(Obj("saw", oid, x, y, r=SAWS[oid] * scale, index=i))
        elif oid in PADS:
            box(25, 4, "pad")
        elif oid in ORBS:
            box(36, 36, "orb")
        elif oid in MODE_PORTALS or oid in (10, 11, 99, 101, 286, 287):
            box(34, 86, "portal")
        elif oid in SPEED_PORTALS:
            box(35, 44, "portal")
    return objs


class World:
    def __init__(self, objs):
        self.objs = objs
        self.grid = {}
        for o in objs:
            half = max(o.w / 2, o.r)
            for c in range(int((o.x - half) // CELL), int((o.x + half) // CELL) + 1):
                self.grid.setdefault(c, []).append(o)
        self.end_x = max((o.x for o in objs), default=0) + 2 * CELL
        # Block faces hidden by a neighbouring block (inside a wall) can't be stood on.
        solids = [o for o in objs if o.kind == "solid"]
        faces = {}
        for o in solids:
            faces.setdefault((round(o.x), round(o.y - o.h / 2)), []).append(o)
        self.covered_top, self.covered_bottom = set(), set()
        for o in solids:
            for other in faces.get((round(o.x), round(o.y + o.h / 2)), ()):
                if other is not o:
                    self.covered_top.add(o.index)
                    self.covered_bottom.add(other.index)

        self._near = {}

    def near(self, x):
        """Objects within about a cell of x (cached per column)."""
        c = int(x // CELL)
        found = self._near.get(c)
        if found is None:
            found = list(dict.fromkeys(o for cc in (c - 1, c, c + 1) for o in self.grid.get(cc, ())))
            self._near[c] = found
        return found


@dataclass(frozen=True)
class State:
    x: float
    y: float
    vy: float = 0.0
    gdir: int = 1           # 1 = normal gravity, -1 = upside down
    mode: str = "cube"
    mini: bool = False
    speed: float = 1
    grounded: bool = True
    floor: float = 0.0
    ceil: float | None = None
    holding: bool = False
    last_orb: int = -1
    last_pad: int = -1
    trace: tuple | None = None  # linked list of (col, event, previous, number of events)

    @property
    def size(self):
        if self.mode == "wave":
            return 6 if self.mini else 10
        return 18 if self.mini else 30

    def key(self):
        """States with the same key are treated as the same situation and merged."""
        return (int(self.x), round(self.y), round(self.vy * 2), self.gdir, self.mode, self.mini,
                self.speed, self.ceil, self.holding if self.mode != "cube" else None)


class Death(Exception):
    def __init__(self, cause, x):
        super().__init__(cause)
        self.cause, self.x = cause, x


class Unsupported(Exception):
    def __init__(self, what, x):
        super().__init__(what)
        self.what, self.x = what, x


def _overlap(ax, ay, aw, ah, o):
    if o.kind == "saw":
        nx = min(max(o.x, ax - aw / 2), ax + aw / 2)
        ny = min(max(o.y, ay - ah / 2), ay + ah / 2)
        return (nx - o.x) ** 2 + (ny - o.y) ** 2 < o.r ** 2
    return abs(ax - o.x) * 2 < aw + o.w and abs(ay - o.y) * 2 < ah + o.h


def _event(s, label):
    return (round((s.x - 15) / CELL, 1), label, s.trace, s.trace[3] + 1 if s.trace else 1)


def _effort(s):
    return s.trace[3] if s.trace else 0



def step(s, click, world):
    """Advance one frame. `click` = the player is holding the button this frame."""
    near = world.near(s.x)
    rel = s.vy * s.gdir  # vertical speed relative to gravity: + = away from the floor
    gdir, grounded, trace = s.gdir, s.grounded, s.trace
    last_orb, last_pad = s.last_orb, s.last_pad
    new_press = click and not s.holding
    size = s.size

    # Orbs (need a fresh click while touching them).
    used_orb = False
    if new_press:
        for o in near:
            if o.kind == "orb" and o.index != last_orb and _overlap(s.x, s.y, size, size, o):
                color = ORBS[o.oid]
                if s.mode == "wave":
                    if color in ("blue", "green"):
                        gdir = -gdir
                elif color in ("blue", "green"):
                    gdir, rel = -gdir, -8.0 if color == "blue" else -10.0
                else:
                    rel = ORB_POWER[color] * (MINI_JUMP if s.mini else 1) * (1 if s.mode in ("cube", "robot") else 0.7)
                last_orb, grounded, used_orb = o.index, False, True
                trace = _event(s, f"{color} orb")
                break

    # Gamemode controls.
    mode = s.mode
    if mode in ("cube", "robot"):
        if click and grounded and not used_orb:
            rel, grounded = JUMP * (MINI_JUMP if s.mini else 1), False
            trace = _event(s, "jump")
        elif not grounded:
            rel = max(rel - GRAVITY, -TERMINAL)
    elif mode == "ship":
        rel = max(min(rel + (0.5 if click else -0.6), 7.0), -7.0) if not used_orb else rel
        if click != s.holding:
            trace = _event(s, "hold" if click else "release")
        grounded = False
    elif mode == "ufo":
        if new_press and not used_orb:
            rel = 7.5
            trace = _event(s, "flap")
        else:
            rel = max(rel - 0.65, -10.0)
        grounded = False
    elif mode == "ball":
        if new_press and grounded and not used_orb:
            gdir, rel, grounded = -gdir, 0.0, False
            trace = _event(s, "flip")
        else:
            rel = max(rel - 0.6 * GRAVITY, -12.0)
            grounded = False if rel < 0 else grounded
    elif mode == "wave":
        vx = SPEEDS[s.speed] / FPS
        rel = vx * (2 if s.mini else 1) * (1 if click else -1)
        if click != s.holding:
            trace = _event(s, "hold" if click else "release")

    vy = rel * gdir
    x = s.x + SPEEDS[s.speed] / FPS
    y = s.y + vy
    if mode in ("cube", "robot", "ball"):
        grounded = False  # re-established by landing below

    # Floor / ceiling boundaries.
    half = size / 2
    if y - half < s.floor:
        y, vy = s.floor + half, max(vy, 0.0)
        grounded = gdir == 1 or grounded
    if s.ceil is not None and y + half > s.ceil:
        y, vy = s.ceil - half, min(vy, 0.0)
        grounded = gdir == -1 or grounded
    if s.ceil is None and gdir == -1 and y > s.floor + 1500:
        raise Death("flew off the top of the level (upside down with no ceiling)", x)

    # Solid blocks: land on top, bonk the underside, die if the core hits a side.
    # Work in "gravity space" (multiply by gdir) so upside-down is the same maths.
    near = world.near(x)
    inner = size * 0.3
    snap = half - inner / 2  # how deep you can sink into a surface before the core touches it
    for o in near:
        if o.kind != "solid" or not _overlap(x, y, size, size, o):
            continue
        centre, rel_vy = y * gdir, vy * gdir
        block_top = o.y * gdir + o.h / 2
        block_bottom = o.y * gdir - o.h / 2
        # Which face is the player's floor side depends on gravity.
        floor_covered = o.index in (world.covered_top if gdir == 1 else world.covered_bottom)
        roof_covered = o.index in (world.covered_bottom if gdir == 1 else world.covered_top)
        if rel_vy <= 0 and not floor_covered and block_top - (centre - half) <= snap:
            y, vy, grounded = (block_top + half) * gdir, 0.0, True
        elif rel_vy > 0 and not roof_covered and (centre + half) - block_bottom <= snap:
            if mode in ("cube", "robot"):
                raise Death(f"hit its head on the block at {o.where()}", x)
            y, vy = (block_bottom - half) * gdir, 0.0
    for o in near:
        if o.kind == "solid" and _overlap(x, y, inner, inner, o):
            raise Death(f"crashed into the block at {o.where()}", x)

    # Hazards.
    for o in near:
        if o.kind in ("spike", "saw") and _overlap(x, y, size, size, o):
            raise Death(f"hit the {NAMES.get(o.oid, 'hazard')} at {o.where()}", x)

    state = replace(s, x=x, y=y, vy=vy, gdir=gdir, grounded=grounded, holding=click,
                    last_orb=last_orb, last_pad=last_pad, trace=trace)

    # Pads and portals trigger on touch.
    for o in near:
        if o.kind == "pad" and o.index != state.last_pad and _overlap(x, y, size, size, o):
            color = PADS[o.oid]
            if color == "blue":
                state = replace(state, gdir=-state.gdir, vy=-8.0 * -state.gdir, grounded=False)
            elif state.mode != "wave":
                power = PAD_POWER[color] * (MINI_JUMP if state.mini else 1) * (1 if state.mode in ("cube", "robot") else 0.7)
                state = replace(state, vy=power * state.gdir, grounded=False)
            state = replace(state, last_pad=o.index, trace=_event(state, f"{color} pad"))
        elif o.kind == "portal" and _overlap(x, y, size, size, o):
            state = _portal(state, o)
    return state


def _portal(s, o):
    oid = o.oid
    if oid in SPEED_PORTALS:
        return replace(s, speed=SPEED_PORTALS[oid])
    if oid == 10:
        return replace(s, gdir=1)
    if oid == 11:
        return replace(s, gdir=-1)
    if oid in (99, 101):
        return replace(s, mini=oid == 101)
    if oid in (286, 287):
        if oid == 286:
            raise Unsupported("dual mode", s.x)
        return s
    mode = MODE_PORTALS[oid]
    if mode == s.mode:
        return s
    if mode not in SUPPORTED_MODES:
        raise Unsupported(f"{mode} mode", s.x)
    if mode in BOUNDS_HEIGHT:
        height = BOUNDS_HEIGHT[mode]
        floor = max(0.0, ((o.y - height / 2) // CELL) * CELL)
        ceil = floor + height
        y = min(max(s.y, floor + s.size / 2), ceil - s.size / 2)
    else:
        floor, ceil, y = 0.0, None, s.y
    return replace(s, mode=mode, floor=floor, ceil=ceil, y=y, vy=s.vy * 0.5, grounded=False,
                   trace=_event(s, f"enters {mode}"))


def start_state(header=None):
    """Starting state, reading the level's start settings (gamemode, mini, speed)."""
    header = header or {}
    mode = HEADER_MODES.get(int(header.get("kA2") or 0), "cube")
    speed = HEADER_SPEEDS.get(int(header.get("kA4") or 0), 1)
    s = State(x=0.0, y=15.0, mode="cube", mini=header.get("kA3") == "1", speed=speed)
    s = replace(s, y=s.size / 2)
    if header.get("kA8") == "1":
        raise Unsupported("dual mode", 0)
    if mode != "cube":
        s = _portal(s, Obj("portal", {v: k for k, v in MODE_PORTALS.items()}[mode], 0, 150))
    return s


def _inputs(s, world, frame):
    if s.mode in ("cube", "robot"):
        if s.grounded or any(o.kind == "orb" for o in world.near(s.x)):
            return (False, True)
        return (False,)
    if s.mode in ("ship", "wave", "ufo") and frame % 2:
        return (s.holding,)  # flying modes: decide every other frame (1/30 s) to keep it fast
    return (False, True)


def check(raw_objects, header=None, max_states=250, time_limit=30.0):
    """Can the level be beaten? Returns a dict summary for Claude (and people)."""
    deadline = time.monotonic() + time_limit
    world = World(build_objects(raw_objects))
    end_col = round((world.end_x - 15) / CELL, 1)
    try:
        states = [start_state(header)]
    except Unsupported as e:
        return {"beatable": None, "note": f"can't check: level starts in {e.what}"}
    furthest = None
    frame = 0
    while states:
        frame += 1
        merged, winners = {}, []
        for s in states:
            for click in _inputs(s, world, frame):
                try:
                    nxt = step(s, click, world)
                except Death as d:
                    if furthest is None or d.x > furthest.x:
                        furthest = d
                    continue
                except Unsupported as u:
                    return {"beatable": None, "checked_up_to_col": round(u.x / CELL, 1),
                            "note": f"no problems up to here, but I can't simulate {u.what} yet"}
                if nxt.x >= world.end_x:
                    winners.append(nxt)
                    continue
                key = nxt.key()
                if key not in merged or _effort(nxt) < _effort(merged[key]):
                    merged[key] = nxt  # prefer the route with fewer inputs
        if winners:
            best = min(winners, key=_effort)
            return {"beatable": True, "level_end_col": end_col, "one_way_to_beat_it": _route(best)}
        states = list(merged.values())
        if states and frame % 60 == 0 and time.monotonic() > deadline:
            return {"beatable": None, "checked_up_to_col": round((states[0].x - 15) / CELL, 1),
                    "note": "ran out of time; no impossible parts found up to here"}
        if len(states) > max_states:
            states.sort(key=lambda st: (st.mode, st.y, st.vy))
            stride = len(states) / max_states
            states = [states[int(i * stride)] for i in range(max_states)]
    return {
        "beatable": False,
        "level_end_col": end_col,
        "best_attempt_reached_col": round((furthest.x - 15) / CELL, 1) if furthest else 0,
        "died_because": f"the player {furthest.cause}" if furthest else "unknown",
        "tip": "the obstacle above (or just before it) is impossible -- move, remove or give a pad/orb there",
    }


def _route(s, limit=60):
    events, node = [], s.trace
    while node:
        col, label, node, _ = node
        events.append(f"{label} @ col {col:g}")
    events.reverse()
    return events[:limit] + ([f"... {len(events) - limit} more"] if len(events) > limit else [])
