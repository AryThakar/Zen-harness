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


def seeing(*turns) -> ScriptedProvider:
    """A scripted model on a server whose vision part is loaded."""
    model = ScriptedProvider(list(turns))
    model.sees_images = lambda: True
    return model


SAVE = {"name": "plot.py", "purpose": "Draws a line.", "how_it_works": "matplotlib.", "inputs_outputs": "line.png",
        "how_to_run": "Run it.", "limits": "One line."}


def test_a_script_edits_an_image_by_its_id_into_a_new_image(ctx):
    from app.tools.scratch import Scratch, run_code
    original = ctx.ws.add_image(png((60, 30)), "photo.png")
    code = ("from PIL import Image, ImageDraw\nimg = Image.open('image_1').convert('RGB')\n"
            "ImageDraw.Draw(img).text((5, 5), 'Hi', fill='white')\nimg.resize((120, 60)).save('photo_hi.png')\n"
            "print(img.size)")
    scratch = Scratch()
    r = run_code(ctx.ws, scratch, code)
    assert r.status == "ok", r.result
    new = r.result["files"][0]["id"]
    assert ctx.ws.kind(new) == "image" and ctx.ws.entry(new)["meta"]["width"] == 120 and new != original
    assert scratch.images == [new] and "next" not in r.result  # the runner shows it; no tool to call


def test_the_coder_is_shown_what_its_script_drew(ctx):
    plot = "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.title('Sales')\nplt.savefig('line.png')\n"
    model = seeing(turn(call("run_code", code=plot)), turn({**call("save"), "input": SAVE}))
    out = run_worker(model, PROFILES["coder"], "Draw a line titled Sales.", ctx)
    assert "view_image" not in {t["name"] for t in model.requests[0]["tools"]}
    shown = model.requests[1]["messages"][-1]  # after the run's result, before the next step
    assert shown["role"] == "user" and shown["content"].startswith("[The picture your script made: image_1 (line.png, ")
    assert len(shown["images"]) == 1 and shown["images"][0]["mime"] == "image/png"
    assert out["file"] and not out.get("warnings") and not ctx.look.seen  # no separate vision call


def test_an_image_the_task_names_or_the_coder_reads_is_shown(ctx):
    ctx.ws.add_image(png(), "passengers.png")
    model = seeing(turn(call("read_file", file="image_1")), turn({**call("save"), "input": SAVE}))
    run_worker(model, PROFILES["coder"], "Add the caption 'Q3' to the passengers chart.", ctx)
    opening = model.requests[0]["messages"][0]
    assert "[The picture the task names: image_1 (passengers.png, 40x20).]" in opening["content"]
    assert len(opening["images"]) == 1  # on the opening itself: one user turn
    assert model.requests[1]["messages"][-1]["content"] == "[The picture you read: image_1 (passengers.png, 40x20).]"


def test_without_vision_on_the_server_nothing_is_shown(ctx):
    plot = "import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.savefig('line.png')\n"
    model = ScriptedProvider([turn(call("run_code", code=plot)), turn({**call("save"), "input": SAVE})])
    run_worker(model, PROFILES["coder"], "Draw the passengers line.", ctx)
    assert not any(m.get("images") for r in model.requests for m in r["messages"])


def test_only_the_newest_pictures_stay_in_view():
    from app.assistant.context import fewer_images
    pic = {"mime": "image/png", "data": "x"}
    messages = [{"role": "user", "content": "[a]", "images": [pic, pic]}, {"role": "tool", "content": "{}"},
                {"role": "user", "content": "[b]", "images": [pic]}]
    view = fewer_images(messages, keep=2)
    assert [len(m.get("images") or []) for m in view] == [1, 0, 1]
    assert view[0]["content"] == "[a] [1 of its pictures no longer shown]" and messages[0]["images"] == [pic, pic]


def test_a_save_named_after_the_picture_keeps_the_script(ctx):
    """Seen on the real model: the coder called save with the edited image's name ('passengers_modified.png')
    and the save was refused - the script is what save keeps; the picture is kept already."""
    ctx.ws.add_image(png(), "passengers.png")
    edit = ("from PIL import Image\nimg = Image.open('image_1')\nimg.resize((80, 20)).save('passengers_modified.png')\n"
            "print(img.size)")
    report = {**SAVE, "name": "passengers_modified.png", "inputs_outputs": "image_1 in, passengers_modified.png out"}
    model = seeing(turn(call("run_code", code=edit)), turn({**call("save"), "input": report}))
    out = run_worker(model, PROFILES["coder"], "Make the passengers chart twice as wide.", ctx)
    assert out["name"] == "passengers_modified.py" and not out.get("warnings")
    assert ctx.ws.kind(out["file"]) == "file" and ctx.ws.entry("image_2")["meta"]["width"] == 80
