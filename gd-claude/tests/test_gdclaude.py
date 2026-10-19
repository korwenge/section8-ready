import base64
import gzip
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gd_api  # noqa: E402
import gdclaude  # noqa: E402
from level_builder import DesignError, from_gd_object, preview, to_gd_object, validate  # noqa: E402
from save_file import DEFAULT_HEADER, LocalLevels, encrypt, encode_level_string  # noqa: E402

EXISTING_LEVEL = DEFAULT_HEADER + "1,1,2,315,3,15;1,8,2,345,3,15;1,13,2,600,3,45;"


def make_save(tmp_path, mac=False):
    xml = (
        '<?xml version="1.0"?><plist version="1.0" gjver="2.0"><dict>'
        "<k>LLM_01</k><d><k>_isArr</k><t /><k>k_0</k><d><k>kCEK</k><i>4</i>"
        "<k>k2</k><s>My First Level</s><k>k4</k><s>" + encode_level_string(EXISTING_LEVEL) + "</s>"
        "<k>k5</k><s>RobTop</s><k>k13</k><t /><k>k21</k><i>2</i><k>k48</k><i>3</i></d></d>"
        "<k>LLM_02</k><i>38</i></dict></plist>"
    )
    path = tmp_path / "CCLocalLevels.dat"
    path.write_bytes(encrypt(xml, mac=mac))
    return path


@pytest.mark.parametrize("mac", [False, True])
def test_save_round_trip(tmp_path, mac):
    path = make_save(tmp_path, mac)
    levels = LocalLevels(path)
    assert levels.mac == mac
    assert [s["name"] for s in levels.summaries()] == ["My First Level"]

    levels.create_level("AI Level", "made by claude", ["1,8,2,315,3,15"])
    levels.append_objects(levels.find("first"), ["1,1,2,900,3,15"])
    levels.save()

    reloaded = LocalLevels(path)
    assert [s["name"] for s in reloaded.summaries()] == ["AI Level", "My First Level"]
    assert reloaded.summaries()[0]["description"] == "made by claude"
    assert reloaded.summaries()[0]["objects"] == 1
    assert reloaded.level_string(reloaded.find("AI Level")).endswith("1,8,2,315,3,15;")
    assert reloaded.stats(reloaded.find("My First Level"))["total_objects"] == 4
    assert reloaded.top["LLM_02"] == 38
    assert len(list(tmp_path.glob("*.backup-*"))) == 1


def test_grid_conversion_round_trips():
    obj = {"type": "spike", "col": 10, "row": 0}
    assert to_gd_object(obj) == "1,8,2,315,3,15"
    assert to_gd_object({"type": "spike", "col": 2, "row": 3, "rotation": 180}) == "1,8,2,75,3,105,6,180"
    for o in [obj, {"type": "yellow_pad", "col": 4, "row": 0}, {"type": "small_spike", "col": 1, "row": 2}]:
        assert from_gd_object(gd_api.parse_kv(to_gd_object(o), ",")) == o
    assert from_gd_object(gd_api.parse_kv(to_gd_object(obj, col_offset=5), ","))["col"] == 15


def test_color_trigger():
    trig = {"type": "color_trigger", "col": 0, "row": 5,
            "color": {"channel": "background", "r": 10, "g": 20, "b": 30}}
    validate([trig])
    assert to_gd_object(trig).endswith("7,10,8,20,9,30,10,0.5,23,1000")


@pytest.mark.parametrize("bad", [
    [],
    [{"type": "laser", "col": 1, "row": 0}],
    [{"type": "spike", "col": "1", "row": 0}],
    [{"type": "spike", "col": 1, "row": 500}],
    [{"type": "color_trigger", "col": 1, "row": 0}],
])
def test_validate_rejects_bad_designs(bad):
    with pytest.raises(DesignError):
        validate(bad)


def test_preview_draws_ground_and_objects():
    art = preview([{"type": "block", "col": 0, "row": 0}, {"type": "spike", "col": 2, "row": 0},
                   {"type": "spike", "col": 2, "row": 3, "rotation": 180}])
    lines = art.splitlines()
    assert lines[-3].endswith("|# ^")
    assert "v" in lines[0]


