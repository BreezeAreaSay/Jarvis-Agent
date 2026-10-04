"""Policy Engine v1 (04-security.md §2, ADR 0022, ADR 0030): чистая функция от вызова, его preview и зон.

Решение не зависит ни от содержимого, прочитанного раньше, ни от текста модели: права не выводятся
из данных. Самое строгое правило среди всех эффектов вызова побеждает: DENY > REQUIRE_APPROVAL > ALLOW.

Единственный побочный эффект, который может пройти без человека, — LAUNCH (открыть приложение из
инвентаря, http(s)-адрес, папку) по прямой команде пользователя. Тот же LAUNCH, предложенный моделью,
требует подтверждения: побочный эффект, предложенный моделью, без человека не исполняется никогда.
"""

import fnmatch
from dataclasses import dataclass

from jarvis.domain.paths import OsFamily, is_absolute, is_within, name_of, unsupported_form
from jarvis.domain.tools import (
    EffectKind,
    Invoker,
    PolicyDecision,
    PolicyOutcome,
    TargetKind,
    ToolCall,
    ToolEffect,
    ToolPreview,
)
from jarvis.domain.urls import is_web_url

_FORBIDDEN = {
    EffectKind.DELETE: "удаление в Session 3 не поддерживается",
    EffectKind.PROCESS_CONTROL: "управление процессами запрещено",
    EffectKind.NETWORK: "сетевые действия запрещены",
    EffectKind.SYSTEM_CHANGE: "изменение системы запрещено",
}
_RANK = {PolicyOutcome.ALLOW: 0, PolicyOutcome.REQUIRE_APPROVAL: 1, PolicyOutcome.DENY: 2}

# Ресурсы эффекта LAUNCH (ADR 0030): приложение из инвентаря, веб-адрес; папка — канонический путь.
APP_RESOURCE = "app:"
URL_RESOURCE = "url:"


@dataclass(frozen=True)
class PolicyZones:
    """Зоны путей (канонические абсолютные пути той же ОС)."""

    os_family: OsFamily
    internal: tuple[str, ...] = ()  # данные Jarvis: база, аудит — ни читать, ни менять инструментами
    secrets: tuple[str, ...] = ()  # ~/.ssh, ~/.gnupg …: чтение — с подтверждением, запись — запрет
    secret_names: tuple[str, ...] = ()  # шаблоны имён файлов с секретами: *.kdbx, id_rsa*, *.pem
    workspaces: tuple[str, ...] = ()  # запись — с подтверждением; вне их — запрет
    # Если заданы — чтение вне этих папок требует подтверждения; пусто — читать можно везде, кроме зон выше.
    read_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Пустой или относительный корень совпал бы с чем угодно (или ни с чем) — это ошибка сборки.
        for root in (*self.internal, *self.secrets, *self.workspaces, *self.read_roots):
            if not is_absolute(root, self.os_family):
                raise ValueError(f"корень зоны должен быть абсолютным путём {self.os_family}: {root!r}")

    def is_internal(self, path: str) -> bool:
        return any(is_within(path, root, self.os_family) for root in self.internal)

    def is_secret(self, path: str) -> bool:
        if any(is_within(path, root, self.os_family) for root in self.secrets):
            return True
        name = name_of(path, self.os_family)
        if self.os_family == "windows":
            name = name.casefold()
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.secret_names)

    def is_workspace(self, path: str) -> bool:
        return any(is_within(path, root, self.os_family) for root in self.workspaces)

    def is_freely_readable(self, path: str) -> bool:
        return not self.read_roots or any(is_within(path, root, self.os_family) for root in self.read_roots)


def _looks_like_path(resource: str) -> bool:
    return "/" in resource or "\\" in resource


