"""Набор фраз Router: точность прямых команд на трудных отрицательных примерах (ADR 0026).

Файл набора — YAML с `kind: routing_dataset`: инвентарь, рабочая папка, пороги и случаи «фраза →
ожидаемое решение». Router решает каждую фразу без модели и без исполнения. Главная метрика — ложные
DIRECT: фраза, которой нужен агент, исполнилась бы прямой командой. Их, как и DIRECT не того действия
(чужое приложение, папка, адрес), должно быть ровно ноль — порог не настраивается. Полнота прямых
команд и задержка Router — с порогами из набора: пропущенная прямая команда стоит лишь вызова модели.
"""

import statistics
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, PositiveFloat, model_validator

from jarvis.adapters.inventory import StaticInventory
from jarvis.core.routing.router import Router
from jarvis.domain.intents import EntityKind, IntentId
from jarvis.domain.routing import Route, RouteDecision
from jarvis.evals.scenario import ScenarioError, ScenarioInventory, is_routing_dataset

EVAL_HOME = "/home/jarvis-eval"  # домашняя папка «компьютера» набора: известные папки — внутри неё

# Сущность, по которой видно, что сделает прямая команда: какое приложение, адрес, папка, шаблон, процесс.
MAIN_ENTITY: dict[IntentId, EntityKind | None] = {
    IntentId.APP_LAUNCH: EntityKind.APP,
    IntentId.URL_OPEN: EntityKind.URL,
    IntentId.FOLDER_OPEN: EntityKind.FOLDER,
    IntentId.FS_CURRENT: None,
    IntentId.FS_LIST: EntityKind.FOLDER,
    IntentId.FS_SEARCH: EntityKind.PATTERN,
    IntentId.PROCESS_LIST: EntityKind.PROCESS,
}

Verdict = Literal["ok", "false_direct", "wrong_direct", "missed_direct", "wrong_strategy"]


class RoutingCase(BaseModel, frozen=True, extra="forbid"):
    text: str = Field(min_length=1)
    expect: Literal[Route.DIRECT, Route.AGENT, Route.CLARIFY]
    intent: IntentId | None = None  # для DIRECT — обязательно
    entity: str | None = None  # значение главной сущности DIRECT; известная папка — `folder:downloads`
    tags: list[str] = Field(min_length=1)  # язык и вид случая: ru, en, mixed, typo, hard_negative, …
    note: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.expect is Route.DIRECT and self.intent is None:
            raise ValueError(f"«{self.text}»: для direct нужно intent")
        if self.expect is not Route.DIRECT and (self.intent is not None or self.entity is not None):
            raise ValueError(f"«{self.text}»: intent и entity бывают только у direct")
        return self


class RoutingThresholds(BaseModel, frozen=True, extra="forbid"):
    min_direct_recall: float = Field(default=0.9, ge=0, le=1)
    max_p95_ms: PositiveFloat = 50.0  # задержка решения Router, без загрузки инвентаря


