"""«Чтение вслух»: предложения, статья со страницы, откуда берётся текст, прогресс, пауза и скорость."""

import asyncio

import httpx
import numpy as np
import pytest

from jarvis.context import AppContext
from jarvis.platform.fake import FakePlatform
from jarvis.plugins.audio import time_stretch
from jarvis.plugins.reader import Reader, clean_text, extract_article, split_sentences
from jarvis.services import UserFacingError
from tests.conftest import EventLog
from tests.plugins.conftest import FakeTts, enable, manager, wait_until

ARTICLE = """<!doctype html><html><head><meta charset="utf-8">
<title>Медузы — Новости науки</title><meta property="og:title" content="Медузы умеют учиться">
<script>alert("реклама")</script><style>p {color: red}</style></head>
<body><header><nav><a href="/">Главная</a> <a href="/news">Меню новостей</a></nav></header>
<div class="share-buttons">Поделиться в соцсетях</div>
<article><h1>Медузы умеют учиться</h1>
<p>Учёные выяснили, что медузы запоминают препятствия. Это открытие удивило биологов.</p>
<ul><li><p>Первый опыт длился неделю.</p></li><li>Второй — месяц.</li></ul>
<p>Подробности опубликованы в журнале.</p></article>
<aside>Реклама: купите аквариум</aside><footer>© 2026 Наука</footer></body></html>"""


def plugin_of(ctx: AppContext) -> Reader:
    plugin = manager(ctx).get("reader")
    assert isinstance(plugin, Reader)
    return plugin


def tts_of(ctx: AppContext) -> FakeTts:
    tts = ctx.tts
    assert isinstance(tts, FakeTts)
    return tts


def test_split_sentences_handles_abbreviations() -> None:
    text = (
        "Привет, мир! Это тест. Т. е. сокращения не режут: т.е. всё хорошо. "
        "А. С. Пушкин родился в 1799 г. в Москве.\nНовая строка без точки\n\n1. Первый пункт"
    )
    assert split_sentences(text) == [
        "Привет, мир!",
        "Это тест.",
        "Т. е. сокращения не режут: т.е. всё хорошо.",
        "А. С. Пушкин родился в 1799 г. в Москве.",
        "Новая строка без точки",
        "1. Первый пункт",
    ]


def test_long_sentences_are_split_for_speech() -> None:
    sentence = ", ".join(f"часть номер {i} довольно длинного перечисления" for i in range(20)) + "."
    parts = split_sentences(sentence)
    assert len(parts) > 1 and all(len(p) <= 320 for p in parts)
    assert " ".join(parts) == sentence


def test_clean_text_removes_markup_and_links() -> None:
    assert clean_text("# Заголовок\n**Жирный** текст, см. https://example.com/x?y=1") == (
        "Заголовок\nЖирный текст, см. ссылка"
    )


def test_extract_article() -> None:
    title, text = extract_article(ARTICLE.encode("utf-8"))
    assert title == "Медузы умеют учиться"
    lines = text.splitlines()
    assert lines[0] == "Учёные выяснили, что медузы запоминают препятствия. Это открытие удивило биологов."
    assert "Первый опыт длился неделю." in lines and "Второй — месяц." in lines
    assert lines.count("Первый опыт длился неделю.") == 1
    for junk in ("alert", "Меню", "Поделиться", "Реклама", "© 2026", "color"):
        assert junk not in text


async def test_reads_with_progress(pctx: AppContext, pevents: EventLog) -> None:
    await enable(pctx, "reader")
    result = await manager(pctx).action(
        "reader", "read", {"text": "Первое предложение. Второе предложение! Третье?"}
    )
    assert result == {"ok": True, "total": 3, "title": ""}
    plugin = plugin_of(pctx)
    await wait_until(lambda: plugin.status()["state"] == "done")
    events = pevents.of("plugin.reader.progress")
    assert events[0]["sentences"] == ["Первое предложение.", "Второе предложение!", "Третье?"]
    assert all("sentences" not in e for e in events[1:])
    playing = [e["index"] for e in events if e["state"] == "playing"]
    assert playing[-3:] == [0, 1, 2]
    assert events[-1]["state"] == "done" and events[-1]["sentence"] == "Третье?"
    tts = tts_of(pctx)
    assert tts.synthesized == ["Первое предложение.", "Второе предложение!", "Третье?"]
    assert len(tts.played) == 3


async def test_speed_changes_tempo(pctx: AppContext) -> None:
    tts = tts_of(pctx)
    tts.seconds = 0.5
    await enable(pctx, "reader", config={"speed": 1.5})
    await manager(pctx).action("reader", "read", {"text": "Быстрое чтение."})
    await wait_until(lambda: plugin_of(pctx).status()["state"] == "done")
    synthesized = int(tts.rate * tts.seconds)
    assert abs(tts.played[0] - synthesized / 1.5) < synthesized * 0.05
    assert await manager(pctx).action("reader", "speed", {"value": 1.25}) == {"ok": True, "speed": 1.25}
    assert manager(pctx).get("reader").config["speed"] == 1.25


