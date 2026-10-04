"""CloudPrivacyPolicy — граница приватности облака (ADR 0028).

Чистая функция: классы данных промпта (по происхождению), текст, который уходит, настройки `cloud.*` и
разрешения человека на задачу → `PrivacyDecision`. Проверяется **каждый** вызов провайдера вне
компьютера — ровно то, что уходит: после чтения файла промпт несёт другое, чем на первом шаге.

Порядок правил: облако выключено → секреты и private_roots (никогда) → поиск секретов по тексту
(никогда) → объём промпта → классы, не разрешённые настройкой и не разрешённые человеком для этого
провайдера (согласие). Модель в решении не участвует.
"""

from collections.abc import Collection, Sequence

from jarvis.domain.paths import OsFamily, is_within
from jarvis.domain.privacy import (
    ANY_PROVIDER,
    NEVER,
    CloudGrant,
    DataClass,
    PrivacyDecision,
    PrivacyVerdict,
)
from jarvis.domain.secrets import find_secrets
from jarvis.domain.settings import CloudSettings


class CloudPrivacyPolicy:
    def __init__(
        self, settings: CloudSettings, *, os_family: OsFamily, private_roots: Sequence[str] = ()
    ) -> None:
        """`private_roots` — канонические пути (их строит сборка приложения из `cloud.private_roots`)."""
        self._settings = settings
        self._os_family: OsFamily = os_family
        self._private_roots = tuple(private_roots)

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    def is_private(self, path: str | None) -> bool:
        return path is not None and any(
            is_within(path, root, self._os_family) for root in self._private_roots
        )

    def check(
        self,
        *,
        provider: str,
        data_classes: Collection[DataClass],
        text: str,
        tokens: int,
        grants: Collection[CloudGrant] = (),
        working_directory: str | None = None,
    ) -> PrivacyDecision:
        found = set(data_classes)
        secrets_found = find_secrets(text)
        if secrets_found:
            found.add(DataClass.SECRETS)
        if self.is_private(working_directory):
            found.add(DataClass.PRIVATE)  # задача, начатая в private_roots, не уходит целиком
        listed = sorted(found)

        def decision(verdict: PrivacyVerdict, rules: list[str], reason: str, blocked: Collection[DataClass]):
            return PrivacyDecision(
                verdict=verdict,
                provider=provider,
                found=listed,
                blocked=sorted(blocked),
                secrets_found=secrets_found,
                rules=rules,
                reason=reason,
            )

        if not self._settings.enabled:
            return decision(
                PrivacyVerdict.DENY, ["cloud.disabled"], "облако выключено (cloud.enabled = false)", []
            )
        never = found & NEVER
        if never:
            rules = [f"privacy.never.{item.value}" for item in sorted(never)]
            if secrets_found:
                rules.append("privacy.scanner")
            return decision(
                PrivacyVerdict.DENY, rules, "секреты и private_roots не покидают компьютер", never
            )
        if tokens > self._settings.max_prompt_tokens:
            return decision(
                PrivacyVerdict.DENY,
                ["privacy.prompt_too_large"],
                f"промпт {tokens} токенов больше cloud.max_prompt_tokens "
                f"({self._settings.max_prompt_tokens})",
                [],
            )
        allowed = self._allowed() | {
            grant.data_class for grant in grants if grant.provider in (provider, ANY_PROVIDER)
        }
        missing = found - allowed
        if missing:
            return decision(
                PrivacyVerdict.CONSENT,
                [f"privacy.consent.{item.value}" for item in sorted(missing)],
                "не разрешено настройкой и человеком для этой задачи: "
                + ", ".join(item.value for item in sorted(missing)),
                missing,
            )
        granted = sorted(item.value for item in found - self._allowed())
        rules = ["privacy.allowed", *(f"privacy.granted.{item}" for item in granted)]
        return decision(PrivacyVerdict.ALLOW, rules, "классы данных разрешены", [])

    def _allowed(self) -> set[DataClass]:
        settings = self._settings
        flags = {
            DataClass.LOCAL_METADATA: settings.allow_local_metadata,
            DataClass.FILE_CONTENT: settings.allow_file_content,
            DataClass.SOURCE_CODE: settings.allow_source_code,
            DataClass.PERSONAL_DATA: settings.allow_personal_data,
        }
        return {item for item, allowed in flags.items() if allowed}
