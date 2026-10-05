"""view_image and the coder working with images - the vision model replaced by a recording stand-in."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.providers.base import ModelTurn
from app.providers.scripted import ScriptedProvider, call
from app.assistant.registry import ToolContext, execute
from app.assistant.workers import PROFILES, run_worker
from app.tools.search_index import SearchIndex
from app.tools.vision import MAX_SIDE, prepare
from app.tools.workspace import Workspace


class Eyes:
    """Answers like the vision model would, and remembers what it was shown."""

    def __init__(self, answer="A red square."):
        self.answer, self.seen = answer, []

    def __call__(self, image: bytes, mime: str, question: str) -> str:
        self.seen.append((Image.open(io.BytesIO(image)).size, mime, question))
        return self.answer


def png(size=(40, 20), color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "ws")
    return ToolContext(ws=ws, index=SearchIndex(tmp_path / "ws" / "search.sqlite", None), look=Eyes())


def turn(*calls) -> ModelTurn:
    return ModelTurn(text="", tool_calls=list(calls), stop="tool_use")


def test_view_image_answers_by_name_and_says_how_far_to_trust_it(ctx):
    ctx.ws.add_image(png(), "logo.png")
    r = execute(ctx, "view_image", {"image": "logo.png", "question": "What colour is it?"})
    assert r.status == "ok" and r.result["image"] == "image_1" and r.result["answer"] == "A red square."
    assert r.result["size"] == "40x20" and "rough for exact numbers" in r.result["note"]
    assert ctx.look.seen == [((40, 20), "image/png", "What colour is it?")]
    assert "Give question" in execute(ctx, "view_image", {"image": "image_1", "question": " "}).result["error"]["message"]
    blind = ToolContext(ws=ctx.ws, index=ctx.index)
    assert "vision part" in execute(blind, "view_image", {"image": "image_1", "question": "x"}).result["error"]["message"]


def test_large_photos_are_sent_small():
    photo = io.BytesIO()
    Image.new("RGB", (4000, 3000), "blue").save(photo, format="JPEG")
    data, mime = prepare(photo.getvalue())
    assert mime == "image/jpeg" and max(Image.open(io.BytesIO(data)).size) == MAX_SIDE


def test_a_script_edits_an_image_by_its_id_into_a_new_image(ctx):
    from app.tools.scratch import Scratch, run_code
    original = ctx.ws.add_image(png((60, 30)), "photo.png")
    code = ("from PIL import Image, ImageDraw\nimg = Image.open('image_1').convert('RGB')\n"
            "ImageDraw.Draw(img).text((5, 5), 'Hi', fill='white')\nimg.resize((120, 60)).save('photo_hi.png')\n"
            "print(img.size)")
    r = run_code(ctx.ws, Scratch(can_look=True), code)
    assert r.status == "ok", r.result
    new = r.result["files"][0]["id"]
    assert ctx.ws.kind(new) == "image" and ctx.ws.entry(new)["meta"]["width"] == 120 and new != original
    assert r.result["next"].startswith(f"It made {new}: look with view_image(image='{new}'")
    assert "next" not in run_code(ctx.ws, Scratch(), code).result  # a helper that can't look isn't sent to


def test_the_coder_looks_at_what_it_drew_before_saving(ctx):
    plot = "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.title('Sales')\nplt.savefig('line.png')\n"
    model = ScriptedProvider([turn(call("run_code", code=plot)),
                              turn(call("view_image", image="image_1", question="Is the title 'Sales'?")),
                              turn({**call("save"), "input": {"name": "plot.py", "purpose": "Draws a line.",
                                                              "how_it_works": "matplotlib.", "inputs_outputs": "line.png",
                                                              "how_to_run": "Run it.", "limits": "One line."}})])
    ctx.look = Eyes("Yes, the title says Sales.")
    out = run_worker(model, PROFILES["coder"], "Draw a line titled Sales.", ctx)
    run_result = model.requests[1]["messages"][-1]["content"]
    assert "look with view_image(image='image_1'" in run_result
    assert "the title says Sales" in model.requests[2]["messages"][-1]["content"]
    assert out["file"] and not out.get("warnings")


def test_a_picture_saved_unseen_is_sent_back_to_be_looked_at(ctx):
    plot = "import matplotlib.pyplot as plt\nplt.bar(['a', 'b'], [1, 2])\nplt.savefig('bars.png')\n"
    report = {"name": "bars.py", "purpose": "Draws two bars.", "how_it_works": "matplotlib.",
              "inputs_outputs": "bars.png", "how_to_run": "python bars.py", "limits": "Two bars."}
    model = ScriptedProvider([turn(call("run_code", code=plot)), turn({**call("save"), "input": report}),
                              turn(call("view_image", image="image_1", question="Two bars?")),
                              turn({**call("save"), "input": report})])
    out = run_worker(model, PROFILES["coder"], "Draw two bars.", ctx)
    told = model.requests[2]["messages"][-1]["content"]
    assert "you made image_1 but never looked at it" in told
    assert out["file"] and not out.get("warnings") and len(ctx.look.seen) == 1
