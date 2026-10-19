import base64
import io
import json
import sys
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.server import AppState, serve  # noqa: E402
from catalog import Catalog  # noqa: E402
from deco import ChangeSet, grid_to_objects, image_to_grid, render_region  # noqa: E402
from deco_agent import DecoSession  # noqa: E402
from gdlevel import GDObject, Level  # noqa: E402
from save_file import DEFAULT_HEADER, LocalLevels, encode_level_string, encrypt  # noqa: E402

LEVEL = DEFAULT_HEADER + "1,1,2,15,3,15,21,3,24,7;1,8,2,45,3,15;1,1888,2,300,3,150,24,-3,155,2,999,x;"


@pytest.fixture(scope="module")
def catalog():
    return Catalog(download=False)


def make_save(tmp_path, level=LEVEL):
    xml = ('<?xml version="1.0"?><plist version="1.0" gjver="2.0"><dict><k>LLM_01</k><d><k>_isArr</k><t />'
           f"<k>k_0</k><d><k>kCEK</k><i>4</i><k>k2</k><s>Deco Test</s><k>k4</k><s>{encode_level_string(level)}</s>"
           "<k>k5</k><s>me</s><k>k13</k><t /><k>k21</k><i>2</i></d></d></dict></plist>")
    path = tmp_path / "CCLocalLevels.dat"
    path.write_bytes(encrypt(xml))
    return path


# --- level model ------------------------------------------------------------------

def test_level_round_trip_is_exact():
    assert Level(LEVEL).to_string() == LEVEL


def test_unknown_properties_survive_edits():
    level = Level(LEVEL)
    level.add({"1": 211, "2": 10, "3": 20, "21": 5})
    out = Level(level.to_string())
    assert "1,1888,2,300,3,150,24,-3,155,2,999,x" in [o.raw for o in out.objects]
    assert out.objects[-1].raw == "1,211,2,10,3,20,21,5"


def test_colour_channels_are_allocated_and_reused():
    level = Level(LEVEL)
    red = level.channel_for((255, 0, 0))
    assert red not in (3, 1000, 1001) and level.channel_for((255, 0, 0)) == red
    blue = level.channel_for((0, 0, 255), opacity=0.5)
    assert blue != red
    reread = Level(level.to_string())
    assert reread.color_rgb(red) == (255, 0, 0, 1.0) and reread.color_rgb(blue) == (0, 0, 255, 0.5)


def test_build_orders_keys_like_gd():
    assert GDObject.build({"21": 4, "3": 15, "2": 7.5, "1": 211, "128": 0.25}).raw == "1,211,2,7.5,3,15,21,4,128,0.25"


# --- deco engine ------------------------------------------------------------------

def test_pixel_art_merges_rectangles():
    rows = ["RRRR", "RRRR", "..B.", "RRRR"]
    objects = grid_to_objects(rows, {"R": "#ff0000", "B": "#0000ff"}, left=0, top=120, pixel=30)
    assert len(objects) == 3  # 4x2 red block, the blue pixel, the last red row
    big = next(o for o in objects if o["129"] == 2)
    assert (big["2"], big["3"], big["128"]) == (60, 90, 4)


def test_image_conversion_keeps_transparency():
    img = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
    for x in range(5, 15):
        for y in range(2, 8):
            img.putpixel((x, y), (255, 128, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    rows, palette = image_to_grid(buf.getvalue(), 10, 10, colours=4)
    assert len(rows) == 5 and len(rows[0]) == 10
    assert rows[0].startswith("..") and len(palette) >= 1


def test_changeset_apply_and_json(catalog):
    level = Level(LEVEL)
    cs = ChangeSet(add=[{"1": 211, "2": 0, "3": 0, "_hex": "#123456"}, {"1": 211, "2": 5, "3": 0, "_hex": "#123456"}],
                   remove=["1,8,2,45,3,15"], summary="x", channels={1000: ["#000000", 1.0]})
    report = ChangeSet.from_json(json.loads(json.dumps(cs.to_json()))).apply(level)
    assert report == {"added": 2, "removed": 1, "new_colours": 1, "missing": 0}
    assert level.color_rgb(1000)[:3] == (0, 0, 0)
    added = level.objects[-2:]
    assert added[0].props["21"] == added[1].props["21"]


def test_render_region_png(catalog):
    png = render_region(Level(LEVEL), catalog, 0, -30, 400, 300, extra=[{"1": 211, "2": 100, "3": 100, "_hex": "#ff0000"}])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --- agent ------------------------------------------------------------------------

def tool_use(name, inp, i):
    return NS(type="tool_use", name=name, input=inp, id=i)


def test_agent_builds_a_proposal(catalog):
    session = DecoSession(Level(LEVEL), catalog, client=object())
    replies = iter([
        NS(stop_reason="tool_use", content=[
            tool_use("get_objects", {"x0": 0, "y0": 0, "x1": 100, "y1": 100}, "a"),
            tool_use("draw_pixel_art", {"rows": ["AB", "BA"], "palette": {"A": "#fff", "B": "#000"},
                                        "left": 0, "top": 60, "pixel_size": 15}, "b"),
            tool_use("propose_changes", {"add": [{"id": 503, "x": 1, "y": 2, "channel": 3, "props": {"24": 5}}],
                                         "remove_refs": [1], "set_channels": [{"channel": 1000, "color": "#101010"}]}, "c"),
            tool_use("propose_changes", {"add": [{"id": 1, "x": 0, "y": 0, "props": {"2": 5}}]}, "d"),
            tool_use("look_at_area", {"x0": 0, "y0": 0, "x1": 100, "y1": 100}, "e"),
        ]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="done")]),
    ])
    session._call = lambda: next(replies)
    assert session.ask("make it cool", (0, 0, 300, 300)) == "done"
    results = session.messages[2]["content"]
    assert json.loads(results[0]["content"])["objects"][1]["ref"] == 1
    assert results[3]["is_error"] and "not 1/2/3" in results[3]["content"]
    assert results[4]["content"][0]["type"] == "image"
    assert len(session.proposal.add) == 5 and session.proposal.remove == ["1,8,2,45,3,15"]
    assert session.proposal.channels == {1000: ["#101010", 1.0]}
    # the selection picture was sent with the prompt
    assert session.messages[0]["content"][0]["type"] == "image"


