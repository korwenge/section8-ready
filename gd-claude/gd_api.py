"""Tiny client for the official Geometry Dash servers (boomlings.com).

The GD servers speak a quirky form-POST protocol: every request carries a
shared "secret", and responses are colon-separated key/value strings.
This module hides all of that and returns plain dicts.
"""

import base64
import gzip
import zlib
from collections import Counter

import requests

BASE_URL = "https://www.boomlings.com/database"
SECRET = "Wmfd2893gb7"  # public secret every GD client sends, not a credential

LENGTHS = {0: "Tiny", 1: "Short", 2: "Medium", 3: "Long", 4: "XL", 5: "Platformer"}
DEMON_DIFFICULTIES = {3: "Easy Demon", 4: "Medium Demon", 0: "Hard Demon",
                      5: "Insane Demon", 6: "Extreme Demon"}
NORMAL_DIFFICULTIES = {0: "N/A", 10: "Easy", 20: "Normal", 30: "Hard",
                       40: "Harder", 50: "Insane"}
SEARCH_TYPES = {"search": 0, "most_downloaded": 1, "most_liked": 2, "trending": 3,
                "recent": 4, "featured": 6, "magic": 7, "awarded": 11,
                "hall_of_fame": 16}
RATINGS = {0: "", 1: "Epic", 2: "Legendary", 3: "Mythic"}

# A few well-known object IDs, so level stats are readable.
OBJECT_NAMES = {
    1: "block", 8: "spike", 39: "small spike", 36: "yellow orb", 84: "blue orb",
    141: "pink orb", 35: "yellow pad", 67: "blue pad", 12: "cube portal",
    13: "ship portal", 47: "ball portal", 111: "UFO portal", 660: "wave portal",
    745: "robot portal", 1331: "spider portal", 1933: "swing portal",
    200: "slow speed", 201: "normal speed", 202: "fast speed", 203: "faster speed",
    1334: "fastest speed", 899: "color trigger", 901: "move trigger",
    1007: "alpha trigger", 1268: "spawn trigger", 1346: "rotate trigger",
    1049: "toggle trigger", 1616: "stop trigger", 1006: "pulse trigger",
    1347: "follow trigger", 1913: "zoom trigger", 1595: "touch trigger",
    1611: "count trigger", 1811: "instant count trigger", 1912: "random trigger",
}
GAMEMODE_PORTALS = {12, 13, 47, 111, 660, 745, 1331, 1933}
SPEED_PORTALS = {200, 201, 202, 203, 1334}


class GDError(Exception):
    pass


def _post(endpoint, **data):
    data.setdefault("secret", SECRET)
    data.setdefault("gameVersion", 22)
    data.setdefault("binaryVersion", 42)
    # The GD servers reject requests that send a normal User-Agent.
    resp = requests.post(f"{BASE_URL}/{endpoint}", data=data,
                         headers={"User-Agent": ""}, timeout=15)
    resp.raise_for_status()
    text = resp.text.strip()
    if text == "-1" or not text:
        raise GDError(f"{endpoint}: nothing found")
    return text


def parse_kv(chunk, sep=":"):
    """'1:Bloodbath:2:10565740' -> {'1': 'Bloodbath', '2': '10565740'}"""
    parts = chunk.split(sep)
    return dict(zip(parts[0::2], parts[1::2]))


def b64decode_text(value):
    if not value:
        return ""
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", "replace")
    except ValueError:
        return value


def difficulty_name(raw):
    if raw.get("25") == "1":
        return "Auto"
    if raw.get("17") == "1":
        return DEMON_DIFFICULTIES.get(int(raw.get("43") or 0), "Demon")
    if int(raw.get("8") or 0) == 0:
        return "N/A"
    return NORMAL_DIFFICULTIES.get(int(raw.get("9") or 0), "N/A")


def level_summary(raw, creators=None, songs=None):
    creators = creators or {}
    songs = songs or {}
    song_id = raw.get("35", "0")
    summary = {
        "id": int(raw.get("1", 0)),
        "name": raw.get("2", ""),
        "description": b64decode_text(raw.get("3", "")),
        "creator": creators.get(raw.get("6"), "unknown"),
        "difficulty": difficulty_name(raw),
        "stars": int(raw.get("18") or 0),
        "downloads": int(raw.get("10") or 0),
        "likes": int(raw.get("14") or 0),
        "length": LENGTHS.get(int(raw.get("15") or 0), "?"),
        "coins": int(raw.get("37") or 0),
        "verified_coins": raw.get("38") == "1",
        "featured": int(raw.get("19") or 0) > 0,
        "rating": RATINGS.get(int(raw.get("42") or 0), ""),
        "object_count": int(raw.get("45") or 0),
    }
    if song_id != "0":
        summary["song"] = songs.get(song_id, f"custom song #{song_id}")
    else:
        summary["song"] = f"official song #{int(raw.get('12') or 0) + 1}"
    return summary


