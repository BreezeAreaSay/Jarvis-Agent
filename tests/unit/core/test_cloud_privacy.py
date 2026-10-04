"""Граница приватности облака (ADR 0028): классы данных, поиск секретов, происхождение результата."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jarvis.core.models.privacy import CloudPrivacyPolicy
from jarvis.core.policy import PolicyZones
from jarvis.core.tools.provenance import DataClassifier
from jarvis.domain.privacy import ANY_PROVIDER, NEVER, CloudGrant, DataClass, PrivacyVerdict, request_classes
from jarvis.domain.secrets import MASK, find_secrets, mask_secrets
from jarvis.domain.settings import CloudSettings
from jarvis.domain.tools import EffectKind, ExecutionTarget, TargetKind, ToolEffect, ToolPreview, ToolResult
from tests.fakes import FakeTool

ON = CloudSettings(enabled=True)
META = [DataClass.LOCAL_METADATA]
HOST = ExecutionTarget(kind=TargetKind.HOST, os_family="posix", name="test")


def check(
    classes: list[DataClass],
    *,
    text: str = "обычный текст",
    settings: CloudSettings = ON,
    grants: list[CloudGrant] | None = None,
    working_directory: str | None = None,
    tokens: int = 100,
):
    policy = CloudPrivacyPolicy(settings, os_family="posix", private_roots=["/work/private"])
    return policy.check(
        provider="cloud_a",
        data_classes=classes,
        text=text,
        tokens=tokens,
        grants=grants or [],
        working_directory=working_directory,
    )


def test_plain_text_may_leave_when_the_cloud_is_enabled() -> None:
    decision = check(META)
    assert decision.verdict is PrivacyVerdict.ALLOW
    assert decision.rules == ["privacy.allowed"]


def test_nothing_leaves_when_the_cloud_is_disabled() -> None:
    decision = check(META, settings=CloudSettings())
    assert decision.verdict is PrivacyVerdict.DENY
    assert decision.rules == ["cloud.disabled"]


@pytest.mark.parametrize("data", [DataClass.FILE_CONTENT, DataClass.SOURCE_CODE, DataClass.PERSONAL_DATA])
def test_files_code_and_personal_data_need_consent_by_default(data: DataClass) -> None:
    decision = check([DataClass.LOCAL_METADATA, data])
    assert decision.verdict is PrivacyVerdict.CONSENT
    assert decision.blocked == [data]
    assert decision.rules == [f"privacy.consent.{data.value}"]


@pytest.mark.parametrize("data", [DataClass.PRIVATE, DataClass.SECRETS])
def test_private_and_secrets_never_leave_even_with_consent_or_settings(data: DataClass) -> None:
    settings = CloudSettings(
        enabled=True, allow_file_content=True, allow_source_code=True, allow_personal_data=True
    )
    grants = [CloudGrant(provider=ANY_PROVIDER, data_class=item) for item in DataClass if item not in NEVER]
    decision = check([data], settings=settings, grants=grants)
    assert decision.verdict is PrivacyVerdict.DENY
    assert decision.blocked == [data]


def test_secrets_cannot_be_granted_at_all() -> None:
    with pytest.raises(ValueError, match="не разрешается"):
        CloudGrant(provider="cloud_a", data_class=DataClass.SECRETS)
    with pytest.raises(ValueError, match="allow_secrets"):
        CloudSettings(enabled=True, allow_secrets=True)  # type: ignore[arg-type]


def test_a_task_started_in_a_private_root_never_leaves() -> None:
    decision = check(META, working_directory="/work/private/repo")
    assert decision.verdict is PrivacyVerdict.DENY
    assert DataClass.PRIVATE in decision.found


@pytest.mark.parametrize(
    "text",
    [
        "Проанализируй /work/private/plan-merger.md",
        "что лежит в /work/private?",
        '{"path": "/work/private/a.txt"}',
    ],
)
def test_a_path_from_private_roots_in_the_text_never_leaves(text: str) -> None:
    decision = check(META, text=text)
    assert decision.verdict is PrivacyVerdict.DENY
    assert DataClass.PRIVATE in decision.found
    assert "privacy.private_path" in decision.rules


@pytest.mark.parametrize("text", ["/work/private-2/a.txt", "/mnt/work/private/a.txt", "/work/privates"])
def test_a_similar_but_different_path_is_not_private(text: str) -> None:
    assert check(META, text=text).verdict is PrivacyVerdict.ALLOW


@pytest.mark.parametrize(
    "text",
    [
        "c:/users/u/private/notes.txt",
        "C:\\Users\\U\\Private\\notes.txt",
        json.dumps({"path": "C:\\Users\\u\\Private\\notes.txt"}),  # наблюдение: «\» удвоены
        "смотри C:\\Users\\u\\Private",
    ],
)
def test_windows_private_paths_match_regardless_of_case_and_separator(text: str) -> None:
    policy = CloudPrivacyPolicy(ON, os_family="windows", private_roots=["C:\\Users\\u\\Private"])
    assert policy.mentions_private(text)
    assert not policy.mentions_private("C:\\Users\\u\\Private2\\notes.txt")


def test_a_key_inside_otherwise_allowed_text_blocks_the_call() -> None:
    decision = check(META, text="вот мой ключ: sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    assert decision.verdict is PrivacyVerdict.DENY
    assert decision.secrets_found == ["api_key"]
    assert "privacy.scanner" in decision.rules
    assert "ABCDEFGHIJ" not in decision.model_dump_json()  # в решении — категории, не значения


def test_a_grant_allows_the_class_only_for_that_provider() -> None:
    grant = [CloudGrant(provider="cloud_a", data_class=DataClass.SOURCE_CODE)]
    assert check([DataClass.SOURCE_CODE], grants=grant).verdict is PrivacyVerdict.ALLOW
    other = [CloudGrant(provider="cloud_b", data_class=DataClass.SOURCE_CODE)]
    assert check([DataClass.SOURCE_CODE], grants=other).verdict is PrivacyVerdict.CONSENT
    launch = [CloudGrant(provider=ANY_PROVIDER, data_class=DataClass.SOURCE_CODE)]
    decision = check([DataClass.SOURCE_CODE], grants=launch)
    assert decision.verdict is PrivacyVerdict.ALLOW
    assert "privacy.granted.source_code" in decision.rules


def test_settings_can_allow_a_class_for_every_task() -> None:
    settings = CloudSettings(enabled=True, allow_file_content=True)
    assert check([DataClass.FILE_CONTENT], settings=settings).verdict is PrivacyVerdict.ALLOW
    assert check([DataClass.SOURCE_CODE], settings=settings).verdict is PrivacyVerdict.CONSENT


def test_an_oversized_prompt_stays_local() -> None:
    decision = check(META, tokens=40_000)
    assert decision.verdict is PrivacyVerdict.DENY
    assert decision.rules == ["privacy.prompt_too_large"]


@given(st.sets(st.sampled_from(list(DataClass))), st.booleans(), st.booleans(), st.booleans())
def test_private_and_secrets_are_never_allowed(
    classes: set[DataClass], files: bool, code: bool, personal: bool
) -> None:
    settings = CloudSettings(
        enabled=True, allow_file_content=files, allow_source_code=code, allow_personal_data=personal
    )
    grants = [CloudGrant(provider=ANY_PROVIDER, data_class=item) for item in DataClass if item not in NEVER]
    decision = check(sorted(classes), settings=settings, grants=grants)
    if classes & NEVER:
        assert decision.verdict is PrivacyVerdict.DENY


# --- классы запроса и поиск секретов --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Посмотри:\n```python\nprint(1)\n```",
        "Traceback (most recent call last):\n  File 'x.py', line 1",
        "def f(x):\n    return x\nclass A:\n    pass\nimport os",
    ],
)
def test_code_pasted_into_the_request_is_source_code(text: str) -> None:
    assert DataClass.SOURCE_CODE in request_classes(text)


def test_an_ordinary_request_is_metadata() -> None:
    assert request_classes("Объясни, что такое Docker") == {DataClass.LOCAL_METADATA}
    assert request_classes("Какие Python процессы работают?") == {DataClass.LOCAL_METADATA}


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA==", "private_key"),
        ("Authorization: Bearer abcdefghijklmnop1234", "authorization_header"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "jwt",
        ),
        ("key = AKIAIOSFODNN7EXAMPLE", "api_key"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "api_key"),
        ("DB_PASSWORD=hunter2hunter2", "secret_assignment"),
        ("пароль: qwerty12345", "secret_assignment"),
        ("postgres://admin:s3cretpass@db.internal:5432/app", "connection_string"),
        ("Server=db;Database=app;User Id=sa;Password=Passw0rd!;", "connection_string"),
    ],
)
def test_the_scanner_finds_common_secrets(text: str, category: str) -> None:
    assert category in find_secrets(text)
    assert MASK in mask_secrets(text)


def test_the_scanner_ignores_ordinary_text() -> None:
    assert find_secrets("Docker — это контейнеры. Пароль от Wi-Fi спроси у администратора.") == []
    assert find_secrets('api_key = "env:JARVIS_SMART_API_KEY"') == []  # ссылка на переменную — не ключ


def test_masking_removes_a_known_value() -> None:
    assert mask_secrets("ошибка для ключа abc-123-XYZ", "abc-123-XYZ") == f"ошибка для ключа {MASK}"


# --- происхождение результата инструмента ------------------------------------------------------

ZONES = PolicyZones(os_family="posix", secrets=("/home/u/.ssh",), secret_names=(".env", "*.pem"))
CLASSIFIER = DataClassifier(ZONES, private_roots=["/work/private"], personal_roots=["/home/u/Documents"])


def classify(path: str, *, data: frozenset[DataClass] = frozenset({DataClass.FILE_CONTENT}), output=None):
    tool = FakeTool("t", output_data=data)
    preview = ToolPreview(
        summary="s",
        normalized_arguments={},
        effects=[ToolEffect(kind=EffectKind.READ, resource=path)],
        target=HOST,
    )
    result = ToolResult(output=output or {"value": "x"})
    return CLASSIFIER.classify(tool.definition, preview, result)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/work/notes.txt", {DataClass.FILE_CONTENT}),
        ("/work/main.py", {DataClass.FILE_CONTENT, DataClass.SOURCE_CODE}),
        ("/work/Dockerfile", {DataClass.FILE_CONTENT, DataClass.SOURCE_CODE}),
        ("/work/.env", {DataClass.FILE_CONTENT, DataClass.SECRETS}),
        ("/home/u/.ssh/config", {DataClass.FILE_CONTENT, DataClass.SECRETS}),
        ("/work/private/plan.md", {DataClass.FILE_CONTENT, DataClass.PRIVATE}),
        ("/home/u/Documents/passport.pdf", {DataClass.FILE_CONTENT, DataClass.PERSONAL_DATA}),
    ],
)
def test_data_classes_come_from_where_the_data_was_read(path: str, expected: set[DataClass]) -> None:
    assert classify(path) == expected


def test_paths_inside_the_result_are_classified_too() -> None:
    listing = {"entries": [{"path": "/work/private/secret-plan.md"}, {"path": "/work/a.txt"}]}
    found = classify("/work", data=frozenset({DataClass.LOCAL_METADATA}), output=listing)
    assert found == {DataClass.LOCAL_METADATA, DataClass.PRIVATE}  # имена из private_roots — тоже private


def test_listing_names_is_metadata() -> None:
    found = classify(
        "/work", data=frozenset({DataClass.LOCAL_METADATA}), output={"entries": [{"path": "/work/a"}]}
    )
    assert found == {DataClass.LOCAL_METADATA}


def test_a_large_result_is_classified_from_the_head_too() -> None:
    # Начало результата — то, что видит модель; классы не теряются за пределом числа строк.
    entries = [{"name": "passport.pdf", "path": "/home/u/Documents/passport.pdf", "kind": "file"}]
    entries += [{"name": f"f{i}", "path": f"/work/f{i}", "kind": "file"} for i in range(5000)]
    entries.append({"name": "plan.md", "path": "/work/private/plan.md", "kind": "file"})
    found = classify("/work", data=frozenset({DataClass.LOCAL_METADATA}), output={"entries": entries})
    assert {DataClass.PERSONAL_DATA, DataClass.PRIVATE} <= found


def test_paths_inside_longer_strings_and_keys_are_classified() -> None:
    output = {
        "processes": [{"pid": 1, "command": "python3 /work/private/job.py --fast"}],
        "/home/u/.ssh/id_ed25519": "ключ как имя поля",
    }
    found = classify("/work", data=frozenset({DataClass.LOCAL_METADATA}), output=output)
    assert {DataClass.PRIVATE, DataClass.SECRETS} <= found
