"""`cloud.share` — согласие человека отправить классы данных задачи провайдеру вне компьютера (ADR 0028).

Служебный инструмент: модель его не видит и вызвать не может, вызывает его сам Jarvis (исполнитель
агента), когда облачному провайдеру уровня мешает только неразрешённый класс данных. Эффект —
CLOUD_SHARE: политика требует подтверждения человека; одобрение — разрешение класса этому провайдеру до
конца задачи. Сам вызов ничего не отправляет: отправку делает Model Gateway после проверки границы.
"""

from pydantic import BaseModel, Field, field_validator

from jarvis.domain.privacy import CLOUD_SHARE_TOOL, NEVER, DataClass
from jarvis.domain.tools import (
    EffectKind,
    TargetKind,
    ToolDefinition,
    ToolEffect,
    ToolId,
    ToolPreview,
    ToolVerification,
)
from jarvis.ports.tools import ToolContext

_NAMES = {
    DataClass.LOCAL_METADATA: "имена файлов и метаданные",
    DataClass.FILE_CONTENT: "содержимое файлов",
    DataClass.SOURCE_CODE: "исходный код",
    DataClass.PERSONAL_DATA: "личные данные",
}


class CloudShareArgs(BaseModel, frozen=True, extra="forbid"):
    provider: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    data_classes: list[DataClass] = Field(min_length=1, max_length=len(DataClass))

    @field_validator("data_classes")
    @classmethod
    def _consentable(cls, value: list[DataClass]) -> list[DataClass]:
        if set(value) & NEVER:
            raise ValueError("секреты и private_roots не отправляются ни с каким согласием")
        return sorted(set(value))


class CloudShareOutput(BaseModel, frozen=True, extra="forbid"):
    provider: str
    data_classes: list[DataClass]
    granted: bool


class CloudShareTool:
    definition = ToolDefinition(
        id=ToolId(CLOUD_SHARE_TOOL),
        description=(
            "Согласие человека отправить классы данных этой задачи облачному провайдеру (служебный).\n"
            "Вызывает только Jarvis; разрешение действует до конца задачи."
        ),
        input_model=CloudShareArgs,
        output_model=CloudShareOutput,
        effects=frozenset({EffectKind.CLOUD_SHARE}),
        targets=frozenset({TargetKind.HOST}),
        timeout_s=5.0,
        untrusted_output=False,
        output_data=frozenset({DataClass.LOCAL_METADATA}),
        model_visible=False,
    )

    async def preview(self, arguments: BaseModel, context: ToolContext) -> ToolPreview:
        assert isinstance(arguments, CloudShareArgs)
        names = ", ".join(_NAMES.get(item, item.value) for item in arguments.data_classes)
        classes = ",".join(item.value for item in arguments.data_classes)
        return ToolPreview(
            summary=f"Отправить облачному провайдеру {arguments.provider} для этой задачи: {names}",
            normalized_arguments=arguments.model_dump(mode="json"),
            effects=[
                ToolEffect(kind=EffectKind.CLOUD_SHARE, resource=f"cloud:{arguments.provider}:{classes}")
            ],
            target=context.target,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel:
        assert isinstance(arguments, CloudShareArgs)
        return CloudShareOutput(
            provider=arguments.provider, data_classes=arguments.data_classes, granted=True
        )

    async def verify(self, arguments: BaseModel, output: BaseModel, context: ToolContext) -> ToolVerification:
        assert isinstance(output, CloudShareOutput)
        return ToolVerification(passed=output.granted, checks=["разрешение записано до конца задачи"])