def test_parse_online_level_list():
    desc = base64.urlsafe_b64encode(b"hello").decode()
    text = (f"1:10565740:2:Bloodbath:3:{desc}:6:503085:8:10:9:50:10:999:14:50:15:3:17:1:43:6:18:10:35:0:12:5:37:0"
            "#503085:Riot:37415##1:0:10#hash")
    [level] = gd_api._parse_level_list(text)
    assert level["name"] == "Bloodbath" and level["creator"] == "Riot"
    assert level["difficulty"] == "Extreme Demon" and level["description"] == "hello"
    assert level["length"] == "Long" and level["song"] == "official song #6"


def test_level_data_decoding():
    data = base64.urlsafe_b64encode(gzip.compress(EXISTING_LEVEL.encode())).decode()
    stats = gd_api.analyze_level_string(gd_api.decode_level_string(data))
    assert stats["total_objects"] == 3
    assert stats["gamemode_sequence"] == ["ship portal"]


# --- the assistant loop, with a fake Claude ----------------------------------

def tool_use(name, inp, id_="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=id_)


def text(t):
    return SimpleNamespace(type="text", text=t)


def test_assistant_builds_a_level(tmp_path, monkeypatch):
    path = make_save(tmp_path)
    assistant = gdclaude.Assistant.__new__(gdclaude.Assistant)
    assistant.save_path, assistant.auto_yes, assistant._levels, assistant.messages = path, True, None, []

    design = [{"type": "block", "col": 8, "row": 0}, {"type": "spike", "col": 12, "row": 0}]
    replies = iter([
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("inspect_my_level", {"name": "first"}, "a")]),
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("create_level", {"name": "Sunrise", "objects": design}, "b"),
                                                         tool_use("add_to_level", {"name": "first", "objects": design}, "c")]),
        SimpleNamespace(stop_reason="end_turn", content=[text("Done!")]),
    ])
    monkeypatch.setattr(assistant, "_call_claude", lambda: next(replies))
    assistant.ask("build me something")

    inspect_result = json.loads(assistant.messages[2]["content"][0]["content"])
    assert inspect_result["last_col"] == 19.5
    assert [o["type"] for o in inspect_result["objects_in_window"]] == ["block", "spike", "ship_portal"]

    results = assistant.messages[4]["content"]
    assert len(results) == 2 and not any(r["is_error"] for r in results)
    assert json.loads(results[1]["content"])["placed_from_col"] == 23

    levels = LocalLevels(path)
    assert [s["name"] for s in levels.summaries()] == ["Sunrise", "My First Level"]
    assert levels.stats(levels.find("first"))["total_objects"] == 5


def test_assistant_reports_bad_tool_input_and_declines(tmp_path, monkeypatch):
    path = make_save(tmp_path)
    assistant = gdclaude.Assistant.__new__(gdclaude.Assistant)
    assistant.save_path, assistant.auto_yes, assistant._levels, assistant.messages = path, False, None, []
    monkeypatch.setattr("builtins.input", lambda _: "make it harder")

    replies = iter([
        SimpleNamespace(stop_reason="tool_use", content=[
            tool_use("create_level", {"name": "X", "objects": [{"type": "nope", "col": 0, "row": 0}]}, "a"),
            tool_use("create_level", {"name": "Y", "objects": [{"type": "spike", "col": 9, "row": 0}]}, "b")]),
        SimpleNamespace(stop_reason="end_turn", content=[text("ok")]),
    ])
    monkeypatch.setattr(assistant, "_call_claude", lambda: next(replies))
    assistant.ask("go")

    bad, declined = assistant.messages[2]["content"]
    assert bad["is_error"] and "palette" in bad["content"]
    assert json.loads(declined["content"]) == {"written": False, "player_feedback": "make it harder"}
    assert [s["name"] for s in LocalLevels(path).summaries()] == ["My First Level"]


def test_refusal_rolls_back_turn(tmp_path, monkeypatch):
    assistant = gdclaude.Assistant.__new__(gdclaude.Assistant)
    assistant.save_path, assistant.auto_yes, assistant._levels, assistant.messages = None, True, None, []
    monkeypatch.setattr(assistant, "_call_claude",
                        lambda: SimpleNamespace(stop_reason="refusal", content=[]))
    assistant.ask("hi")
    assert assistant.messages == []
