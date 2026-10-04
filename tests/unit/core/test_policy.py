"""Policy Engine v1: решение — чистая функция вызова, preview и зон; самое строгое правило побеждает."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jarvis.core.policy import PolicyEngine, PolicyZones
from jarvis.domain.ids import TaskId
from jarvis.domain.tools import (
    EffectKind,
    ExecutionTarget,
    Invoker,
    PolicyOutcome,
    TargetKind,
    ToolCall,
    ToolCallId,
    ToolEffect,
    ToolId,
    ToolPreview,
)

HOST = ExecutionTarget(kind=TargetKind.HOST, os_family="posix", name="local")
ZONES = PolicyZones(
    os_family="posix",
    internal=("/home/u/.local/share/jarvis",),
    secrets=("/home/u/.ssh",),
    secret_names=("*.pem", "id_rsa*"),
    workspaces=("/home/u/work",),
)
ALLOW, DENY, ASK = PolicyOutcome.ALLOW, PolicyOutcome.DENY, PolicyOutcome.REQUIRE_APPROVAL


def decide(
    *effects: tuple[EffectKind, str],
    target: ExecutionTarget = HOST,
    zones: PolicyZones = ZONES,
    invoker: Invoker = Invoker.MODEL,
) -> tuple[PolicyOutcome, list[str]]:
    call = ToolCall(
        id=ToolCallId("task_1.call_1"),
        task_id=TaskId("task_1"),
        tool_id=ToolId("t"),
        arguments={},
        target=target,
        invoker=invoker,
    )
    preview = ToolPreview(
        summary="s",
        normalized_arguments={},
        effects=[ToolEffect(kind=kind, resource=resource) for kind, resource in effects],
        target=target,
    )
    decision = PolicyEngine(zones).decide(call, preview)
    return decision.outcome, decision.rules


@pytest.mark.parametrize(
    ("effect", "resource", "outcome", "rule"),
    [
        (EffectKind.READ, "/home/u/docs/a.txt", ALLOW, "effect.read"),
        (EffectKind.READ, "/home/u/.ssh/config", ASK, "zone.secrets.read"),
        (EffectKind.READ, "/tmp/server.pem", ASK, "zone.secrets.read"),
        (EffectKind.READ, "/home/u/id_rsa.pub", ASK, "zone.secrets.read"),
        (EffectKind.READ, "/home/u/.local/share/jarvis/data/jarvis.db", DENY, "zone.internal"),
        (EffectKind.WRITE, "/home/u/work/notes.md", ASK, "effect.write.workspace"),
        (EffectKind.CREATE, "/home/u/work/new", ASK, "effect.write.workspace"),
        (EffectKind.WRITE, "/home/u/docs/a.txt", DENY, "effect.write.outside"),
        (EffectKind.WRITE, "/home/u/work/key.pem", DENY, "zone.secrets.write"),
        (EffectKind.WRITE, "/home/u/.local/share/jarvis/config/config.toml", DENY, "zone.internal"),
        (EffectKind.DELETE, "/home/u/work/a.txt", DENY, "effect.delete"),
        (EffectKind.PROCESS_CONTROL, "pid:42", DENY, "effect.process_control"),
        (EffectKind.NETWORK, "example.com", DENY, "effect.network"),
        (EffectKind.SYSTEM_CHANGE, "registry", DENY, "effect.system_change"),
    ],
)
def test_rules_per_effect(effect: EffectKind, resource: str, outcome: PolicyOutcome, rule: str) -> None:
    assert decide((effect, resource)) == (outcome, [rule])


def test_no_effects_is_allowed() -> None:
    assert decide() == (ALLOW, ["effect.none"])


def test_the_most_restrictive_effect_wins() -> None:
    outcome, rules = decide(
        (EffectKind.READ, "/home/u/docs/a.txt"),
        (EffectKind.WRITE, "/home/u/work/b.txt"),
        (EffectKind.DELETE, "/home/u/work/c.txt"),
    )
    assert outcome is DENY
    assert rules == ["effect.delete", "effect.read", "effect.write.workspace"]


def test_zone_matching_is_by_path_component_not_string_prefix() -> None:
    assert decide((EffectKind.READ, "/home/u/.sshkeys/notes"))[0] is ALLOW
    assert decide((EffectKind.WRITE, "/home/u/workbench/a"))[0] is DENY


def test_windows_zones_ignore_case_and_separator_style() -> None:
    zones = PolicyZones(
        os_family="windows",
        internal=("C:\\Users\\U\\AppData\\Local\\Jarvis",),
        secret_names=("*.kdbx",),
        workspaces=("C:\\Work",),
    )
    target = ExecutionTarget(kind=TargetKind.HOST, os_family="windows", name="local")
    assert (
        decide((EffectKind.READ, "c:/users/u/appdata/local/JARVIS/data"), target=target, zones=zones)[0]
        is DENY
    )
    assert decide((EffectKind.READ, "D:\\Vault\\Passwords.KDBX"), target=target, zones=zones)[0] is ASK
    assert decide((EffectKind.WRITE, "c:\\work\\a.txt"), target=target, zones=zones)[0] is ASK


def test_read_roots_send_other_reads_to_a_human() -> None:
    """С `read_roots` (бенчмарк) читать без человека можно только внутри них; запись — по прежним правилам."""
    zones = PolicyZones(
        os_family="posix", read_roots=("/tmp/bench/workspace",), workspaces=("/tmp/bench/workspace",)
    )
    assert decide((EffectKind.READ, "/tmp/bench/workspace/README.md"), zones=zones) == (
        ALLOW,
        ["effect.read"],
    )
    assert decide((EffectKind.READ, "/home/u/Documents/tax.pdf"), zones=zones) == (
        ASK,
        ["zone.outside_read_roots"],
    )
    assert decide((EffectKind.READ, "processes"), zones=zones) == (ALLOW, ["effect.read"])
    assert decide((EffectKind.WRITE, "/home/u/Documents/a"), zones=zones)[0] is DENY
    assert decide((EffectKind.READ, "/home/u/docs/a.txt"))[0] is ALLOW  # без read_roots — как раньше
    with pytest.raises(ValueError, match="абсолютным"):
        PolicyZones(os_family="posix", read_roots=("workspace",))


@pytest.mark.parametrize("kind", [TargetKind.WSL, TargetKind.DOCKER, TargetKind.SSH])
def test_only_the_host_target_is_supported(kind: TargetKind) -> None:
    target = ExecutionTarget(kind=kind, os_family="posix", name="x")
    assert decide((EffectKind.READ, "/home/u/a"), target=target) == (DENY, ["target.unsupported"])


def test_preview_for_another_target_is_denied() -> None:
    call = ToolCall(
        id=ToolCallId("task_1.call_1"),
        task_id=TaskId("task_1"),
        tool_id=ToolId("t"),
        arguments={},
        target=HOST,
    )
    other = ExecutionTarget(kind=TargetKind.HOST, os_family="posix", name="other")
    preview = ToolPreview(summary="s", normalized_arguments={}, effects=[], target=other)
    assert PolicyEngine(ZONES).decide(call, preview).outcome is DENY


@pytest.mark.parametrize("root", ["", "relative/dir", "C:", "~/.ssh"])
def test_zone_roots_must_be_absolute(root: str) -> None:
    with pytest.raises(ValueError, match="абсолютным"):
        PolicyZones(os_family="posix", secrets=(root,))


@pytest.mark.parametrize(
    "resource",
    [
        "\\\\?\\C:\\Users\\U\\AppData\\Local\\Jarvis\\data",  # префикс \\?\ обходит сравнение строк
        "\\\\?\\UNC\\localhost\\C$\\Users\\U\\.ssh\\id_rsa",
        "\\\\localhost\\C$\\Users\\U\\.kube\\config",  # UNC на тот же диск
        "\\\\.\\PhysicalDrive0",
        "C:\\work\\.env::$DATA",  # поток NTFS: имя секрета не совпадёт с шаблоном
        "\\Users\\U\\.ssh\\id_rsa",  # без буквы диска
    ],
)
def test_windows_path_forms_that_dodge_zones_are_denied(resource: str) -> None:
    zones = PolicyZones(
        os_family="windows",
        internal=("C:\\Users\\U\\AppData\\Local\\Jarvis",),
        secrets=("C:\\Users\\U\\.ssh", "C:\\Users\\U\\.kube"),
        secret_names=(".env",),
    )
    target = ExecutionTarget(kind=TargetKind.HOST, os_family="windows", name="local")
    assert decide((EffectKind.READ, resource), target=target, zones=zones) == (
        DENY,
        ["path.unsupported_form"],
    )


def test_relative_posix_path_is_denied_but_named_resources_are_not_paths() -> None:
    assert decide((EffectKind.READ, "relative/notes.txt")) == (DENY, ["path.unsupported_form"])
    assert decide((EffectKind.READ, "process-table")) == (ALLOW, ["effect.read"])


SIDE_EFFECTS = [kind for kind in EffectKind if kind is not EffectKind.READ]
RESOURCES = st.one_of(
    st.sampled_from(
        [
            "/home/u/work/a.txt",
            "/home/u/work",
            "/tmp/x",
            "/home/u/.ssh/id_rsa",
            "process-table",
            "example.com",
        ]
    ),
    st.text(min_size=1, max_size=40),
)


@given(
    st.lists(st.tuples(st.sampled_from(SIDE_EFFECTS), RESOURCES), min_size=1, max_size=4),
    st.lists(st.tuples(st.just(EffectKind.READ), RESOURCES), max_size=3),
)
def test_a_call_with_side_effects_is_never_allowed_without_a_human(
    side: list[tuple[EffectKind, str]], reads: list[tuple[EffectKind, str]]
) -> None:
    """Инвариант v1: побочный эффект — отказ или подтверждение человеком. Поэтому данные, которые
    модель прочитала (недоверенный контент), не могут привести к эффекту без человека, даже пока
    признака «задача заражена» нет (ADR 0006, ADR 0023): он понадобится первому разрешённому эффекту."""
    outcome, _ = decide(*side, *reads)
    assert outcome in (DENY, ASK)


# --- LAUNCH (ADR 0030): прямая команда пользователя или предложение модели --------------------------

DIRECT, MODEL = Invoker.DIRECT, Invoker.MODEL


@pytest.mark.parametrize(
    ("resource", "invoker", "outcome", "rule"),
    [
        ("app:telegram", DIRECT, ALLOW, "launch.app.direct"),
        ("app:telegram", MODEL, ASK, "launch.app.model"),
        ("url:https://github.com", DIRECT, ALLOW, "launch.url.direct"),
        ("url:https://github.com", MODEL, ASK, "launch.url.model"),
        ("url:file:///etc/passwd", DIRECT, DENY, "launch.url.scheme"),
        ("url:javascript:alert(1)", DIRECT, DENY, "launch.url.scheme"),
        ("url:http://user:pass@evil.com", DIRECT, DENY, "launch.url.scheme"),
        ("/home/u/docs", DIRECT, ALLOW, "launch.folder.direct"),
        ("/home/u/docs", MODEL, ASK, "launch.folder.model"),
        ("/home/u/.ssh", DIRECT, ASK, "zone.secrets.launch"),
        ("/home/u/.ssh", MODEL, ASK, "zone.secrets.launch"),
        ("/home/u/.local/share/jarvis", DIRECT, DENY, "zone.internal"),
        ("/home/u/.local/share/jarvis/data", MODEL, DENY, "zone.internal"),
        ("docs", DIRECT, DENY, "launch.unknown"),
        ("cmd.exe", DIRECT, DENY, "launch.unknown"),
        ("cmd.exe /c del *", DIRECT, DENY, "path.unsupported_form"),
        ("C:/Windows/System32/cmd.exe", DIRECT, DENY, "path.unsupported_form"),
    ],
)
def test_launch_depends_on_who_proposed_it(
    resource: str, invoker: Invoker, outcome: PolicyOutcome, rule: str
) -> None:
    assert decide((EffectKind.LAUNCH, resource), invoker=invoker) == (outcome, [rule])


def test_a_direct_launch_does_not_cover_other_effects_of_the_same_call() -> None:
    outcome, rules = decide(
        (EffectKind.LAUNCH, "app:telegram"), (EffectKind.WRITE, "/home/u/docs/a.txt"), invoker=DIRECT
    )
    assert outcome is DENY
    assert "effect.write.outside" in rules


@given(
    st.lists(st.tuples(st.sampled_from(SIDE_EFFECTS), RESOURCES), min_size=1, max_size=4),
    st.lists(st.tuples(st.just(EffectKind.READ), RESOURCES), max_size=3),
)
def test_a_side_effect_proposed_by_a_model_is_never_allowed(
    side: list[tuple[EffectKind, str]], reads: list[tuple[EffectKind, str]]
) -> None:
    """ADR 0030: побочный эффект, предложенный моделью, без человека не исполняется никогда — LAUNCH
    из инвентаря тоже."""
    launches = [(EffectKind.LAUNCH, resource) for resource in ("app:telegram", "url:https://ya.ru")]
    outcome, _ = decide(*side, *launches[: len(side) % 3], *reads, invoker=MODEL)
    assert outcome in (DENY, ASK)


@given(st.lists(st.tuples(st.sampled_from(SIDE_EFFECTS), RESOURCES), min_size=1, max_size=4))
def test_a_direct_command_allows_only_a_launch(side: list[tuple[EffectKind, str]]) -> None:
    """Прямой команде без человека разрешён только LAUNCH; любой другой побочный эффект — как прежде."""
    outcome, _ = decide(*side, invoker=DIRECT)
    if any(kind is not EffectKind.LAUNCH for kind, _ in side):
        assert outcome in (DENY, ASK)
