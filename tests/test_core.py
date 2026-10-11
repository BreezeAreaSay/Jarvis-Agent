"""core.handle: порядок событий во всех ветках, исключения, отмена, тайминги, журнал, шпион мозга, зрение."""

import base64
import importlib
import json
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from jarvis import vision
from jarvis.config import Config, VisionConfig
from jarvis.context import Context
from jarvis.core import NEEDS_GPT, Core, brain_timings
from jarvis.events import Done, Items, Level, Status, TextChunk
from pc import screen
from pc.result import Result
from pc.screen import Shot
from pc.windows import WindowInfo


def install(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> types.ModuleType:
    """Подменить модуль соседа фейком (и в sys.modules, и атрибутом пакета)."""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    package, _, attr = name.rpartition(".")
    monkeypatch.setattr(importlib.import_module(package), attr, mod, raising=False)
    return mod


@dataclass
class Hit:
    action: str
    args: dict[str, Any]
    confidence: float = 1.0


@dataclass
class Route:
    level: str
    reason: str
    text: str
    deep: bool = False
    hit: Hit | None = None
    local_only: bool = False
    vision: str = ""


@dataclass
class Outcome:
    kind: str = "done"
    ok: bool = True
    text: str = "Готово"
    items: list[str] = field(default_factory=list)
    autohide: bool = True
    action: str = ""


@dataclass
class Decision:
    kind: str = "tool"
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    timings: dict[str, Any] = field(default_factory=dict)


class FakeHands:
    def __init__(self, decision: Decision | Exception | None = None, server: str = "ready") -> None:
        self.decision = decision or Decision(tool="vol", args={"delta": -10})
        self.status: dict[str, Any] = {"server": server}
        self.calls: list[str] = []
        self.on_decide: Any = None

    def decide(self, text: str, ctx: Any) -> Decision:
        self.calls.append(text)
        if self.on_decide:
            self.on_decide()
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


class SpyBrain:
    """Шпион мозга: считает ask и cancel; поток — заданные события."""

    def __init__(self, events: list[Any] | None = None, fail_after: int | None = None) -> None:
        self.events = (
            events if events is not None else [TextChunk("Отв"), TextChunk("ет"), Done(ok=True, text="Ответ")]
        )
        self.fail_after = fail_after
        self.asks: list[tuple[str, bool]] = []
        self.cancels = 0
        self.ready = True

    def ask(self, text: str, ctx: Any, deep: bool = False):
        self.asks.append((text, deep))
        for i, ev in enumerate(self.events):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("codex умер")
            if isinstance(ev, Done) and self.cancels:
                yield Done(ok=False, text="прервано", cancelled=True)
                return
            yield ev

    def cancel(self) -> None:
        self.cancels += 1


class FakeJournal:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


@pytest.fixture
def mods(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Фейковые router и execute: route — что вернуть, outcome — результат действия, calls — вызовы run."""
    state = types.SimpleNamespace(route=None, outcome=Outcome(), calls=[], exec_error=None, route_error=None)

    def route(text: str, ctx: Any, mode: str) -> Route:
        if state.route_error:
            raise state.route_error
        return state.route or Route("hands", "hands", text)

    def run(tool: str, args: dict, text: str, ctx: Any, source: str, dry: bool = False) -> Outcome:
        state.calls.append((tool, args, text, source, dry))
        if state.exec_error:
            raise state.exec_error
        return state.outcome

    install(monkeypatch, "jarvis.router", route=route)
    install(monkeypatch, "jarvis.execute", run=run)
    return state


def check_order(events: list[Any]) -> Done:
    assert events, "нет событий"
    assert isinstance(events[0], Level), events
    assert isinstance(events[-1], Done), events
    assert sum(isinstance(e, Done) for e in events) == 1, events
    return events[-1]


def collect(core: Core, text: str = "сделай потише", **kw: Any) -> list[Any]:
    return list(core.handle(text, Context(), **kw))


# --- ветки -----------------------------------------------------------------------------------------


def test_grammar_branch(mods: types.SimpleNamespace) -> None:
    mods.route = Route("grammar", "grammar:find", "найди отчёт", hit=Hit("find", {"query": "отчёт"}))
    mods.outcome = Outcome(text="Нашёл 2", items=[r"C:\Users\me\отчёт.docx", r"C:\Users\me\отчёт2.docx"])
    journal = FakeJournal()
    events = collect(Core(Config(), FakeHands(), SpyBrain(), journal), "найди отчёт", dry=True)
    done = check_order(events)
    assert events[0] == Level("grammar", "grammar:find")
    assert isinstance(events[1], Items) and len(events[1].items) == 2
    assert done.ok and done.text == "Нашёл 2" and done.autohide and done.level == "grammar"
    assert mods.calls == [("find", {"query": "отчёт"}, "найди отчёт", "grammar", True)]
    for key in ("route", "grammar", "exec", "total"):
        assert isinstance(done.timings[key], float)
    rec = journal.records[0]
    assert rec["level"] == "grammar" and rec["tool"] == "find" and rec["args"] == {"query": "отчёт"}
    assert (
        rec["ok"] is True and rec["error"] == "" and rec["text"] == "найди отчёт" and rec["mode"] == "normal"
    )


def test_hands_tool_branch(mods: types.SimpleNamespace) -> None:
    timings = {"cache_n": 1183, "prompt_n": 14, "prompt_ms": 20.0, "predicted_ms": 90.0, "total_ms": 350.0}
    hands = FakeHands(Decision(tool="vol", args={"delta": -10}, timings={**timings, "predicted_n": 9}))
    journal = FakeJournal()
    events = collect(Core(Config(), hands, SpyBrain(), journal), hotkey_ms=42.0)
    done = check_order(events)
    assert events[0] == Level("hands", "hands", model="4b")
    assert done.level == "hands" and done.ok
    assert mods.calls[0][:2] == ("vol", {"delta": -10}) and mods.calls[0][3] == "hands"
    assert done.timings["hands"] == timings  # только ключи журнала
    assert done.timings["hotkey_to_window"] == 42.0
    assert journal.records[0]["hands"]["prompt_n"] == 14
    assert journal.records[0]["hotkey_to_window"] == 42.0


def test_hands_starting_status(mods: types.SimpleNamespace) -> None:
    hands = FakeHands(server="starting")
    hands.on_decide = lambda: hands.status.update(server="ready")
    events = collect(Core(Config(), hands, SpyBrain()))
    check_order(events)
    statuses = [e for e in events if isinstance(e, Status)]
    assert statuses == [
        Status("Запускаю руки…", key="hands-start"),
        Status("Запускаю руки…", done=True, ok=True, key="hands-start"),
    ]
    events = collect(Core(Config(), FakeHands(OSError("нет сервера"), server="unknown"), SpyBrain()))
    assert Status("Подключаюсь к рукам…", done=True, ok=False, key="hands-start") in events
    events = collect(Core(Config(), FakeHands(server="ready"), SpyBrain()))
    assert not any(isinstance(e, Status) for e in events)


@pytest.mark.parametrize(("tool", "key"), [("reply", "text"), ("clarify", "question")])
def test_hands_reply_and_clarify(mods: types.SimpleNamespace, tool: str, key: str) -> None:
    hands = FakeHands(Decision(tool=tool, args={key: "Что именно сделать?"}))
    done = check_order(collect(Core(Config(), hands, SpyBrain())))
    assert done.ok and done.text == "Что именно сделать?" and done.autohide is False
    assert mods.calls == []


def test_ask_gpt_goes_to_brain(mods: types.SimpleNamespace) -> None:
    brain = SpyBrain(
        [TextChunk("Потому что"), Status("⚙ list_windows", kind="tool"), Done(ok=True, text="Потому что…")]
    )
    journal = FakeJournal()
    events = collect(Core(Config(), FakeHands(Decision(tool="ask_gpt")), brain, journal), "почему так")
    done = check_order(events)
    levels = [e for e in events if isinstance(e, Level)]
    assert levels == [
        Level("hands", "hands", model="4b"),
        Level("brain", "hands→ask_gpt", model="gpt-6-luna"),
    ]
    assert brain.asks == [("почему так", False)]
    assert done.level == "brain" and done.reason == "hands→ask_gpt" and done.ok
    assert [e.text for e in events if isinstance(e, TextChunk)] == ["Потому что"]
    assert mods.calls == []
    assert journal.records[0]["reason"] == "hands→ask_gpt" and journal.records[0]["tool"] == "ask_gpt"


@pytest.mark.parametrize(
    "decision", [Decision(kind="error", reason="обрезано по max_tokens"), OSError("нет связи")]
)
def test_hands_error_goes_to_brain(mods: types.SimpleNamespace, decision: Any) -> None:
    brain = SpyBrain()
    done = check_order(collect(Core(Config(), FakeHands(decision), brain)))
    assert brain.asks and done.level == "brain" and done.reason == "hands→error"


def test_execute_ask_gpt_goes_to_brain(mods: types.SimpleNamespace) -> None:
    mods.outcome = Outcome(kind="ask_gpt", ok=True, text="")
    brain = SpyBrain()
    done = check_order(collect(Core(Config(), FakeHands(), brain)))
    assert brain.asks and done.level == "brain" and done.reason == "hands→ask_gpt"


def test_brain_branch_deep_and_timings(mods: types.SimpleNamespace) -> None:
    mods.route = Route("brain", "prefix:think", "чем опасно", deep=True)
    raw = {
        "t_first_token": 1.25,
        "t_total": 2.5,
        "new_thread": True,
        "model": "gpt-6.1-sol",
        "usage": {"in": 9},
    }
    brain = SpyBrain([Level("brain", "из мозга"), TextChunk("Тем"), Done(ok=True, text="Тем…", timings=raw)])
    journal = FakeJournal()
    events = collect(Core(Config(), FakeHands(), brain, journal), "думай: чем опасно")
    done = check_order(events)
    assert [e for e in events if isinstance(e, Level)] == [
        Level("brain", "prefix:think", model="gpt-6.1-sol")
    ]
    assert brain.asks == [("чем опасно", True)]
    assert done.reason == "prefix:think" and done.level == "brain"
    assert done.timings["t_first_token"] == 1.25  # тайминги мозга дополнены, а не заменены
    assert done.timings["brain"] == {
        "first_token": 1250.0,
        "total": 2500.0,
        "new_thread": True,
        "model": "gpt-6.1-sol",
        "usage": {"in": 9},
    }
    assert "route" in done.timings and "total" in done.timings
    assert journal.records[0]["brain"]["first_token"] == 1250.0


def test_brain_timings_units() -> None:
    assert brain_timings({"first_token_ms": 900, "total": 1500}) == {"first_token": 900.0, "total": 1500.0}
    assert brain_timings({"t_first_token": 2.0})["first_token"] == 2000.0
    assert brain_timings({"t_first_token": 1800})["first_token"] == 1800.0  # уже мс
    assert brain_timings({"t_first_token": None}) == {}


def test_brain_status_forwarded(mods: types.SimpleNamespace) -> None:
    """Строку «Запускаю GPT…» даёт сам мозг; core её пробрасывает и своей не добавляет."""
    mods.route = Route("brain", "heur:question", "почему")
    start = Status("Запускаю GPT…", key="brain-start")
    brain = SpyBrain([start, Status("Запускаю GPT…", done=True, key="brain-start"), Done(ok=True, text="да")])
    events = collect(Core(Config(), FakeHands(), brain))
    check_order(events)
    assert next(e for e in events if isinstance(e, Status)) == start
    assert sum(isinstance(e, Status) for e in events) == 2


def test_brain_stream_without_done(mods: types.SimpleNamespace) -> None:
    mods.route = Route("brain", "heur:question", "почему")
    done = check_order(collect(Core(Config(), FakeHands(), SpyBrain([TextChunk("обрыв")]))))
    assert not done.ok and done.level == "brain"


# --- локально: шпион мозга -------------------------------------------------------------------------


def test_local_mode_route_brain_no_brain_call(mods: types.SimpleNamespace) -> None:
    mods.route = Route("brain", "heur:question", "почему небо голубое")
    brain = SpyBrain()
    events = collect(Core(Config(mode="local"), FakeHands(), brain))
    done = check_order(events)
    assert events[0] == Level("local", "local:needs_gpt")
    assert done.text == NEEDS_GPT and not done.ok and done.level == "local"
    assert brain.asks == []


def test_local_level_from_router(mods: types.SimpleNamespace) -> None:
    mods.route = Route("local", "local:needs_gpt", "почему", local_only=True)
    brain = SpyBrain()
    done = check_order(collect(Core(Config(), FakeHands(), brain)))
    assert done.text == NEEDS_GPT and done.level == "local" and brain.asks == []


@pytest.mark.parametrize("mode", ["normal", "local"])
def test_local_only_hands_ask_gpt_no_brain_call(mods: types.SimpleNamespace, mode: str) -> None:
    mods.route = Route("hands", "hands", "сделай что-то", local_only=True)
    brain = SpyBrain()
    events = collect(Core(Config(mode=mode), FakeHands(Decision(tool="ask_gpt")), brain))
    done = check_order(events)
    assert [e.level for e in events if isinstance(e, Level)] == ["hands", "local"]
    assert done.reason == "hands→ask_gpt" and done.text == NEEDS_GPT and brain.asks == []


def test_local_hands_error_shows_reason(mods: types.SimpleNamespace) -> None:
    hands = FakeHands(Decision(kind="error", reason="руки не отвечают"))
    events = collect(Core(Config(mode="local"), hands, SpyBrain()))
    done = check_order(events)
    assert done.text == NEEDS_GPT and done.reason == "hands→error"
    assert any(isinstance(e, Status) and "руки не отвечают" in e.text and e.kind == "warn" for e in events)


def test_no_brain_object(mods: types.SimpleNamespace) -> None:
    mods.route = Route("brain", "heur:question", "почему")
    done = check_order(collect(Core(Config(), FakeHands(), None)))
    assert done.level == "local" and done.text == NEEDS_GPT


@pytest.mark.parametrize(
    ("mode", "text"),
    [
        ("local", "почему небо голубое"),
        ("normal", "локально: почему небо голубое"),
        ("normal", "локально: напиши письмо начальнику"),
        ("local", "gpt: расскажи про Vulkan"),
        ("local", "думай: чем опасно держать модель в VRAM"),
        ("normal", "локально: сделай что-нибудь хорошее"),
    ],
)
def test_spy_real_router_never_calls_brain(monkeypatch: pytest.MonkeyPatch, mode: str, text: str) -> None:
    """S8 п.2: в local-режиме и с «локально:» по всему пути (настоящий роутер) мозг не вызывается."""
    calls: list[Any] = []
    install(
        monkeypatch,
        "jarvis.execute",
        run=lambda *a, **k: calls.append(a) or Outcome(kind="clarify", ok=True, text="?", autohide=False),
    )
    monkeypatch.delitem(sys.modules, "jarvis.router", raising=False)  # настоящий роутер
    monkeypatch.delattr(importlib.import_module("jarvis"), "router", raising=False)
    from pc import apps

    monkeypatch.setattr(apps, "resolve", lambda name: (None, 0.0))
    brain = SpyBrain()
    for decision in (Decision(tool="ask_gpt"), Decision(kind="error", reason="x"), OSError("нет рук")):
        done = check_order(collect(Core(Config(mode=mode), FakeHands(decision), brain), text))
        assert done.level == "local" and done.text == NEEDS_GPT
    assert brain.asks == [] and brain.cancels == 0


# --- исключения и отмена ---------------------------------------------------------------------------


def test_router_exception_still_level_and_done(mods: types.SimpleNamespace) -> None:
    mods.route_error = ValueError("сломался роутер")
    journal = FakeJournal()
    events = collect(Core(Config(), FakeHands(), SpyBrain(), journal))
    done = check_order(events)
    assert not done.ok and "сломался роутер" in done.text
    assert journal.records[0]["ok"] is False and journal.records[0]["error"] == done.text


def test_execute_exception(mods: types.SimpleNamespace) -> None:
    mods.exec_error = RuntimeError("COM упал")
    events = collect(Core(Config(), FakeHands(), SpyBrain()))
    done = check_order(events)
    assert events[0].level == "hands" and done.level == "hands" and not done.ok and "COM упал" in done.text


def test_brain_exception_mid_stream(mods: types.SimpleNamespace) -> None:
    mods.route = Route("brain", "heur:question", "почему")
    events = collect(Core(Config(), FakeHands(), SpyBrain(fail_after=1)))
    done = check_order(events)
    assert isinstance(events[1], TextChunk) and not done.ok and done.level == "brain"


def test_not_implemented_is_understandable(mods: types.SimpleNamespace) -> None:
    mods.exec_error = NotImplementedError()
    done = check_order(collect(Core(Config(), FakeHands(), SpyBrain())))
    assert done.text == "Эта часть Jarvis ещё не готова"


def test_empty_text(mods: types.SimpleNamespace) -> None:
    mods.route = Route("hands", "empty", "")
    hands = FakeHands()
    done = check_order(collect(Core(Config(), hands, SpyBrain()), "  "))
    assert not done.ok and hands.calls == []


def test_cancel_during_hands_skips_action(mods: types.SimpleNamespace) -> None:
    brain = SpyBrain()
    core = Core(Config(), FakeHands(), brain)
    core.hands.on_decide = core.cancel
    done = check_order(list(core.handle("закрой его", Context())))
    assert done.cancelled and not done.ok and mods.calls == []
    assert brain.cancels == 1


def test_cancel_during_brain_stream(mods: types.SimpleNamespace) -> None:
    mods.route = Route("brain", "heur:question", "почему")
    brain = SpyBrain([TextChunk("а"), TextChunk("б"), Done(ok=True, text="аб")])
    core = Core(Config(), FakeHands(), brain)
    events = []
    for ev in core.handle("почему", Context()):
        events.append(ev)
        if isinstance(ev, TextChunk):
            core.cancel()
    done = check_order(events)
    assert done.cancelled and brain.cancels >= 1


def test_cancel_flag_resets_between_requests(mods: types.SimpleNamespace) -> None:
    core = Core(Config(), FakeHands(), SpyBrain())
    core.cancel()
    done = check_order(list(core.handle("громче", Context())))
    assert not done.cancelled and done.ok


def test_consumer_stops_early_closes_brain(mods: types.SimpleNamespace) -> None:
    mods.route = Route("brain", "heur:question", "почему")
    closed: list[bool] = []

    class Brain(SpyBrain):
        def ask(self, text: str, ctx: Any, deep: bool = False):
            try:
                yield TextChunk("а")
                yield Done(ok=True, text="а")
            finally:
                closed.append(True)

    gen = Core(Config(), FakeHands(), Brain()).handle("почему", Context())
    next(gen), next(gen)  # Level, TextChunk
    gen.close()
    assert closed == [True]


def test_journal_failure_does_not_break(mods: types.SimpleNamespace) -> None:
    class Broken:
        def write(self, record: dict[str, Any]) -> None:
            raise OSError("диск")

    done = check_order(collect(Core(Config(), FakeHands(), SpyBrain(), Broken())))
    assert done.ok


# --- зрение ------------------------------------------------------------------------------------------

CHROME = WindowInfo(77, "Новости — Google Chrome", 5, "chrome.exe")
SCREEN_LABEL = "⚙ снимок экрана → GPT"


class VisionBrain(SpyBrain):
    """Шпион мозга, который принимает снимки (как настоящий Brain.ask)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.images: list[list[str]] = []

    def ask(self, text: str, ctx: Any, deep: bool = False, images: Any = ()):  # type: ignore[override]
        self.images.append(list(images))
        return super().ask(text, ctx, deep)


@pytest.fixture
def cam(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Фейковые pc.screen.capture_* и vision.encode_jpeg: calls — вызовы снимка, result — Shot, Result
    или исключение."""
    state = types.SimpleNamespace(
        calls=[],
        result=Shot(4, 2, b"\x01\x02\x03\x00" * 8),
        jpeg=b"\xff\xd8JPEG\xff\xd9",
        encode_args=None,
        encode_error=None,
        on_capture=None,
    )

    def capture(kind: str):
        def fn(hwnd: Any, caller: str, deny_apps: Any = ()) -> Any:
            state.calls.append((kind, hwnd, caller, list(deny_apps)))
            if state.on_capture is not None:
                state.on_capture()
            if isinstance(state.result, Exception):
                raise state.result
            return state.result

        return fn

    def encode(shot: Shot, max_side: int = 1600, quality: int = 80) -> bytes:
        state.encode_args = (max_side, quality)
        if state.encode_error is not None:
            raise state.encode_error
        return state.jpeg

    monkeypatch.setattr(screen, "capture_window", capture("window"))
    monkeypatch.setattr(screen, "capture_screen", capture("screen"))
    monkeypatch.setattr(vision, "encode_jpeg", encode)
    return state


def vision_run(
    core: Core, text: str = "экран: что тут не так?", window: Any = CHROME, **kw: Any
) -> list[Any]:
    return list(core.handle(text, Context(active_window=window), **kw))


def test_vision_screen_shot_goes_to_brain_with_image(mods: types.SimpleNamespace, cam: Any) -> None:
    mods.route = Route("brain", "vision:screen", "что тут не так?", vision="screen")
    brain, journal = VisionBrain(), FakeJournal()
    cfg = Config(vision=VisionConfig(max_side=1200, jpeg_quality=70, deny_apps=["Telegram.exe"]))
    events = vision_run(Core(cfg, FakeHands(), brain, journal))
    done = check_order(events)
    assert events[0] == Level("brain", "vision:screen", model="gpt-6-luna")
    assert events[1:3] == [
        Status(SCREEN_LABEL, kind="tool", key="vision"),
        Status(SCREEN_LABEL, kind="tool", done=True, ok=True, key="vision"),
    ]
    assert cam.calls == [("screen", 77, "user", ["Telegram.exe"])]  # монитор окна на момент хоткея
    assert cam.encode_args == (1200, 70)
    assert brain.asks == [("что тут не так?", False)]
    assert brain.images == [[vision.data_url(cam.jpeg)]]
    assert done.ok and (done.level, done.reason) == ("brain", "vision:screen")
    assert isinstance(done.timings["shot"], float) and isinstance(done.timings["encode"], float)
    rec = journal.records[0]
    assert (rec["vision"], rec["jpeg_bytes"], rec["shot_px"]) == ("screen", len(cam.jpeg), [4, 2])
    assert isinstance(rec["shot"], float) and isinstance(rec["encode"], float) and "tools" not in rec
    dumped = json.dumps(rec, ensure_ascii=False)  # ни картинки, ни заголовка окна
    assert "data:image" not in dumped and base64.b64encode(cam.jpeg).decode() not in dumped
    assert "Новости" not in dumped


def test_vision_window_shot(mods: types.SimpleNamespace, cam: Any) -> None:
    mods.route = Route("brain", "vision:window", "что в этом окне?", deep=True, vision="window")
    brain = VisionBrain()
    events = vision_run(Core(Config(), FakeHands(), brain), "думай: что в этом окне?")
    done = check_order(events)
    label = "⚙ снимок окна «Новости — Google Chrome» → GPT"
    assert Status(label, kind="tool", key="vision") in events
    assert cam.calls == [("window", 77, "user", [])]
    assert brain.asks == [("что в этом окне?", True)] and len(brain.images[0]) == 1 and done.ok


def test_vision_screen_without_window_uses_primary(mods: types.SimpleNamespace, cam: Any) -> None:
    mods.route = Route("brain", "vision:screen", "что на экране", vision="screen")
    check_order(vision_run(Core(Config(), FakeHands(), VisionBrain()), window=None))
    assert cam.calls == [("screen", None, "user", [])]


def test_vision_window_unknown(mods: types.SimpleNamespace, cam: Any) -> None:
    """`jarvis ask` из консоли или окно из трея: окна «до Jarvis» нет — не угадываем."""
    mods.route = Route("brain", "vision:window", "что в этом окне", vision="window")
    brain, journal = VisionBrain(), FakeJournal()
    done = check_order(vision_run(Core(Config(), FakeHands(), brain, journal), window=None))
    assert not done.ok and done.text == vision.NO_WINDOW_TEXT
    assert cam.calls == [] and brain.asks == [] and journal.records[0]["vision"] == "window"


def test_vision_refusal_does_not_ask_brain(mods: types.SimpleNamespace, cam: Any) -> None:
    mods.route = Route("brain", "vision:window", "что тут", vision="window")
    cam.result = Result(False, screen.CLOSED_TEXT)
    brain = VisionBrain()
    events = vision_run(Core(Config(), FakeHands(), brain))
    done = check_order(events)
    assert not done.ok and done.text == screen.CLOSED_TEXT and done.level == "brain"
    assert [e for e in events if isinstance(e, Status)][-1].ok is False
    assert brain.asks == [] and cam.encode_args is None


def test_vision_capture_and_encode_errors(mods: types.SimpleNamespace, cam: Any) -> None:
    mods.route = Route("brain", "vision:screen", "что тут", vision="screen")
    brain = VisionBrain()
    cam.result = OSError("GetDC упал")
    done = check_order(vision_run(Core(Config(), FakeHands(), brain)))
    assert not done.ok and done.text == "Снимок не получился: GetDC упал"
    cam.result = Shot(1, 1, b"\x01\x01\x01\x00")
    cam.encode_error = RuntimeError("Qt не сохранил JPEG")
    done = check_order(vision_run(Core(Config(), FakeHands(), brain)))
    assert not done.ok and done.text == "Снимок не сжался в JPEG: Qt не сохранил JPEG"
    assert brain.asks == []


def test_vision_disabled(mods: types.SimpleNamespace, cam: Any) -> None:
    mods.route = Route("brain", "vision:screen", "что тут", vision="screen")
    brain = VisionBrain()
    cfg = Config(vision=VisionConfig(enabled=False))
    done = check_order(vision_run(Core(cfg, FakeHands(), brain)))
    assert not done.ok and done.text == vision.DISABLED_TEXT
    assert cam.calls == [] and brain.asks == []


@pytest.mark.parametrize(
    ("mode", "route"),
    [
        ("local", Route("brain", "vision:screen", "что тут", vision="screen")),
        ("normal", Route("local", "local:needs_gpt", "что тут", local_only=True, vision="screen")),
    ],
)
def test_vision_local_mode_no_shot(mods: types.SimpleNamespace, cam: Any, mode: str, route: Route) -> None:
    mods.route = route
    brain = VisionBrain()
    done = check_order(vision_run(Core(Config(mode=mode), FakeHands(), brain)))
    assert done.text == NEEDS_GPT and done.level == "local"
    assert cam.calls == [] and brain.asks == []


def test_vision_cancel_before_send(mods: types.SimpleNamespace, cam: Any) -> None:
    """Esc, пока снимается экран: снимок не уходит, мозг не спрашивается."""
    mods.route = Route("brain", "vision:screen", "что тут", vision="screen")
    brain = VisionBrain()
    core = Core(Config(), FakeHands(), brain)
    cam.on_capture = core.cancel
    events = vision_run(core)
    done = check_order(events)
    assert done.cancelled and not done.ok and brain.asks == []
    assert Status(SCREEN_LABEL, kind="tool", done=True, ok=False, key="vision") in events


def test_vision_dry_takes_no_shot(mods: types.SimpleNamespace, cam: Any) -> None:
    mods.route = Route("brain", "vision:screen", "что тут", vision="screen")
    brain = VisionBrain()
    done = check_order(vision_run(Core(Config(), FakeHands(), brain), dry=True))
    assert done.ok and done.text == "(dry) снимок экрана → GPT: что тут"
    assert cam.calls == [] and brain.asks == []


def _real_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "jarvis.router", raising=False)
    monkeypatch.delattr(importlib.import_module("jarvis"), "router", raising=False)


@pytest.mark.parametrize(
    ("mode", "text"),
    [
        ("local", "посмотри на экран"),
        ("normal", "локально: что в этом окне?"),
        ("local", "думай: экран: что тут"),
    ],
)
def test_vision_real_router_local_never_shoots(
    monkeypatch: pytest.MonkeyPatch, cam: Any, mode: str, text: str
) -> None:
    _real_router(monkeypatch)
    brain = VisionBrain()
    done = check_order(vision_run(Core(Config(mode=mode), FakeHands(), brain), text))
    assert done.level == "local" and done.text == NEEDS_GPT
    assert cam.calls == [] and brain.asks == []


def test_vision_real_router_end_to_end(monkeypatch: pytest.MonkeyPatch, cam: Any) -> None:
    _real_router(monkeypatch)
    brain = VisionBrain()
    done = check_order(vision_run(Core(Config(), FakeHands(), brain), "посмотри на экран и скажи что не так"))
    assert done.ok and done.reason == "vision:screen"
    assert cam.calls == [("screen", 77, "user", [])]
    assert brain.asks == [("скажи что не так", False)] and brain.images[0][0].startswith(
        "data:image/jpeg;base64,"
    )