def test_time_stretch_keeps_pitch_and_changes_length() -> None:
    rate = 16000
    t = np.arange(rate) / rate
    tone = (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    for speed in (0.75, 1.5):
        out = time_stretch(tone, rate, speed)
        assert abs(len(out) - rate / speed) <= rate * 0.01
        peak_hz = np.abs(np.fft.rfft(out)).argmax() * rate / len(out)
        assert abs(peak_hz - 200) < 10  # высота голоса не меняется
    assert time_stretch(tone, rate, 1.0) is tone or np.array_equal(time_stretch(tone, rate, 1.0), tone)


async def test_pause_resume_and_stop(pctx: AppContext, pevents: EventLog) -> None:
    tts = tts_of(pctx)
    tts.play_delay = 0.3
    await enable(pctx, "reader")
    plugins = manager(pctx)
    plugin = plugin_of(pctx)
    await plugins.action("reader", "read", {"text": "Раз. Два. Три. Четыре."})
    await wait_until(lambda: len(tts.played) == 1)
    assert await plugins.action("reader", "pause", {}) == {"ok": True, "state": "paused"}
    assert tts.silenced >= 1
    played = len(tts.played)
    await asyncio.sleep(0.4)  # на паузе ничего не звучит
    assert plugin.status()["state"] == "paused" and len(tts.played) == played
    assert pevents.of("plugin.reader.progress")[-1]["state"] == "paused"
    await plugins.action("reader", "resume", {})
    await wait_until(lambda: len(tts.played) > played)
    assert plugin.status()["index"] == 0  # прерванная фраза читается заново
    assert tts.synthesized.count("Раз.") == 1  # готовый звук не синтезируется повторно
    assert await plugins.action("reader", "stop", {}) == {"ok": True, "state": "stopped"}
    assert not plugin.active and pevents.of("plugin.reader.progress")[-1]["state"] == "stopped"
    await plugins.action("reader", "resume", {})  # после «стоп» — с того же места
    await wait_until(lambda: plugin.active)
    await plugins.action("reader", "seek", {"index": 3})
    await wait_until(lambda: plugin.status()["state"] == "done")
    assert tts.synthesized[-1] == "Четыре."


async def test_global_stop_ends_reading(pctx: AppContext) -> None:
    tts = tts_of(pctx)
    tts.play_delay = 0.3
    await enable(pctx, "reader")
    await manager(pctx).action("reader", "read", {"text": "Раз. Два. Три."})
    plugin = plugin_of(pctx)
    await wait_until(lambda: len(tts.played) == 1)
    tts.silence()  # диспетчер на «стоп» глушит речь и сообщает dispatch.done {kind: "stop"}
    pctx.bus.publish("dispatch.done", {"text": "стоп", "kind": "stop"})
    await wait_until(lambda: not plugin.active)
    assert plugin.status()["state"] == "stopped" and len(tts.played) == 1


async def test_selection_then_clipboard(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    platform = pctx.platform
    assert isinstance(platform, FakePlatform)
    platform.clipboard = "старый буфер"
    original_press = platform.press_keys

    def copy_selection(combo: str) -> None:
        original_press(combo)
        if combo == "ctrl+c":
            platform.clipboard = "Выделенный текст."

    monkeypatch.setattr(platform, "press_keys", copy_selection)
    await enable(pctx, "reader")
    plugin = plugin_of(pctx)
    assert await plugin.obtain({}) == ("", "Выделенный текст.")
    assert platform.clipboard == "старый буфер"  # буфер вернулся на место

    monkeypatch.setattr(platform, "press_keys", original_press)  # ничего не выделено — читаем буфер
    assert await plugin.obtain({}) == ("", "старый буфер")
    platform.clipboard = ""
    with pytest.raises(UserFacingError, match="Нечего читать"):
        await plugin.obtain({})


async def test_reads_article_by_link(pctx: AppContext, pevents: EventLog) -> None:
    def site(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "science.example"
        return httpx.Response(200, content=ARTICLE.encode("utf-8"), headers={"content-type": "text/html"})

    manager(pctx).http_transport = httpx.MockTransport(site)
    platform = pctx.platform
    assert isinstance(platform, FakePlatform)
    platform.clipboard = "https://science.example/jellyfish"
    await enable(pctx, "reader")
    result = await manager(pctx).action("reader", "read", {"selection": False})
    assert result["title"] == "Медузы умеют учиться"
    sentences = pevents.of("plugin.reader.progress")[0]["sentences"]
    assert sentences[:3] == [
        "Медузы умеют учиться",
        "Учёные выяснили, что медузы запоминают препятствия.",
        "Это открытие удивило биологов.",
    ]
    with pytest.raises(UserFacingError, match="не статья"):
        manager(pctx).http_transport = httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        )
        await plugin_of(pctx).fetch_article("https://science.example/paper.pdf")


def test_extract_article_in_windows_1251() -> None:
    html = (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1251">'
        "<title>Старая страница</title></head><body><div class='text'>"
        "<p>Кодировка страниц на русском бывает разной, и текст должен читаться правильно.</p>"
        "<p>Второй абзац тоже на месте, без кракозябр и знаков вопроса.</p></div></body></html>"
    )
    title, text = extract_article(html.encode("cp1251"))
    assert title == "Старая страница"
    assert text.splitlines()[0].startswith("Кодировка страниц на русском")
