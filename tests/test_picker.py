"""Searchable picker: ranking (phone codes, names, cities) and keyboard behaviour."""

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from autoapply import picker

PHONE = ["Afghanistan (+93)", "Antigua and Barbuda (+1268)", "Australia (+61)", "Canada (+1)", "India (+91)",
         "United Kingdom (+44)", "United States (+1)", "Uzbekistan (+998)"]
CITIES = ["Austin, TX", "Boston, MA", "New York, NY", "Newark, NJ", "York, PA"]


def test_phone_code_exact_matches_first():
    assert picker.rank(PHONE, "+1") == ["Canada (+1)", "United States (+1)", "Antigua and Barbuda (+1268)"]
    assert picker.rank(PHONE, "1")[:2] == ["Canada (+1)", "United States (+1)"]
    assert picker.rank(PHONE, "+44") == ["United Kingdom (+44)"]
    assert picker.rank(PHONE, "+9") == ["Afghanistan (+93)", "India (+91)", "Uzbekistan (+998)"]


def test_phone_by_region_name():
    assert picker.rank(PHONE, "united") == ["United Kingdom (+44)", "United States (+1)"]
    assert picker.rank(PHONE, "states") == ["United States (+1)"]
    assert picker.rank(PHONE, "us")[0] == "United States (+1)"  # initials beat the substring in "Australia"


def test_city_state():
    assert picker.rank(CITIES, "york") == ["York, PA", "New York, NY"]
    assert picker.rank(CITIES, "new york")[0] == "New York, NY"
    assert picker.rank(CITIES, "austin tx") == ["Austin, TX"]
    assert picker.rank(CITIES, "new") == ["New York, NY", "Newark, NJ"]
    assert picker.rank(CITIES, "zzz") == []
    assert picker.rank(CITIES, "") == CITIES


def _run(question, keys: str):
    with create_pipe_input() as inp:
        inp.send_text(keys)
        app = question.application
        app.input, app.output = inp, DummyOutput()
        return question.ask()


UP, DOWN, ENTER, TAB = "\x1b[A", "\x1b[B", "\r", "\t"


@pytest.mark.parametrize("keys,want", [
    (ENTER, "Afghanistan (+93)"),
    ("+1" + ENTER, "Canada (+1)"),
    ("+1" + DOWN + ENTER, "United States (+1)"),
    ("+1" + UP + ENTER, "Skip"),  # wraps to the pinned option
    ("united st" + ENTER, "United States (+1)"),
    ("zzz" + ENTER, "Skip"),  # nothing matches: only the pinned choice is left
])
def test_select_keys(keys, want):
    assert _run(picker.select("Phone code", PHONE, pinned=["Skip"]), keys) == want


def test_select_backspace_widens_filter():
    assert _run(picker.select("City", CITIES), "yorkz\x7f\x7f\x7f\x7f\x7fbos" + ENTER) == "Boston, MA"


def test_checkbox_tab_checks_and_enter_takes_highlighted():
    assert _run(picker.checkbox("Cities", CITIES), "new" + TAB + TAB + ENTER) == ["New York, NY", "Newark, NJ"]
    assert _run(picker.checkbox("Cities", CITIES), "austin" + ENTER) == ["Austin, TX"]
