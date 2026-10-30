# Словарь ударений и «ё» Jarvis — лицензии

Эта папка и словарь `ru_dict`, который из неё собирается, распространяются **не** по лицензии PolyForm
Noncommercial остального Jarvis, а по своим лицензиям — их можно использовать где угодно, в том числе
в коммерческих проектах, на условиях ниже.

| Что | Лицензия |
|---|---|
| `build_ru_dict.py` — сборка словаря | [GNU GPL 3.0 или более поздняя](https://www.gnu.org/licenses/gpl-3.0.html) |
| `stress.tsv` — ударения | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) |
| `manual.txt` — ручные правки | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) |
| `ru_dict` — собранный словарь espeak-ng (в установщике Jarvis — `resources/core/app/jarvis/resources/voice`) | [GNU GPL 3.0 или более поздняя](https://www.gnu.org/licenses/gpl-3.0.html) |

## Откуда данные

- **espeak-ng** — исходники русского словаря (`ru_rules`, `ru_list`, `ru_listx`, `ru_emoji`) из форка
  [csukuangfj/espeak-ng@ed530aa](https://github.com/csukuangfj/espeak-ng/tree/ed530aa113046142eb5115cf2fc9157854d0ffe1),
  с которым собран sherpa-onnx; © авторы espeak-ng, GPL-3.0-or-later. Скрипт скачивает их при сборке
  (SHA-256 закреплены в скрипте) и добавляет правила для знака ударения U+0301.
- **OpenRussian** — [russian-dictionary @50e210c](https://github.com/Badestrand/russian-dictionary),
  © OpenRussian.org, CC BY-SA 4.0. `stress.tsv` — производная работа: формы слов, где ударение OpenRussian
  совпадает с [Silero Stress 1.5](https://github.com/snakers4/silero-stress) (MIT), а espeak-ng ставит иначе.
  Атрибуция: «OpenRussian.org, CC BY-SA 4.0; выборка и проверка — Jarvis».
- **eyo-kernel** — [hcodes/eyo-kernel 4.1.3](https://github.com/hcodes/eyo-kernel), `dictionary/safe.txt`,
  © Denis Seleznev, MIT: слова с бесспорной «ё». Скрипт скачивает файл при сборке (SHA-256 закреплён).
- **espeak-ng-data** — фонемные таблицы из релиза
  [sherpa-onnx tts-models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models), GPL-3.0-or-later;
  компилятор — `libespeak-ng` из колеса [espeakng-loader 0.2.4](https://pypi.org/project/espeakng-loader/).

## Исходный код словаря

Всё, из чего собран `ru_dict`, — в этой папке и по закреплённым ссылкам в `build_ru_dict.py`:

    python scripts/ru_dict/build_ru_dict.py --out build/voice

Сборка повторяема: те же исходники дают тот же файл (SHA-256 — в `ru_dict.json` рядом со словарём).