class PolicyEngine:
    def __init__(self, zones: PolicyZones) -> None:
        self._zones = zones

    def decide(self, call: ToolCall, preview: ToolPreview) -> PolicyDecision:
        if preview.target.kind is not TargetKind.HOST or call.target != preview.target:
            return PolicyDecision(
                outcome=PolicyOutcome.DENY, rules=["target.unsupported"], reason="поддерживается только HOST"
            )
        if not preview.effects:
            return PolicyDecision(outcome=PolicyOutcome.ALLOW, rules=["effect.none"], reason="без эффектов")
        verdicts = [self._effect(effect, call.invoker) for effect in preview.effects]
        worst = max(verdicts, key=lambda verdict: _RANK[verdict[0]])
        rules = sorted({rule for _, rule, _ in verdicts})
        return PolicyDecision(outcome=worst[0], rules=rules, reason=worst[2])

    def _effect(self, effect: ToolEffect, invoker: Invoker) -> tuple[PolicyOutcome, str, str]:
        if effect.kind is EffectKind.LAUNCH:
            return self._launch(effect.resource, invoker)
        zones, resource = self._zones, effect.resource
        path_like = _looks_like_path(resource)
        if path_like and (
            unsupported_form(resource, zones.os_family) or not is_absolute(resource, zones.os_family)
        ):
            # Такой путь может указывать в любую зону в обход сравнения строк — решать по нему нельзя.
            return PolicyOutcome.DENY, "path.unsupported_form", f"форма пути не поддерживается: {resource}"
        if zones.is_internal(resource):
            return PolicyOutcome.DENY, "zone.internal", f"данные Jarvis недоступны инструментам: {resource}"
        if effect.kind in _FORBIDDEN:
            return PolicyOutcome.DENY, f"effect.{effect.kind.value}", _FORBIDDEN[effect.kind]
        if effect.kind in (EffectKind.WRITE, EffectKind.CREATE):
            if zones.is_secret(resource):
                return PolicyOutcome.DENY, "zone.secrets.write", f"запись в секреты запрещена: {resource}"
            if zones.is_workspace(resource):
                return (
                    PolicyOutcome.REQUIRE_APPROVAL,
                    "effect.write.workspace",
                    f"запись в рабочей папке требует подтверждения: {resource}",
                )
            return (
                PolicyOutcome.DENY,
                "effect.write.outside",
                f"запись вне рабочих папок запрещена: {resource}",
            )
        if zones.is_secret(resource):
            return (
                PolicyOutcome.REQUIRE_APPROVAL,
                "zone.secrets.read",
                f"чтение секретов требует подтверждения: {resource}",
            )
        if path_like and not zones.is_freely_readable(resource):
            return (
                PolicyOutcome.REQUIRE_APPROVAL,
                "zone.outside_read_roots",
                f"чтение вне разрешённых папок требует подтверждения: {resource}",
            )
        return PolicyOutcome.ALLOW, "effect.read", "чтение разрешено"

    def _launch(self, resource: str, invoker: Invoker) -> tuple[PolicyOutcome, str, str]:
        """LAUNCH: только приложение из инвентаря, http(s)-адрес или папка вне внутренних зон."""
        zones = self._zones
        if resource.startswith(APP_RESOURCE):
            what, kind = f"запуск приложения {resource.removeprefix(APP_RESOURCE)}", "app"
        elif resource.startswith(URL_RESOURCE):
            if not is_web_url(resource.removeprefix(URL_RESOURCE)):
                return (
                    PolicyOutcome.DENY,
                    "launch.url.scheme",
                    f"открывать можно только http(s)-адреса: {resource}",
                )
            what, kind = f"открыть {resource.removeprefix(URL_RESOURCE)}", "url"
        elif _looks_like_path(resource):
            if unsupported_form(resource, zones.os_family) or not is_absolute(resource, zones.os_family):
                return (
                    PolicyOutcome.DENY,
                    "path.unsupported_form",
                    f"форма пути не поддерживается: {resource}",
                )
            if zones.is_internal(resource):
                return (
                    PolicyOutcome.DENY,
                    "zone.internal",
                    f"данные Jarvis недоступны инструментам: {resource}",
                )
            if zones.is_secret(resource):
                return (
                    PolicyOutcome.REQUIRE_APPROVAL,
                    "zone.secrets.launch",
                    f"открыть папку с секретами — только с подтверждением: {resource}",
                )
            what, kind = f"открыть папку {resource}", "folder"
        else:
            return PolicyOutcome.DENY, "launch.unknown", f"запускать можно только известное: {resource}"
        if invoker is Invoker.DIRECT:
            return PolicyOutcome.ALLOW, f"launch.{kind}.direct", f"{what}: прямая команда пользователя"
        return (
            PolicyOutcome.REQUIRE_APPROVAL,
            f"launch.{kind}.model",
            f"{what}: предложено моделью — нужно подтверждение человека",
        )