def test_agent_refusal_rolls_back(catalog):
    session = DecoSession(Level(LEVEL), catalog, client=object())
    session._call = lambda: NS(stop_reason="refusal", content=[])
    with pytest.raises(RuntimeError):
        session.ask("x", (0, 0, 10, 10))
    assert session.messages == []


# --- app backend --------------------------------------------------------------------

class FakeClient:
    """Pretends to be Claude: draws one square, then says done."""

    def __init__(self):
        self.calls = 0
        self.beta = NS(messages=self)

    def stream(self, **kw):
        self.calls += 1
        done = isinstance(kw["messages"][-1]["content"], list) and kw["messages"][-1]["content"][0].get("type") == "tool_result"
        msg = NS(stop_reason="end_turn", content=[NS(type="text", text="ok")]) if done else NS(
            stop_reason="tool_use", content=[tool_use("draw_pixel_art", {"rows": ["A"], "palette": {"A": "#ff0000"},
                                                                         "left": 0, "top": 30}, "t")])
        stream = NS(text_stream=iter(["ok"] if done else []), get_final_message=lambda: msg)
        return _Ctx(stream)


class _Ctx:
    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


@pytest.fixture
def app(tmp_path):
    gd = {"running": True}
    state = AppState(cfg_dir=tmp_path / "cfg", client_factory=FakeClient, gd_check=lambda: gd["running"])
    state.update_settings({"save_path": str(make_save(tmp_path)), "api_key": "sk-test"})
    return state, gd


def wait_job(state, job_id):
    for _ in range(200):
        if state.jobs[job_id]["status"] != "running":
            return state.jobs[job_id]
        threading.Event().wait(0.02)
    raise AssertionError("job didn't finish")


def test_full_flow_queue_then_apply_when_gd_closes(app, tmp_path):
    state, gd = app
    assert [lv["name"] for lv in state.level_list()] == ["Deco Test"]
    job = wait_job(state, state.start_job("Deco Test", "add a red pixel", [0, 0, 300, 300]))
    assert job["status"] == "done" and job["text"] == "ok"
    assert state.proposal_view("Deco Test")["adds"] == 1
    state.accept("Deco Test")
    assert len(state.queue) == 1 and state.proposal_view("Deco Test")["adds"] == 0
    assert len(state.level_view("Deco Test")["queued_shapes"]) == 1
    with pytest.raises(RuntimeError):
        state.apply_queue()  # GD still open
    gd["running"] = False
    report = state.apply_queue()
    assert report["added"] == 1 and state.queue == []
    levels = LocalLevels(state.save_path())
    level = Level(levels.level_string(levels.find("Deco Test")))
    assert level.objects[-1].id == 211 and level.color_rgb(int(level.objects[-1].props["21"]))[:3] == (255, 0, 0)
    assert len(state.backups()) == 1
    # the queue survives restarts (it's on disk) and is now empty
    assert json.loads((tmp_path / "cfg" / "queue.json").read_text()) == []


def test_convert_image_without_api_key(tmp_path):
    state = AppState(cfg_dir=tmp_path / "cfg", gd_check=lambda: False)
    state.update_settings({"save_path": str(make_save(tmp_path)), "api_key": ""})
    img = Image.new("RGB", (8, 8), (0, 200, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    result = state.convert_image("Deco Test", [0, 0, 120, 120], url, 15, 4)
    assert result["pixels"] == "8x8" and result["objects"] == 1
    with pytest.raises(RuntimeError, match="API key"):
        state.start_job("Deco Test", "hi", [0, 0, 10, 10])


def test_restore_backup(app):
    state, gd = app
    gd["running"] = False
    original = state.save_path().read_bytes()
    state.queue.append({"level": "Deco Test", "changes": ChangeSet(add=[{"1": 1, "2": 0, "3": 0}]).to_json(),
                        "summary": "x", "time": 0})
    state.apply_queue()
    assert state.save_path().read_bytes() != original
    state.restore_backup(state.backups()[0]["name"])
    assert state.save_path().read_bytes() == original


def test_http_api_requires_token(app):
    state, _ = app
    server, url, stop = serve(state)
    try:
        base = url.split("/?")[0]
        token = url.split("t=")[1]
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/api/levels")
        assert err.value.code == 403
        req = urllib.request.Request(base + "/api/levels", headers={"X-Token": token})
        assert json.load(urllib.request.urlopen(req))[0]["name"] == "Deco Test"
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "<title>GD Claude</title>" in page
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/../server.py")
    finally:
        stop.set()
        server.shutdown()
