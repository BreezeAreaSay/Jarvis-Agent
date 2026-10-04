"""Router (ADR 0026): детерминированное решение без модели; точность прямых команд важнее полноты."""

from pathlib import Path

import pytest

from jarvis.adapters.inventory import StaticInventory
from jarvis.core.routing.router import Router, decision_payload, prepare
from jarvis.domain.intents import EntityKind, IntentId
from jarvis.domain.inventory import AppEntry, KnownFolder
from jarvis.domain.routing import CloudMode, Route, RouteDecision, RoutingLevel
from jarvis.evals.routing import RoutingCase, dataset_inventory, load_routing_datasets

DATASET_DIR = Path(__file__).resolve().parents[3] / "evals" / "routing"
(DATASET,) = load_routing_datasets([DATASET_DIR])
NEGATIVES = [case for case in DATASET.cases if case.expect is not Route.DIRECT]
POSITIVES = [case for case in DATASET.cases if case.expect is Route.DIRECT and "missed" not in case.tags]


def app(app_id: str, name: str, *aliases: str) -> AppEntry:
    return AppEntry(
        id=app_id, name=name, aliases=list(aliases), target=f"C:/Apps/{app_id}.lnk", kind="shortcut"
    )


TELEGRAM = app("telegram", "Telegram Desktop", "телеграм")
FIREFOX = app("firefox", "Mozilla Firefox", "firefox")


def router(*apps: AppEntry, browser: str | None = None, **folders: str) -> Router:
    known = {KnownFolder(name): path for name, path in folders.items()}
    return Router(StaticInventory(apps, default_browser=browser, folders=known))


def decide(text: str, *apps: AppEntry, browser: str | None = None, **folders: str) -> RouteDecision:
    return router(*apps, browser=browser, **folders).decide(text, "/work")


@pytest.fixture(scope="module")
def dataset_router() -> Router:
    return Router(dataset_inventory(DATASET.inventory))


def test_the_dataset_is_mostly_hard_negatives() -> None:
    assert len(DATASET.cases) >= 150
    assert len(NEGATIVES) > len(DATASET.cases) / 2
    tags = {tag for case in DATASET.cases for tag in case.tags}
    assert {"ru", "en", "mixed", "typo", "colloquial", "synonym", "ambiguous", "adversarial"} <= tags


@pytest.mark.parametrize("case", NEGATIVES, ids=lambda case: case.text)
def test_no_false_direct(dataset_router: Router, case: RoutingCase) -> None:
    decision = dataset_router.decide(case.text, DATASET.working_directory)
    assert decision.strategy is case.expect, decision.rules


@pytest.mark.parametrize("case", POSITIVES, ids=lambda case: case.text)
def test_direct_commands_resolve_the_right_action(dataset_router: Router, case: RoutingCase) -> None:
    decision = dataset_router.decide(case.text, DATASET.working_directory)
    assert decision.strategy is Route.DIRECT, decision.rules
    assert decision.intent is case.intent


def test_an_app_from_the_inventory_is_a_direct_launch() -> None:
    decision = decide("открой телеграм", TELEGRAM)
    assert decision.strategy is Route.DIRECT
    assert decision.intent is IntentId.APP_LAUNCH
    entity = decision.entity(EntityKind.APP)
    assert entity is not None
    assert (entity.value, entity.label, entity.source) == (
        "telegram",
        "Telegram Desktop",
        "inventory.app.alias",
    )
    assert decision.rules == ["direct.launch.verb", "inventory.app.alias"]
    assert decision.level is None


def test_an_unknown_app_goes_to_the_local_agent() -> None:
    decision = decide("открой телегарм", TELEGRAM)
    assert decision.strategy is Route.AGENT
    assert decision.level is RoutingLevel.LOCAL
    assert decision.rules == [
        "agent.default",
        "direct.launch.verb",
        "direct.reject.unknown_app",
        "level.default",
    ]


def test_the_browser_is_the_default_browser_from_the_inventory() -> None:
    decision = decide("запусти браузер", TELEGRAM, FIREFOX, browser="firefox")
    entity = decision.entity(EntityKind.APP)
    assert entity is not None
    assert entity.value == "firefox"
    assert "inventory.default_browser" in decision.rules
    assert decide("запусти браузер", TELEGRAM).strategy is Route.AGENT  # браузера по умолчанию нет


def test_equal_candidates_are_a_question_not_a_guess() -> None:
    decision = decide(
        "запусти питон", app("py312", "Python 3.12", "питон"), app("py313", "Python 3.13", "питон")
    )
    assert decision.strategy is Route.CLARIFY
    assert decision.question == "Какое приложение открыть: «Python 3.12», «Python 3.13»?"
    assert [entity.value for entity in decision.entities] == ["py312", "py313"]


def test_a_site_named_like_an_installed_app_is_a_question() -> None:
    battle = app("battle-net", "Battle.net")
    decision = decide("открой Battle.net", battle)
    assert decision.strategy is Route.CLARIFY
    assert decision.question == "Что открыть: приложение «Battle.net» или сайт https://battle.net?"
    assert decide("открой github.com", battle).intent is IntentId.URL_OPEN


def test_a_question_never_launches_anything() -> None:
    for text in (
        "открыть телеграм?",
        "открой загрузки?",
        "открыть github.com?",
        "открыть телеграм?!",
    ):
        decision = decide(text, TELEGRAM, downloads="/home/u/Downloads")
        assert decision.strategy is Route.AGENT
        assert "direct.reject.question" in decision.rules


