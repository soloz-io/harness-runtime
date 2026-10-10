"""The ask_user question shape: plain options, media options, and the carousel rules."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.middleware.human_interaction.ask_user import AskUserQuestion, ask_user

CLIP = {
    "kind": "video",
    "url": "https://cdn.test/a.mp4",
    "aspect": "vertical",
    "poster": "https://cdn.test/a.jpg",
}


def test_plain_string_options_stay_a_list():
    q = AskUserQuestion(question="Move on?", options=["Yes", "No"])
    assert q.layout == "list"
    assert q.model_dump()["options"] == ["Yes", "No"]


def test_carousel_options_carry_their_media():
    q = AskUserQuestion(
        question="Pick a clip",
        layout="carousel",
        options=[{"label": "Take 1", "badge": "Recommended", "media": CLIP}],
    )
    option = q.model_dump()["options"][0]
    assert option["label"] == "Take 1"
    assert option["media"] == CLIP


def test_media_aspect_defaults_to_square():
    q = AskUserQuestion(
        question="Pick a face",
        layout="carousel",
        options=[{"label": "A", "media": {"kind": "image", "url": "https://cdn.test/a.png"}}],
    )
    assert q.model_dump()["options"][0]["media"]["aspect"] == "square"


@pytest.mark.parametrize(
    "options",
    [
        ["Yes", {"label": "B", "media": CLIP}],
        [{"label": "A"}, {"label": "B", "media": CLIP}],
    ],
)
def test_carousel_rejects_options_without_media(options):
    with pytest.raises(ValidationError, match="options without it: 0"):
        AskUserQuestion(question="Pick", layout="carousel", options=options)


def test_carousel_needs_options():
    with pytest.raises(ValidationError, match="needs options"):
        AskUserQuestion(question="Pick", layout="carousel")


def test_unknown_media_kind_is_rejected():
    with pytest.raises(ValidationError):
        AskUserQuestion(
            question="Pick",
            layout="carousel",
            options=[{"label": "A", "media": {"kind": "pdf", "url": "https://cdn.test/a.pdf"}}],
        )


def test_tool_schema_validates_the_agent_call():
    with pytest.raises(ValidationError):
        ask_user.args_schema.model_validate(
            {"questions": [{"question": "Pick", "layout": "carousel", "options": ["Yes"]}]}
        )


def test_a_carousel_is_never_an_approval_dialog():
    with pytest.raises(ValidationError, match='cannot be asked with type="approval"'):
        ask_user.args_schema.model_validate(
            {
                "type": "approval",
                "questions": [
                    {
                        "question": "Pick",
                        "layout": "carousel",
                        "options": [{"label": "A", "media": CLIP}],
                    }
                ],
            }
        )


def test_a_list_question_may_still_be_an_approval():
    ask_user.args_schema.model_validate(
        {"type": "approval", "questions": [{"question": "Move on?", "options": ["Yes", "No"]}]}
    )


def test_a_carousel_clarification_pauses_with_its_options(monkeypatch):
    import core.middleware.human_interaction.ask_user as mod

    seen = {}
    monkeypatch.setattr(
        mod, "interrupt", lambda value: seen.update(value) or {"decisions": [{"message": "A"}]}
    )
    q = AskUserQuestion(question="Pick", layout="carousel", options=[{"label": "A", "media": CLIP}])
    assert ask_user.func(questions=[q]) == "A"
    assert seen["args"]["questions"][0]["options"][0]["media"]["url"] == CLIP["url"]


def test_buttons_are_worded_by_the_caller_and_reach_the_dialog(monkeypatch):
    import core.middleware.human_interaction.ask_user as mod

    seen = {}
    monkeypatch.setattr(
        mod, "interrupt", lambda value: seen.update(value) or {"decisions": [{"message": "ok"}]}
    )
    buttons = mod.AskUserButtons(submit="Use this voice", skip="Record again")
    ask_user.func(questions=[AskUserQuestion(question="Approve your voice?")], buttons=buttons)
    # Only what was set: the dialog keeps its defaults for the rest.
    assert seen["args"]["buttons"] == {"submit": "Use this voice", "skip": "Record again"}


def test_without_buttons_the_dialog_keeps_its_defaults(monkeypatch):
    import core.middleware.human_interaction.ask_user as mod

    seen = {}
    monkeypatch.setattr(
        mod, "interrupt", lambda value: seen.update(value) or {"decisions": [{"message": "ok"}]}
    )
    ask_user.func(questions=[AskUserQuestion(question="Ready?")])
    assert seen["args"]["buttons"] is None


@pytest.mark.parametrize("buttons", [{"submit": ""}, {"skip": "x" * 41}])
def test_a_button_label_is_short_and_not_empty(buttons):
    with pytest.raises(ValidationError):
        ask_user.args_schema(questions=[{"question": "Q"}], buttons=buttons)
