"""Платформа Windows 10/11: звук (pycaw), медиа (WinRT), ввод (SendInput), программы, окна, система.

Импорт пакета работает на любой ОС: Windows-библиотеки (ctypes.WinDLL, pywin32, pycaw, comtypes, winrt, mss)
подключаются лениво, внутри функций. Чистые части (разбор клавиш, поиск программ, пересчёт координат,
разбор вывода PowerShell) проверяются тестами на Linux — tests/platform.

Модули: winapi — прототипы Win32; keys — имена клавиш и сочетания; inputs — SendInput; audio — громкость;
media — «сейчас играет»; apps — инвентарь программ и разбор цели запуска; procs — процессы; windowing — окна;
screen — снимки; system — буфер обмена, питание, яркость, корзина, нагрузка, недавние файлы;
winplatform — WindowsPlatform, собирающая всё вместе.
"""

from jarvis.platform.windows.winplatform import WindowsPlatform

__all__ = ["WindowsPlatform"]
