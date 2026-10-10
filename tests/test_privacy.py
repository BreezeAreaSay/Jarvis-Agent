"""pc.privacy: всё, что уходит мозгу, — без закрытых путей и имён из private_paths."""

import json

import pytest

from pc import paths, privacy
from pc.result import Result

HIDDEN = privacy.HIDDEN

WIN_ENV = {
    "SystemRoot": r"C:\Windows",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "ProgramData": r"C:\ProgramData",
    "USERPROFILE": r"C:\Users\me",
    "APPDATA": r"C:\Users\me\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
}


@pytest.fixture(autouse=True)
def fake_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in paths._ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in WIN_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("JARVIS_DATA_DIR", r"C:\JarvisData")
    monkeypatch.setattr(paths, "_resolve", lambda p: p)
    monkeypatch.setattr(paths, "_known_folder", lambda name: None)


@pytest.fixture
def private(config_file):
    def write(*items: str) -> None:
        body = ", ".join(json.dumps(p, ensure_ascii=False) for p in items)
        config_file(f"[pc]\nprivate_paths = [{body}]\n")

    return write


# --- заголовки ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Отчёт\r\nчерновик\tверсия\x00 2", "Отчёт черновик версия 2"),
        ("doc\u202etxt.exe", "doctxt.exe"),
        ("a\u2028b\x7fc", "a b c"),
        ("  Блокнот  ", "Блокнот"),
        ("", ""),
        ("  \n ", ""),
        (None, ""),
    ],
)
def test_title_cleanup(title: str, expected: str) -> None:
    assert privacy.redact_title(title) == expected


def test_title_length() -> None:
    long = privacy.redact_title("я" * 200)
    assert len(long) == 80 and long.endswith("…")
    assert privacy.redact_title("ж" * 80) == "ж" * 80
    cut = privacy.redact_title("ж" * 81)
    assert len(cut) == 80 and cut.endswith("…")
    assert len(privacy.redact_title("слово " * 40)) <= 80


@pytest.mark.parametrize(
    "title",
    [
        "Тайное — Проводник",
        "тайное",
        "Папка ТАЙНОЕ открыта",
        "план-побега.docx - Word",
        "план-побега - Word",  # Проводник и Word часто прячут расширение
    ],
)
def test_title_with_private_name_hidden(private, title: str) -> None:
    private(r"D:\Тайное", r"C:\Users\me\Documents\план-побега.docx")
    assert privacy.redact_title(title) == HIDDEN


def test_title_without_private_name_kept(private) -> None:
    private(r"D:\Тайное")
    assert privacy.redact_title("Обычное окно — Блокнот") == "Обычное окно — Блокнот"


def test_title_with_zone_path(private) -> None:
    title = privacy.redact_title(r"C:\Users\me\AppData\Roaming\Telegram Desktop - Проводник")
    assert "AppData" not in title and HIDDEN in title
    assert privacy.redact_title(r"C:\Users\me\Documents - Проводник") == r"C:\Users\me\Documents - Проводник"


def test_title_hidden_when_config_broken(config_file) -> None:
    config_file("[pc\nprivate_paths = [")
    assert privacy.redact_title("Блокнот") == HIDDEN


# --- пути ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\me\.ssh\id_rsa",
        r"C:\Users\me\AppData\Local\Google\Chrome\User Data",
        r"\\server\share\x",
        r"C:\JarvisData\journal.jsonl",
        r"C:\Users\me\proj\.env",
        r"C:\Windows\System32\cmd.exe",
        r"D:\Тайное\x.txt",
        r"C:\Users\me\Desktop\Тайное — копия\x.txt",
        r"C:\x\file.txt:stream",
    ],
)
def test_redact_path_hidden(private, path: str) -> None:
    private(r"D:\Тайное")
    assert privacy.redact_path(path) is None


def test_redact_path_kept(private) -> None:
    private(r"D:\Тайное")
    path = r"C:\Users\me\Documents\отчёт.docx"
    assert privacy.redact_path(path) == path


