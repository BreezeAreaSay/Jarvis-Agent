"""Граница приватности облака: классы данных, разрешения на задачу, решение (ADR 0028).

Классы данных ставит Jarvis там, где данные входят в задачу — по происхождению (запрос, результат
инструмента, путь, зона), а не по мнению модели. Решение о вызове провайдера вне компьютера принимает
`CloudPrivacyPolicy` (`core/models/privacy.py`) по этим классам и по самому тексту, который уходит.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class DataClass(StrEnum):
    LOCAL_METADATA = "local_metadata"  # имена файлов, пути, процессы
    FILE_CONTENT = "file_content"  # содержимое файлов
    SOURCE_CODE = "source_code"  # код: файлы кода по расширению или имени; код, вставленный в запрос
    PERSONAL_DATA = "personal_data"  # данные из личных папок пользователя
    PRIVATE = "private"  # всё из cloud.private_roots и задачи, начатые там — никогда
    SECRETS = "secrets"  # зона секретов, найденные ключи, токены, пароли — никогда


# Не уходят из компьютера ни при каких настройках и ни с каким согласием.
NEVER = frozenset({DataClass.PRIVATE, DataClass.SECRETS})
# Уходят, только если разрешены настройкой или человеком для этой задачи.
CONSENTABLE = frozenset(
    {DataClass.LOCAL_METADATA, DataClass.FILE_CONTENT, DataClass.SOURCE_CODE, DataClass.PERSONAL_DATA}
)
ANY_PROVIDER = "*"  # разрешение, данное до запуска (`--allow-cloud`): любому провайдеру этой задачи
CLOUD_SHARE_TOOL = "cloud.share"  # служебный инструмент согласия: вызывает только Jarvis, модель не видит


class CloudGrant(BaseModel, frozen=True, extra="forbid"):
    """Разрешение человека отправить класс данных провайдеру — только до конца этой задачи."""

    provider: str = Field(min_length=1)  # ID провайдера из конфига или ANY_PROVIDER
    data_class: DataClass

    @field_validator("data_class")
    @classmethod
    def _consentable(cls, value: DataClass) -> DataClass:
        if value in NEVER:
            raise ValueError(f"класс {value} не разрешается ни настройкой, ни согласием")
        return value


class PrivacyVerdict(StrEnum):
    ALLOW = "allow"  # можно отправить
    CONSENT = "consent"  # нельзя без разрешения человека на эти классы
    DENY = "deny"  # нельзя вовсе: секреты, private_roots, слишком большой промпт, облако выключено


class PrivacyDecision(BaseModel, frozen=True, extra="forbid"):
    verdict: PrivacyVerdict
    provider: str
    found: list[DataClass]  # классы данных в промпте
    blocked: list[DataClass] = []  # что мешает: никогда не уходящие или не разрешённые классы
    secrets_found: list[str] = []  # категории находок поиска секретов (без значений)
    rules: list[str] = Field(min_length=1)
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict is PrivacyVerdict.ALLOW


_FENCE = re.compile(r"```")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)|^\s+at [\w.$]+\(.*:\d+\)$", re.MULTILINE)
_CODE_LINE = re.compile(
    r"^\s*(?:def |class |import |from \S+ import |function |const |let |var |public |private |"
    r"#include|package |func |fn |using |SELECT |INSERT |UPDATE |CREATE TABLE)"
    r"|[;{}]\s*$"
    r"|^\s*(?:if|for|while|return)\b.*[:{(]",
    re.MULTILINE,
)


def request_classes(text: str) -> frozenset[DataClass]:
    """Классы данных в самом запросе пользователя. Код и трассировки, вставленные в запрос, — исходный
    код (ADR 0028); остальное — метаданные. Секреты ищет `CloudPrivacyPolicy` по всему промпту."""
    code_lines = len(_CODE_LINE.findall(text))
    if _FENCE.search(text) or _TRACEBACK.search(text) or code_lines >= 3:
        return frozenset({DataClass.LOCAL_METADATA, DataClass.SOURCE_CODE})
    return frozenset({DataClass.LOCAL_METADATA})


# Файлы кода по расширению и имени: их содержимое — не просто файл, а исходный код.
CODE_EXTENSIONS = frozenset(
    {
        ".py", ".pyi", ".ipynb", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts",
        ".scala", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".fs", ".vb", ".swift",
        ".m", ".mm", ".rb", ".php", ".pl", ".lua", ".r", ".jl", ".dart", ".sh", ".bash", ".zsh", ".ps1",
        ".psm1", ".bat", ".cmd", ".sql", ".vue", ".svelte", ".html", ".css", ".scss", ".gradle", ".tf",
        ".hcl", ".proto", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".json",
    }
)  # fmt: skip
CODE_FILE_NAMES = frozenset(
    {"makefile", "dockerfile", "jenkinsfile", "vagrantfile", "gemfile", "rakefile", "procfile", "justfile"}
)