class RoutingDataset(BaseModel, frozen=True, extra="forbid"):
    kind: Literal["routing_dataset"]
    id: str = Field(pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
    description: str = ""
    working_directory: str = "/work"
    inventory: ScenarioInventory = ScenarioInventory()
    thresholds: RoutingThresholds = RoutingThresholds()
    cases: list[RoutingCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        seen: set[str] = set()
        for case in self.cases:
            if case.text in seen:
                raise ValueError(f"фраза повторяется: «{case.text}»")
            seen.add(case.text)
        return self


class CaseResult(BaseModel, frozen=True):
    text: str
    tags: list[str]
    expected: Route
    expected_intent: IntentId | None
    expected_entity: str | None
    actual: Route
    actual_intent: IntentId | None
    actual_entity: str | None
    rules: list[str]
    verdict: Verdict
    duration_ms: float


class TagStats(BaseModel, frozen=True):
    cases: int
    correct: int


class RoutingReport(BaseModel, frozen=True):
    id: str
    cases: int
    negatives: int  # случаи, где DIRECT быть не должно (agent или clarify)
    direct_expected: int
    direct_hit: int
    false_direct: int
    wrong_direct: int
    missed_direct: int
    wrong_strategy: int
    direct_recall: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    thresholds: RoutingThresholds
    tags: dict[str, TagStats]
    failures: list[CaseResult]  # все случаи с вердиктом не ok
    problems: list[str]

    @property
    def passed(self) -> bool:
        return not self.problems


def load_routing_datasets(paths: Sequence[Path]) -> list[RoutingDataset]:
    """Наборы фраз среди файлов и папок eval: YAML с `kind: routing_dataset`."""
    datasets: list[RoutingDataset] = []
    for file in _yaml_files(paths):
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
        if not is_routing_dataset(data):
            continue
        try:
            datasets.append(RoutingDataset.model_validate(data))
        except ValueError as exc:
            raise ScenarioError(f"{file}: {exc}") from None
    return datasets


def run_routing(dataset: RoutingDataset) -> RoutingReport:
    router = Router(dataset_inventory(dataset.inventory))
    router.warm_up()
    router.decide(dataset.cases[0].text, dataset.working_directory)  # прогрев: первое решение не меряется
    results = [_run_case(router, case, dataset.working_directory) for case in dataset.cases]
    return _report(dataset, results)


def dataset_inventory(spec: ScenarioInventory) -> StaticInventory:
    folders = {folder: f"{EVAL_HOME}/{relative}" for folder, relative in spec.folders.items()}
    return StaticInventory(spec.apps, default_browser=spec.default_browser, folders=folders)


def _run_case(router: Router, case: RoutingCase, working_directory: str) -> CaseResult:
    started = time.perf_counter()
    decision = router.decide(case.text, working_directory)
    duration_ms = (time.perf_counter() - started) * 1000
    entity = _main_entity(decision)
    return CaseResult(
        text=case.text,
        tags=case.tags,
        expected=case.expect,
        expected_intent=case.intent,
        expected_entity=case.entity,
        actual=decision.strategy,
        actual_intent=decision.intent,
        actual_entity=entity,
        rules=decision.rules,
        verdict=_verdict(case, decision, entity),
        duration_ms=duration_ms,
    )


def _main_entity(decision: RouteDecision) -> str | None:
    if decision.intent is None:
        return None
    kind = MAIN_ENTITY[decision.intent]
    found = decision.entity(kind) if kind is not None else None
    if found is None:
        return None
    if found.source == "folder.known":  # известная папка — по правилу Router: folder.known.<id>
        known = next(rule for rule in decision.rules if rule.startswith("folder.known."))
        return f"folder:{known.removeprefix('folder.known.')}"
    return found.value


def _verdict(case: RoutingCase, decision: RouteDecision, entity: str | None) -> Verdict:
    if case.expect is not Route.DIRECT:
        if decision.strategy is Route.DIRECT:
            return "false_direct"
        return "ok" if decision.strategy is case.expect else "wrong_strategy"
    if decision.strategy is not Route.DIRECT:
        return "missed_direct"
    if decision.intent is not case.intent or entity != case.entity:
        return "wrong_direct"
    return "ok"


def _report(dataset: RoutingDataset, results: list[CaseResult]) -> RoutingReport:
    count = {verdict: sum(result.verdict == verdict for result in results) for verdict in Verdict.__args__}
    direct_expected = sum(result.expected is Route.DIRECT for result in results)
    direct_hit = sum(result.expected is Route.DIRECT and result.verdict == "ok" for result in results)
    recall = direct_hit / direct_expected if direct_expected else 1.0
    durations = sorted(result.duration_ms for result in results)
    p95 = durations[min(len(durations) - 1, round(0.95 * (len(durations) - 1)))]
    tags: dict[str, TagStats] = {}
    for tag in sorted({tag for result in results for tag in result.tags}):
        tagged = [result for result in results if tag in result.tags]
        tags[tag] = TagStats(cases=len(tagged), correct=sum(result.verdict == "ok" for result in tagged))
    limits = dataset.thresholds
    problems: list[str] = []
    if count["false_direct"]:
        problems.append(f"ложных DIRECT: {count['false_direct']} (допустимо 0)")
    if count["wrong_direct"]:
        problems.append(f"DIRECT не того действия: {count['wrong_direct']} (допустимо 0)")
    if count["wrong_strategy"]:
        problems.append(f"agent и clarify перепутаны: {count['wrong_strategy']}")
    if recall < limits.min_direct_recall:
        problems.append(f"полнота DIRECT {recall:.1%} ниже порога {limits.min_direct_recall:.0%}")
    if p95 > limits.max_p95_ms:
        problems.append(f"p95 решения Router {p95:.2f} мс выше порога {limits.max_p95_ms:g} мс")
    return RoutingReport(
        id=dataset.id,
        cases=len(results),
        negatives=sum(result.expected is not Route.DIRECT for result in results),
        direct_expected=direct_expected,
        direct_hit=direct_hit,
        false_direct=count["false_direct"],
        wrong_direct=count["wrong_direct"],
        missed_direct=count["missed_direct"],
        wrong_strategy=count["wrong_strategy"],
        direct_recall=recall,
        p50_ms=statistics.median(durations),
        p95_ms=p95,
        max_ms=durations[-1],
        thresholds=limits,
        tags=tags,
        failures=[result for result in results if result.verdict != "ok"],
        problems=problems,
    )


def _yaml_files(paths: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.yaml")))
        elif path.is_file():
            files.append(path)
        else:
            raise ScenarioError(f"нет такого файла или папки: {path}")
    return files
