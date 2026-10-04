"""Словарь L0: глаголы команд, имена известных папок, известные имена файлов, расширения (ADR 0026).

Только данные. Правило точности: в словарь попадает форма, которая однозначно означает команду; всё,
что может быть вопросом или просьбой о другом, остаётся агенту. Названий конкретных приложений здесь
нет: их знает инвентарь (адаптер), а ядро знает только понятие «браузер по умолчанию».
"""

from jarvis.domain.inventory import KnownFolder

# Глаголы. Повелительное наклонение и инфинитив («открыть браузер»), английские формы.
LAUNCH_VERBS = (
    "открой", "открыть", "откройте", "запусти", "запустить", "запустите", "включи", "включить",
    "стартуй", "open", "launch", "start", "run",
)  # fmt: skip
OPEN_VERBS = ("открой", "открыть", "откройте", "open")
URL_VERBS = (
    "открой", "открыть", "откройте", "перейди на", "перейти на", "зайди на", "зайти на", "open", "go to",
    "navigate to", "browse to", "visit",
)  # fmt: skip
SHOW_VERBS = (
    "покажи", "показать", "покажите", "выведи", "вывести", "перечисли", "отобрази", "show", "list", "display",
)  # fmt: skip
FIND_VERBS = (
    "найди", "найти", "найдите", "поищи", "отыщи", "где лежит", "где находится", "find", "locate",
    "search for", "where is",
)  # fmt: skip

# Слова, которые можно поставить перед именем: «запусти приложение Telegram», «открой папку загрузки».
APP_WORDS = ("приложение", "программу", "программа", "app", "the app", "application", "the")
FOLDER_WORDS = (
    "папку",
    "папка",
    "каталог",
    "директорию",
    "folder",
    "the folder",
    "directory",
    "the directory",
)
URL_WORDS = (
    "сайт",
    "страницу",
    "ссылку",
    "адрес",
    "the site",
    "site",
    "website",
    "the website",
    "page",
    "url",
)

# «Браузер» — понятие, а не программа: это браузер по умолчанию из инвентаря.
BROWSER_WORDS = frozenset(
    {"браузер", "browser", "веб браузер", "web browser", "интернет браузер", "the browser"}
)

# Известные папки: формы, после которых папка однозначна. Ключи — нормализованные (см. router.norm).
FOLDER_ALIASES: dict[str, KnownFolder] = {
    "загрузки": KnownFolder.DOWNLOADS,
    "загрузках": KnownFolder.DOWNLOADS,
    "загрузок": KnownFolder.DOWNLOADS,
    "downloads": KnownFolder.DOWNLOADS,
    "the downloads folder": KnownFolder.DOWNLOADS,
    "my downloads": KnownFolder.DOWNLOADS,
    "документы": KnownFolder.DOCUMENTS,
    "документах": KnownFolder.DOCUMENTS,
    "мои документы": KnownFolder.DOCUMENTS,
    "documents": KnownFolder.DOCUMENTS,
    "my documents": KnownFolder.DOCUMENTS,
    "рабочий стол": KnownFolder.DESKTOP,
    "рабочем столе": KnownFolder.DESKTOP,
    "desktop": KnownFolder.DESKTOP,
    "the desktop": KnownFolder.DESKTOP,
    "изображения": KnownFolder.PICTURES,
    "изображениях": KnownFolder.PICTURES,
    "картинки": KnownFolder.PICTURES,
    "pictures": KnownFolder.PICTURES,
    "музыка": KnownFolder.MUSIC,
    "музыку": KnownFolder.MUSIC,
    "music": KnownFolder.MUSIC,
    "видео": KnownFolder.VIDEOS,
    "videos": KnownFolder.VIDEOS,
    "домашнюю папку": KnownFolder.HOME,
    "домашняя папка": KnownFolder.HOME,
    "домашней папке": KnownFolder.HOME,
    "home folder": KnownFolder.HOME,
    "the home folder": KnownFolder.HOME,
    "my home folder": KnownFolder.HOME,
    "проводник": KnownFolder.HOME,  # «открой проводник» — проводник в домашней папке
    "explorer": KnownFolder.HOME,
    "file explorer": KnownFolder.HOME,
}
# Без слова «папку» открываются и показываются только папки, имя которых не значит ничего другого:
# «открой музыку» или «покажи видео» — скорее про плеер, это решает агент.
UNAMBIGUOUS_FOLDERS = frozenset(
    {KnownFolder.DOWNLOADS, KnownFolder.DOCUMENTS, KnownFolder.DESKTOP, KnownFolder.HOME}
)

# Рабочая папка задачи.
CURRENT_FOLDER = frozenset(
    {
        "эту папку", "этой папке", "этой папки", "текущую папку", "текущей папке", "текущая папка",
        "рабочую папку", "рабочей папке", "здесь", "тут", "this folder", "the current folder",
        "current folder", "here", "this directory", "the current directory", "current directory",
    }
)  # fmt: skip

# Имена файлов, которые ищут без расширения: «найди README».
KNOWN_FILE_NAMES = frozenset(
    {
        "readme", "license", "licence", "changelog", "contributing", "makefile", "dockerfile", "procfile",
        "gemfile", "authors", "notice", "security", "codeowners", "vagrantfile", "justfile",
    }
)  # fmt: skip

# Расширения для «найди все pdf».
SEARCH_EXTENSIONS = frozenset(
    {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "json", "yaml", "yml", "toml",
        "ini", "log", "py", "js", "ts", "jpg", "jpeg", "png", "gif", "svg", "mp3", "mp4", "zip", "7z", "rar",
        "iso", "msi", "exe",
    }
)  # fmt: skip

# Разговорные имена процессов: «покажи процессы питон».
PROCESS_ALIASES = {"питон": "python", "пайтон": "python", "хром": "chrome", "нода": "node"}
# Слова, которые не являются именем процесса в «покажи процессы …».
PROCESS_STOP_WORDS = frozenset(
    {
        "все", "всех", "мои", "запущенные", "активные", "работающие", "сейчас", "the", "all", "running",
        "active", "my", "that", "which", "которые", "что", "с", "и", "and", "of",
    }
)  # fmt: skip

# Обращение и вежливость в начале и конце команды не меняют смысла.
POLITE_PREFIXES = ("джарвис", "jarvis", "эй джарвис", "hey jarvis", "пожалуйста", "please", "плиз", "ну")
POLITE_SUFFIXES = ("пожалуйста", "please", "плиз", "плз")