# --- свободный текст ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (r"Нет доступа: C:\Users\me\AppData\Roaming\Telegram Desktop\tdata", "Нет доступа: (скрыто)"),
        (
            "PermissionError: [WinError 5] Отказано в доступе: 'C:\\\\Users\\\\me\\\\.ssh\\\\id_rsa'",
            "PermissionError: [WinError 5] Отказано в доступе: '(скрыто)'",
        ),
        (r"Файл C:\Users\me\proj\.env не найден", "Файл (скрыто) не найден"),
        (
            r"Открыл C:\Users\me\Documents\отчёт.docx и C:\Users\me\.codex\auth.json.",
            r"Открыл C:\Users\me\Documents\отчёт.docx и (скрыто).",
        ),
        (r"Не удалось: C:\Windows\System32\cmd.exe", "Не удалось: (скрыто)"),
        ("C:/Users/me/AppData/Local/Google", HIDDEN),
        (r"Папка C:\JarvisData\logs", "Папка (скрыто)"),
        ("строка 1\nC:\\Users\\me\\.ssh\\config\nстрока 3", "строка 1\n(скрыто)\nстрока 3"),
        (r"(C:\Program Files (x86)\App)", "((скрыто))"),
        (r"Путь C:\Users\me\AppData\Roaming и ещё", "Путь (скрыто) и ещё"),
        (r'"C:\Users\me\.aws\credentials"', '"(скрыто)"'),
        (
            r"Сохранил в C:\Users\me\Documents\Мой отчёт за год.docx",
            r"Сохранил в C:\Users\me\Documents\Мой отчёт за год.docx",
        ),
        ("см. http://example.com/a и https://x.y/z", "см. http://example.com/a и https://x.y/z"),
        ("Привет, всё хорошо", "Привет, всё хорошо"),
        ("Диск C:\\ свободен", "Диск C:\\ свободен"),
        ("", ""),
    ],
)
def test_redact_text(text: str, expected: str) -> None:
    assert privacy.redact_text(text) == expected


def test_redact_text_private_names(private) -> None:
    private(r"D:\Тайное", r"E:\Проекты\Секретный проект")
    assert privacy.redact_text("Нашёл ТАЙНОЕ письмо") == "Нашёл (скрыто) письмо"
    assert privacy.redact_text(r"Открыл D:\Тайное\a.txt") == "Открыл (скрыто)"
    assert privacy.redact_text("про секретный проект") == "про (скрыто)"
    assert privacy.redact_text("Ошибка: нет файла") == "Ошибка: нет файла"


# --- redact -------------------------------------------------------------------------------------


def test_redact_nested() -> None:
    value = {
        "text": r"Нет доступа: C:\Users\me\.ssh\id_rsa",
        "items": [r"C:\JarvisData\x.txt", (r"C:\Users\me\Documents\a.txt", 5, None, True, 1.5)],
        r"C:\Users\me\.ssh": {"deep": [r"C:\Users\me\AppData\Local\x"]},
        "title": "окно\nс переводом",
        "count": 3,
    }
    original = json.dumps(value, ensure_ascii=False, default=list)
    result = privacy.redact(value)
    assert result == {
        "text": "Нет доступа: (скрыто)",
        "items": [HIDDEN, (r"C:\Users\me\Documents\a.txt", 5, None, True, 1.5)],
        r"C:\Users\me\.ssh": {"deep": [HIDDEN]},  # ключи не трогаем
        "title": "окно с переводом",
        "count": 3,
    }
    assert isinstance(result["items"][1], tuple)
    assert json.dumps(value, ensure_ascii=False, default=list) == original  # вход не изменён


def test_redact_title_key_is_truncated() -> None:
    assert len(privacy.redact({"title": "x" * 200})["title"]) == 80


def test_redact_result() -> None:
    res = Result(False, r"Нет доступа: C:\Users\me\.ssh\id_rsa", {"path": r"C:\JarvisData\pipe.key"})
    assert privacy.redact(res) == Result(False, "Нет доступа: (скрыто)", {"path": HIDDEN})


def test_redact_scalars_untouched() -> None:
    for value in (None, 0, 1.5, True, b"C:\\Windows"):
        assert privacy.redact(value) == value