def test_a_question_may_read() -> None:
    decision = decide("что в загрузках?", downloads="/home/u/Downloads")
    assert decision.intent is IntentId.FS_LIST
    entity = decision.entity(EntityKind.FOLDER)
    assert entity is not None
    assert entity.value == "/home/u/Downloads"


def test_a_known_folder_missing_on_this_computer_is_not_direct() -> None:
    decision = decide("открой загрузки")
    assert decision.strategy is Route.AGENT
    assert "direct.reject.folder_not_found" in decision.rules


def test_an_ambiguous_folder_needs_the_word_folder() -> None:
    assert decide("открой музыку", music="/home/u/Music").strategy is Route.AGENT
    assert decide("открой папку музыка", music="/home/u/Music").intent is IntentId.FOLDER_OPEN


@pytest.mark.parametrize(
    "text",
    [
        "открой \\\\evil.com\\share",
        "открой папку //evil.com/share",
        "открой file:///etc/passwd",
        "открой http://user:pass@evil.com",
        "запусти C:\\Windows\\System32\\cmd.exe",
        "открой телеграм; rm -rf /",
        "open `telegram`",
    ],
)
def test_untrusted_targets_are_not_direct(text: str) -> None:
    assert decide(text, TELEGRAM).strategy is Route.AGENT


def test_entities_come_only_from_the_command_and_trusted_sources() -> None:
    sources = set()
    dataset_router = Router(dataset_inventory(DATASET.inventory))
    for case in DATASET.cases:
        decision = dataset_router.decide(case.text, DATASET.working_directory)
        sources |= {entity.source for entity in decision.entities}
    assert sources <= {
        "inventory.app.name",
        "inventory.app.alias",
        "inventory.default_browser",
        "folder.known",
        "working_directory",
        "command",
    }


def test_direct_commands_can_be_switched_off() -> None:
    decision = Router(StaticInventory([TELEGRAM]), direct=False).decide("открой телеграм")
    assert decision.strategy is Route.AGENT
    assert decision.rules == ["agent.default", "direct.disabled", "level.default"]


@pytest.mark.parametrize(
    ("raw", "text", "question"),
    [
        ("Джарвис, открой-ка телеграм, пожалуйста!", "открой телеграм", False),
        ("  запусти   стим  ", "запусти стим", False),
        ("открыть браузер?", "открыть браузер", True),
        ("ну плиз открой загрузки...", "открой загрузки", False),
        ("открыть браузер?!", "открыть браузер", True),
        ("открыть телеграм? пожалуйста", "открыть телеграм", True),
        ("open https://ya.ru/search?q=1", "open https://ya.ru/search?q=1", False),
        ("ls ..", "ls ..", False),
        ('открой папку "Мои  проекты"', 'открой папку "Мои  проекты"', False),
        ("открой папку ~/дача-ка", "открой папку ~/дача-ка", False),
    ],
)
def test_politeness_and_punctuation_do_not_change_the_command(raw: str, text: str, question: bool) -> None:
    command = prepare(raw)
    assert (command.text, command.question) == (text, question)


def test_entities_keep_the_characters_of_the_command() -> None:
    command = prepare("Открой Ёлку!")
    assert command.text == "Открой Ёлку"  # из text берутся сущности: «ё» и регистр на месте
    assert command.key == "Открой Елку"  # key — для словаря, той же длины
    assert prepare("открой https://example.com/a!").tail_on_path
    assert not prepare("открой github.com!").tail_on_path


def test_the_decision_payload_is_bounded_and_carries_the_rules() -> None:
    decision = decide("открой телеграм", TELEGRAM)
    payload = decision_payload(decision, 0.25)
    assert payload["strategy"] == "direct"
    assert payload["intent"] == "app.launch"
    assert payload["rules"] == ["direct.launch.verb", "inventory.app.alias"]
    assert payload["duration_ms"] == 0.25
    assert payload["entities"] == [
        {"kind": "app", "value": "telegram", "label": "Telegram Desktop", "source": "inventory.app.alias"}
    ]


def test_routing_is_fast() -> None:
    """Порог V2.1 — меньше 50 мс на решение; здесь с большим запасом, чтобы не зависеть от машины CI."""
    import time

    dataset_router = Router(dataset_inventory(DATASET.inventory))
    dataset_router.warm_up()
    started = time.perf_counter()
    for case in DATASET.cases:
        dataset_router.decide(case.text, DATASET.working_directory)
    assert (time.perf_counter() - started) / len(DATASET.cases) < 0.05


def test_a_task_mode_only_tightens_the_configured_mode() -> None:
    strict = Router(StaticInventory([TELEGRAM]), mode=CloudMode.LOCAL_ONLY)
    decision = strict.decide("Проанализируй архитектуру", "/work", mode=CloudMode.SMART)
    assert (decision.mode, decision.level) == (CloudMode.LOCAL_ONLY, RoutingLevel.LOCAL)
    assert "level.mode.local_only" in decision.rules
    relaxed = Router(StaticInventory([TELEGRAM])).decide("Проанализируй", "/work", mode=CloudMode.LOCAL_ONLY)
    assert relaxed.mode is CloudMode.LOCAL_ONLY
