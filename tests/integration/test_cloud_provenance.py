"""Классы данных на настоящей файловой системе (ADR 0028): личные папки сравниваются с путями чтения
в канонической форме — ссылка на «Документы» не прячет личные данные."""

import os
import sys
from pathlib import Path

import pytest

from jarvis.adapters.inventory.posix import PosixInventory
from jarvis.adapters.tools.filesystem import ReadTextTool
from jarvis.app.composition import build_app
from jarvis.domain.models import ModelRole
from jarvis.domain.privacy import DataClass
from jarvis.domain.routing import CloudMode
from jarvis.domain.task import Origin, TaskRequest
from jarvis.evals.models import ModelReply
from tests.helpers import TEST_TARGET, TEST_ZONES
from tests.hybrid import Hybrid, finish

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(sys.platform == "win32", reason="символьные ссылки и PosixInventory — POSIX"),
]


def read_text(path: str) -> ModelReply:
    action = {"type": "tool", "tool": "filesystem.read_text", "arguments": {"path": path}}
    return ModelReply.model_validate({"json": {"decision": "читаю", "action": action}})


@pytest.mark.parametrize("linked", [False, True], ids=["folder", "symlink"])
async def test_a_file_in_documents_is_personal_data_even_through_a_symlink(
    tmp_path: Path, linked: bool
) -> None:
    base = Path(os.path.realpath(tmp_path))
    home, real = base / "home", base / "data" / "Documents"
    home.mkdir()
    real.mkdir(parents=True)
    if linked:
        (home / "Documents").symlink_to(real)
    else:
        (home / "Documents").mkdir()
    (home / "Documents" / "medical.txt").write_text("диагноз", encoding="utf-8")
    target = str(home / "Documents" / "medical.txt")
    hybrid = Hybrid([finish("локально")], {"cloud_a": [read_text(target), finish("облако")]})
    app = build_app(
        hybrid.config,
        models={ModelRole.EXECUTOR: hybrid.local},
        remote_models=hybrid.remotes,
        storage=hybrid.storage,
        clock=hybrid.clock,
        tools=[ReadTextTool()],
        target=TEST_TARGET,
        zones=TEST_ZONES,
        inventory=PosixInventory(home=home, application_dirs=[]),
    )
    request = TaskRequest(
        text="Проанализируй файл",
        origin=Origin.EVAL,
        mode=CloudMode.SMART,
        allow_cloud=[DataClass.FILE_CONTENT],
    )
    task_id = app.tasks.submit(request)
    await app.tasks.run_until_blocked(task_id)
    (approval,) = app.tasks.approvals(task_id)  # file_content разрешён заранее — спрашивают про личное
    assert approval.call.tool_id == "cloud.share"
    assert approval.call.arguments["data_classes"] == ["personal_data"]
    assert len(hybrid.remotes["cloud_a"].requests) == 1  # содержимое файла в облако не ушло
