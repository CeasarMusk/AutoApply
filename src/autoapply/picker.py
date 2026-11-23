"""A searchable single-choice picker for long option lists (countries, phone codes, cities…).

Type to filter, ↑/↓ (or PgUp/PgDn) to move through what's left, Enter to pick. Matches are
ranked, so the best hit is on top: "+1" puts the +1 countries first, "united" or "us" find
United States, "austin tx" finds "Austin, TX".
"""

from __future__ import annotations

import re

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import IsDone
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style
from questionary import Question

VISIBLE = 10  # rows of the list shown at once

# "+1", "(+44)", "+1-268", "(1)": a dialing code in an option's text
_CODE_RE = re.compile(r"(?:\+|\(\s*\+?)\s*(\d{1,4}(?:[\s-]\d{1,4})?)")
_CODE_QUERY_RE = re.compile(r"\+?\s*\d[\d\s-]*")

STYLE = Style.from_dict({
    "qmark": "fg:ansicyan bold",
    "question": "bold",
    "answer": "fg:ansicyan bold",
    "instruction": "fg:ansibrightblack",
    "prompt": "fg:ansicyan",
    "pointer": "fg:ansicyan bold",
    "highlighted": "reverse",
    "pinned": "fg:ansibrightblack",
    "empty": "fg:ansiyellow",
    "count": "fg:ansibrightblack",
})


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w+]+", " ", s.lower()).split())


def _codes(option: str) -> list[str]:
    return [re.sub(r"\D", "", m) for m in _CODE_RE.findall(option)]


def _score(option: str, query: str) -> int | None:
    """Lower is better; None means no match."""
    q = _norm(query)
    if not q:
        return 0
    if _CODE_QUERY_RE.fullmatch(query.strip()):
        digits = re.sub(r"\D", "", query)
        codes = _codes(option)
        if digits in codes:
            return 0
        if any(c.startswith(digits) for c in codes):
            return 1
        if codes:
            return None  # a phone-code list: "+4" shouldn't match "+1 (234)" through some other digit
    o = _norm(option)
    words = o.split()
    if o == q:
        return 2
    if o.startswith(q):
        return 3
    if f" {q}" in f" {o}":  # starts at a word: "york" in "New York", "new york" in "Greater New York"
        return 4
    initials = "".join(w[0] for w in words if w[0].isalpha())
    if len(q) >= 2 and " " not in q and initials.startswith(q):  # "us" → United States
        return 5
    if q in o:
        return 6
    if all(any(w.startswith(t) for w in words) for t in q.split()):  # "austin tx" → "Austin, TX"
        return 7
    return None


def rank(options: list[str], query: str) -> list[str]:
    """Options matching `query`, best first (ties keep the original order)."""
    scored = [(s, i, o) for i, o in enumerate(options) if (s := _score(o, query)) is not None]
    return [o for _, _, o in sorted(scored)]


def select(message: str, options: list[str], pinned: list[str] | None = None,
           default: str | None = None) -> Question:
    """Searchable select. `pinned` choices (e.g. Skip) always stay at the bottom of the list.
    Returns a questionary Question, so `.ask()` gives the choice or None on Ctrl-C."""
    return _picker(message, options, pinned or [], default, multiple=False)


def checkbox(message: str, options: list[str]) -> Question:
    """Searchable multi-select: Tab checks/unchecks (space is part of the search), Enter confirms.
    Enter with nothing checked takes the highlighted option. `.ask()` gives a list, or None on Ctrl-C."""
    return _picker(message, options, [], None, multiple=True)


def _picker(message: str, options: list[str], pinned: list[str], default: str | None,
            multiple: bool) -> Question:
    query = Buffer(multiline=False)
    state = {"idx": 0, "top": 0, "answer": None}
    checked: list[str] = []

    def shown() -> list[str]:
        return rank(options, query.text) + pinned

    def clamp():
        items = shown()
        state["idx"] = max(0, min(state["idx"], len(items) - 1))
        if state["idx"] < state["top"]:
            state["top"] = state["idx"]
        elif state["idx"] >= state["top"] + VISIBLE:
            state["top"] = state["idx"] - VISIBLE + 1
        state["top"] = max(0, min(state["top"], max(0, len(items) - VISIBLE)))

    def on_change(_):
        state["idx"] = state["top"] = 0
    query.on_text_changed += on_change

    if default in options:
        state["idx"] = options.index(default)
        clamp()

    def header():
        tokens = [("class:qmark", "? "), ("class:question", message), ("", " ")]
        if state["answer"] is not None:
            tokens.append(("class:answer", ", ".join(state["answer"]) if multiple else state["answer"]))
        elif multiple:
            tokens.append(("class:instruction", "(type to filter, ↑/↓ to move, Tab to check, Enter when done)"))
        else:
            tokens.append(("class:instruction", "(type to filter, ↑/↓ to move, Enter to pick)"))
        return tokens

    def body():
        clamp()
        items = shown()
        matched = len(items) - len(pinned)
        tokens = []
        if matched == 0:
            tokens.append(("class:empty", f"  no option matches “{query.text}”\n"))
        for i in range(state["top"], min(len(items), state["top"] + VISIBLE)):
            text = items[i]
            cls = "class:pinned" if i >= matched else ""
            if multiple:
                text = ("[x] " if text in checked else "[ ] ") + text
            if i == state["idx"]:
                tokens += [("class:pointer", "» "), ("class:highlighted", text), ("", "\n")]
            else:
                tokens += [("", "  "), (cls, text), ("", "\n")]
        if len(items) > VISIBLE:
            tokens.append(("class:count", f"  ({state['idx'] + 1}/{len(items)}, ↑/↓ or PgUp/PgDn for more)\n"))
        if multiple and checked:
            tokens.append(("class:count", f"  checked: {', '.join(checked)}"))
        return tokens

    kb = KeyBindings()

    def move(n: int, wrap: bool = True):
        count = len(shown())
        if not count:
            return
        state["idx"] = (state["idx"] + n) % count if wrap else max(0, min(state["idx"] + n, count - 1))

    kb.add("down")(lambda e: move(1))
    kb.add("up")(lambda e: move(-1))
    if multiple:
        @kb.add("tab")
        def _(event):
            items = shown()
            if items:
                clamp()
                item = items[state["idx"]]
                checked.remove(item) if item in checked else checked.append(item)
                move(1)
    else:
        kb.add("tab")(lambda e: move(1))
        kb.add("s-tab")(lambda e: move(-1))
    kb.add("pagedown")(lambda e: move(VISIBLE, wrap=False))
    kb.add("pageup")(lambda e: move(-VISIBLE, wrap=False))

    @kb.add("enter")
    def _(event):
        items = shown()
        if multiple:
            if not checked and items:
                clamp()
                checked.append(items[state["idx"]])
            state["answer"] = [o for o in options if o in checked]
            event.app.exit(result=state["answer"])
        elif items:
            clamp()
            state["answer"] = items[state["idx"]]
            event.app.exit(result=state["answer"])

    @kb.add("escape")
    def _(event):
        query.text = ""

    @kb.add("c-c")
    def _(event):
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    layout = Layout(HSplit([
        Window(FormattedTextControl(header), dont_extend_height=True, wrap_lines=True),
        ConditionalContainer(HSplit([
            VSplit([Window(FormattedTextControl([("class:prompt", "  / ")]), width=4, height=1),
                    Window(BufferControl(query), height=1)]),
            Window(FormattedTextControl(body), dont_extend_height=True),
        ]), filter=~IsDone()),
    ]), focused_element=query)
    return Question(Application(layout=layout, key_bindings=kb, style=STYLE, full_screen=False))
