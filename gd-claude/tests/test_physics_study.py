import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gd_api  # noqa: E402
import physics  # noqa: E402
import study  # noqa: E402
from level_builder import to_gd_object  # noqa: E402
from save_file import DEFAULT_HEADER  # noqa: E402


def run(objs, header=None):
    return physics.check([gd_api.parse_kv(to_gd_object(o), ",") for o in objs], header)


def spike(col, row=0, **kw):
    return {"type": "spike", "col": col, "row": row, **kw}


def block(col, row=0):
    return {"type": "block", "col": col, "row": row}


def portal(kind, col, row=1):
    return {"type": f"{kind}_portal", "col": col, "row": row}


@pytest.mark.parametrize("name, objs, beatable", [
    ("single spike", [spike(10)], True),
    ("triple spike is the max", [spike(10), spike(11), spike(12)], True),
    ("quad spike", [spike(c) for c in range(10, 14)], False),
    ("2-high wall", [block(10), block(10, 1)], True),
    ("3-high wall", [block(10), block(10, 1), block(10, 2)], False),
    ("pad over 4-high wall", [{"type": "yellow_pad", "col": 8, "row": 0}] + [block(10, r) for r in range(4)], True),
    ("orb over spike pit", [{"type": "yellow_orb", "col": 13, "row": 2}] + [spike(c) for c in range(12, 18)], True),
    ("spike pit without orb", [spike(c) for c in range(12, 18)], False),
    ("low ceiling over spike", [block(c, 2) for c in range(8, 20)] + [spike(14)], False),
    ("ship corridor", [portal("ship", 5)] + [block(c, 0) for c in range(8, 30)] + [block(c, 6) for c in range(8, 30)]
     + [spike(c, 1) for c in range(14, 30, 6)], True),
    ("ship corridor sealed", [portal("ship", 5)] + [block(c, 0) for c in range(8, 30)] + [block(c, 6) for c in range(8, 30)]
     + [block(20, r) for r in range(1, 6)], False),
    ("wave zigzag", [portal("wave", 5)] + [block(c, 0) for c in range(8, 30)] + [block(c, 7) for c in range(8, 30)]
     + [spike(c, 1) for c in range(12, 30, 4)] + [spike(c, 6, rotation=180) for c in range(14, 30, 4)], True),
    ("upside down walk", [portal("gravity_up", 6)] + [block(c, 4) for c in range(5, 30)]
     + [spike(15, 3, rotation=180)], True),
])
def test_jump_checker(name, objs, beatable):
    result = run(objs)
    assert result["beatable"] is beatable, result
    if beatable:
        assert result["one_way_to_beat_it"] or not any(o["type"] == "spike" for o in objs)
    else:
        assert "died_because" in result and result["best_attempt_reached_col"] >= 0


def test_death_report_names_the_obstacle():
    result = run([spike(c) for c in range(10, 14)])
    assert "spike at col 13" in result["died_because"] or "spike at col 12" in result["died_because"]


def test_unsupported_mode_is_reported_not_guessed():
    result = run([portal("spider", 5), spike(20)])
    assert result["beatable"] is None and "spider" in result["note"]


def test_level_header_start_settings():
    # A low ceiling over a spike: the cube bonks its head, the ship flies through.
    tunnel = [block(c, 3) for c in range(3, 20)] + [spike(10)]
    assert run(tunnel)["beatable"] is False
    assert run(tunnel, {"kA2": "1"})["beatable"] is True


def make_level_string(objs):
    return DEFAULT_HEADER + ";".join(to_gd_object(o) for o in objs) + ";"


def test_design_metrics():
    objs = ([spike(10), spike(11), spike(12), spike(20), spike(30)] + [portal("ship", 40)]
            + [{"type": "speed_fast", "col": 40, "row": 1}] + [block(c) for c in range(41, 60)]
            + [{"type": "yellow_orb", "col": 50, "row": 3}, {"type": "color_trigger", "col": 1, "row": 5,
                                                              "color": {"channel": "background", "r": 0, "g": 0, "b": 0}}])
    m = study.design_metrics(make_level_string(objs))
    assert m["longest_spike_run"] == 3 and m["spike_runs_of_3_plus"] == 1
    assert m["median_break_between_hazards_cols"] == 9
    assert [s["mode"] for s in m["gamemode_sections"]] == ["cube", "ship"]
    assert m["gamemode_sections"][0]["cols"] == 40
    assert m["triggers"] == 1 and m["speed_portals"] == 1
    assert 4 < m["play_time_seconds"] < 6  # 40 cols at 1x + 19 cols at 2x


def test_study_with_fake_servers(monkeypatch):
    levels = [{"id": i, "name": f"L{i}", "creator": "c", "difficulty": "Easy", "stars": 2,
               "likes": 9, "rating": "", "length": "Short"} for i in range(3)]
    monkeypatch.setattr(gd_api, "search_levels", lambda q, t, difficulty=None: levels)
    data = {0: [spike(10)], 1: [spike(10), spike(11)], 2: [spike(10), spike(11), spike(12)]}
    monkeypatch.setattr(gd_api, "download_level", lambda i: ({"id": i}, make_level_string(data[i])))
    result = study.study("awarded", "easy", count=3, pause=0)
    assert [lvl["name"] for lvl in result["levels"]] == ["L0", "L1", "L2"]
    assert result["medians"]["longest_spike_run"] == 2


def test_assistant_check_design_tool(tmp_path, monkeypatch):
    import gdclaude
    from test_gdclaude import make_assistant, make_save, tool_use
    assistant = make_assistant(make_save(tmp_path))
    content, err = assistant.run_tool(tool_use("check_design", {"objects": [spike(c) for c in range(10, 14)]}))
    assert not err and '"beatable": false' in content
    content, err = assistant.run_tool(tool_use("check_design", {"objects": [spike(5)], "level_name": "first"}))
    assert not err and '"beatable": true' in content and '"your_col_0_is_level_col": 23' in content
