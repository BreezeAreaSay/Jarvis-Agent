# Интерфейс Jarvis: правила и устройство

Интерфейс — `desktop/`: Electron (окна, трей, хоткеи, запуск ядра) + React 19 + TypeScript 7 + Vite 8.
Образец — «Luxify Assistant»: тёмно-синее «стекло», акцент — цвет темы, живая сфера, крупные заголовки
страниц ЗАГЛАВНЫМИ («НАСТРОЙКИ», «НОВАЯ КОМАНДА»), карточки со строками-настройками, синие таблетки-вкладки.

## Структура

```
desktop/
  electron/            main.ts (окна, трей, ядро), preload.ts (мост window.jarvis)
  src/
    main.tsx           вход: главное окно, оверлей (#/overlay) или аватар (#/avatar)
    app/Shell.tsx      каркас: боковая панель, кнопки окна, страница по маршруту
    api/               client.ts (HTTP к ядру), events.ts (WebSocket: onEvent("voice.*", cb)), types.ts
    store/             zustand: app.ts (настройки, голос, история, подтверждения), desks.ts
    lib/               bridge.ts (Electron), router.ts (navigate/useRoute), theme.ts, levels.ts
    ui/index.tsx       базовые компоненты: Button, IconButton, Toggle, Slider, Select, Tabs, Check,
                       TextInput, Setting, Card, Modal, Empty, Dots, notify/notifyError, timeAgo
    components/        Orb (сфера: боковая панель, профиль), JarvisOrb (сфера из частиц: чат, аватар), LogoMark, AppIcon
                       (значки программ по ключу), PageHeader
    pages/<раздел>/    страницы: home, commands, extensions, ai, background («Фоновые задачи» и панель «Браузер
                       агента»), system («Системные операции»), settings, profile, onboarding
    widgets/           виджеты столов: common.tsx (рамка, useWidgetData), core.tsx, extra.tsx, registry.tsx
    overlay/           окно поверх всего: тосты, подтверждения, план агента, субтитры, питомец
    avatar/            аватар на рабочем столе: переливающаяся сфера — только пока Jarvis слушает, думает или
                       говорит (окно прячет главный процесс по avatar:active); щелчок — «стоп»
    styles/            tokens.css, base.css, components.css, shell.css, widgets.css (+ свой файл раздела)
```

## Как писать страницы

```tsx
<div className="page">
  <PageHeader title="Настройки" actions={<Button variant="primary">Сохранить</Button>}>
    {/* вкладки раздела */}
  </PageHeader>
  <div className="page-body">…</div>
</div>
```

- Только готовые компоненты из `@/ui` и классы из `components.css` (`card`, `setting`, `tabs`/`tab`, `chip`,
  `btn-*`, `icon-btn`, `picker-item`, `radio-card`, `swatch`, `inset`, `empty`, `status-dot`, `kbd`). Нужен новый
  общий компонент — сделай его в своей папке.
- Значки: `lucide-react` (линейные), программы и бренды — `<AppIcon name="spotify" size={36} />`.
- Цвета только через переменные (`var(--accent)`, `var(--text-2)`, `rgba(var(--accent-rgb), .2)`), никаких
  жёстких синих — цвет темы меняется в настройках.
- Тексты — по-русски, коротко и по-человечески («Скажите «Джарвис»», «Ничего не играет»). Обращение к
  пользователю на «вы».
- Каждый экран переживает пустые данные, загрузку и ошибку ядра (ошибки — `notifyError(e)`).
- Свои стили — `src/styles/<раздел>.css`, импорт из компонента страницы.
- Данные: `api.get/post/patch/put/del` и события `onEvent(...)` (контракт — docs/CONTRACTS.md).
- Анимации короткие (`var(--t-fast|med|slow)`, `var(--ease)`), с выключателем «Анимации» (`data-anim="off"`).

## Проверка вживую (без Windows)

```
cd core && JARVIS_DATA_DIR=<папка> .venv/bin/python -m jarvis --platform fake --token devtoken --port <порт>
cd desktop && node_modules/.bin/vite --port <порт> --strictPort
# браузер: http://127.0.0.1:<порт vite>/?core=http://127.0.0.1:<порт ядра>&token=devtoken#/settings
```

Платформа `fake` ничего не делает с системой и отдаёт правдоподобные данные. Снимок экрана — Playwright
(Chromium: `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`).