def _parse_level_list(text):
    sections = text.split("#")
    creators = {}
    if len(sections) > 1 and sections[1]:
        for entry in sections[1].split("|"):
            player_id, name, *_ = entry.split(":") + ["", ""]
            creators[player_id] = name
    songs = {}
    if len(sections) > 2 and sections[2]:
        for entry in sections[2].split("~:~"):
            song = parse_kv(entry, "~|~")
            if "1" in song:
                songs[song["1"]] = f"{song.get('2', '?')} by {song.get('4', '?')}"
    return [level_summary(parse_kv(chunk), creators, songs)
            for chunk in sections[0].split("|") if chunk]


def search_levels(query="", search_type="search", page=0):
    text = _post("getGJLevels21.php", str=query,
                 type=SEARCH_TYPES.get(search_type, 0), page=page)
    return _parse_level_list(text)


def decode_level_string(data):
    """Level data is base64 + gzip/zlib. Returns the raw ';'-separated string."""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return gzip.decompress(raw).decode("utf-8", "replace")
    except OSError:
        return zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8", "replace")


def analyze_level_string(level_string):
    """Turn a decoded level string into a compact stats dict Claude can reason about."""
    objects = [parse_kv(o, ",") for o in level_string.split(";")[1:] if o]
    ids = Counter()
    max_x = 0.0
    gamemodes = []
    speeds = []
    for obj in objects:
        try:
            obj_id = int(obj.get("1", 0))
            x = float(obj.get("2", 0))
        except ValueError:
            continue
        ids[obj_id] += 1
        max_x = max(max_x, x)
        if obj_id in GAMEMODE_PORTALS:
            gamemodes.append((x, OBJECT_NAMES[obj_id]))
        elif obj_id in SPEED_PORTALS:
            speeds.append((x, OBJECT_NAMES[obj_id]))
    trigger_count = sum(n for i, n in ids.items() if "trigger" in OBJECT_NAMES.get(i, ""))
    return {
        "total_objects": len(objects),
        "level_width_units": round(max_x),
        "unique_object_types": len(ids),
        "trigger_count": trigger_count,
        "most_used_objects": [
            {"id": i, "name": OBJECT_NAMES.get(i, "other"), "count": n}
            for i, n in ids.most_common(10)
        ],
        "gamemode_sequence": [name for _, name in sorted(gamemodes)][:40],
        "speed_changes": [name for _, name in sorted(speeds)][:40],
    }


def get_level(level_id):
    """Download a level. Use -1 for the current daily and -2 for the weekly demon."""
    text = _post("downloadGJLevel22.php", levelID=level_id)
    raw = parse_kv(text.split("#")[0])
    summary = level_summary(raw)
    if raw.get("4"):
        try:
            summary["stats"] = analyze_level_string(decode_level_string(raw["4"]))
        except (ValueError, OSError, zlib.error) as e:
            summary["stats"] = {"error": f"could not decode level data: {e}"}
    return summary


def get_user(username):
    text = _post("getGJUsers20.php", str=username)
    raw = parse_kv(text.split("#")[0].split("|")[0])
    return {
        "username": raw.get("1", ""),
        "player_id": int(raw.get("2") or 0),
        "account_id": int(raw.get("16") or 0),
        "stars": int(raw.get("3") or 0),
        "moons": int(raw.get("52") or 0),
        "demons": int(raw.get("4") or 0),
        "diamonds": int(raw.get("46") or 0),
        "secret_coins": int(raw.get("13") or 0),
        "user_coins": int(raw.get("17") or 0),
        "creator_points": int(raw.get("8") or 0),
    }


def get_comments(level_id, top=True, page=0):
    text = _post("getGJComments21.php", levelID=level_id, page=page,
                 mode=1 if top else 0, total=0)
    comments = []
    for entry in text.split("#")[0].split("|"):
        if not entry:
            continue
        comment_part, _, user_part = entry.partition(":")
        comment = parse_kv(comment_part, "~")
        user = parse_kv(user_part, "~")
        comments.append({
            "author": user.get("1", "?"),
            "text": b64decode_text(comment.get("2", "")),
            "likes": int(comment.get("4") or 0),
            "percent": int(comment.get("10") or 0),
            "age": comment.get("9", ""),
        })
    return comments
