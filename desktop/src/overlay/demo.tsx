// Демо-режим оверлея (?demo=1 или ?demo=<сцена>): подставные события, чтобы посмотреть оверлей в обычном
// браузере и снять скриншоты. Только в демо у страницы тёмный фон и «окно» «Документы» для питомца.
// Сцены: all, voice, confirm, agent, agent-run, plugins, pet, pet-window, pet-dance, pet-eat, pet-sleep,
// pet-cat, pet-ghost, clipboard (палитра «Умного буфера»), clipboard-result (карточка итога ИИ),
// focus (таймер «Фокуса», «Telegram подождёт…», через 5 с — «Время перерыва»), files (карточка «Поиска по ПК»
// с номерами), files-box (быстрый поиск по горячей клавише).
import { FileText } from "lucide-react";
import { useEffect, useRef } from "react";
import type { Confirmation } from "@/api/types";
import { levels } from "@/lib/levels";
import { useApp } from "@/store/app";
import { emitOverlay } from "./bus";
import { ClipboardDemoDoc, clipboardDemo } from "./ClipboardOverlay";
import { fileSearchDemo } from "./FileSearch";
import { focusDemo } from "./Focus";
import type { Bounds } from "./common";

const SENTENCES = [
  "Глава третья. Утро выдалось тихим,",
  "и город ещё спал, когда поезд",
  "медленно подошёл к перрону.",
  "На платформе не было ни души.",
];

function confirmations(scene: string): Confirmation[] {
  const now = Date.now() / 1000;
  const list: Confirmation[] = [];
  if (scene === "all" || scene === "confirm") {
    list.push({
      id: "demo-confirm",
      title: "Очистить корзину?",
      detail: "Файлов: 12 — их нельзя будет восстановить",
      kind: "plugin",
      source: "voice",
      created: now,
      timeout: 120,
    });
  }
  if (scene === "agent") {
    list.push({
      id: "demo-agent",
      title: "Нажать «Отправить» в «Почта»?",
      detail: "Будет отправлено: «Отчёт готов, пришлю в пятницу.»",
      kind: "agent",
      source: "agent",
      created: now,
      timeout: 120,
    });
  }
  return list;
}

/** Запускает сцену; вернёт остановку. windowRect — «окно» «Документы» для питомца (CSS-пиксели). */
export function startDemo(scene: string, windowRect: () => DOMRect | null, bounds: () => Bounds): () => void {
  const timers: number[] = [];
  const at = (ms: number, fn: () => void) => timers.push(window.setTimeout(fn, ms));
  const every = (ms: number, fn: () => void) => {
    fn();
    timers.push(window.setInterval(fn, ms));
  };
  const has = (...names: string[]) => names.includes(scene) || scene === "all";
  // ручная проверка из консоли: __jarvisOverlay.emit("voice.partial", {text: "…", wake: true})
  const debug = window as unknown as { __jarvisOverlay?: { emit: typeof emitOverlay } };
  debug.__jarvisOverlay = { emit: emitOverlay };

  // «живой» микрофон
  const t0 = performance.now();
  timers.push(
    window.setInterval(() => {
      const t = (performance.now() - t0) / 1000;
      levels.mic = Math.max(0, 0.18 + 0.5 * Math.abs(Math.sin(t * 2.3)) * (0.6 + 0.4 * Math.sin(t * 7.1)));
    }, 50),
  );

  // подтверждения: ядро при подключении перечитывает список — возвращаем свои ещё пару раз
  const wanted = confirmations(scene);
  const original = useApp.getState().resolveConfirmation;
  if (wanted.length) {
    // ответ человека запоминаем, чтобы не вернуть уже решённую карточку
    const resolved = new Set<string>();
    useApp.setState({
      resolveConfirmation: (id, allow) => {
        resolved.add(id);
        if (id.startsWith("demo-")) {
          useApp.setState({ confirmations: useApp.getState().confirmations.filter((c) => c.id !== id) });
          return Promise.resolve();
        }
        return original(id, allow);
      },
    });
    const ensure = () => {
      const cur = useApp.getState().confirmations;
      const missing = wanted.filter((c) => !cur.some((x) => x.id === c.id) && !resolved.has(c.id));
      if (missing.length) useApp.setState({ confirmations: [...cur, ...missing] });
    };
    for (const ms of [0, 400, 1200, 2500, 5000]) at(ms, ensure);
  }

  if (scene === "voice") {
    // как в ролике: «Джарвис включи музы…» → «…музыку» → выполнено → тост
    const cycle = () => {
      emitOverlay("voice.wake", {});
      emitOverlay("voice.state", { state: "listening" });
      const words = ["Джарвис", "Джарвис включи", "Джарвис включи музы", "Джарвис включи музыку"];
      words.forEach((w, i) => at(250 + i * 380, () => emitOverlay("voice.partial", { text: w, wake: true })));
      at(2600, () => emitOverlay("voice.state", { state: "processing" }));
      at(2700, () => emitOverlay("voice.final", { text: "Джарвис, включи музыку", wake: true, command: "включи музыку" }));
      at(3300, () => emitOverlay("toast", { title: "Включить музыку", message: "Выполнено", status: "ok", icon: "spotify", ts: Date.now() / 1000 }));
      at(3350, () => emitOverlay("dispatch.done", { text: "включи музыку", kind: "command", ok: true }));
      at(3600, () => emitOverlay("voice.state", { state: "idle" }));
    };
    at(200, cycle);
    timers.push(window.setInterval(cycle, 9000));
  } else if (has("all")) {
    at(150, () => {
      emitOverlay("voice.wake", {});
      emitOverlay("voice.state", { state: "listening" });
    });
    every(5000, () => emitOverlay("voice.partial", { text: "Джарвис включи музыку", wake: true }));
  }

  if (has("all", "confirm")) {
    at(300, () => emitOverlay("toast", { title: "Включить музыку", message: "Выполнено", status: "ok", icon: "spotify", ts: 0 }));
    at(500, () => emitOverlay("toast", { title: "Не понял команду", message: "сделай мне кофе", status: "error", icon: "unknown", ts: 0 }));
    at(700, () => emitOverlay("reminder.fired", { item: { id: "r1", text: "Позвонить маме", at: Date.now() / 1000, repeat: null, done: false } }));
  }

  if (has("all", "agent-run")) {
    at(200, () => {
      emitOverlay("agent.state", { run_id: "demo", task: "Отправь Максиму отчёт", status: "running", step: 2, total: 4, text: "Нажимаю «Написать»" });
      emitOverlay("agent.step", { run_id: "demo", index: 1, title: "Открываю «Почта»", status: "ok" });
      emitOverlay("agent.step", { run_id: "demo", index: 2, title: "Смотрю, что на экране", status: "ok" });
      emitOverlay("agent.step", { run_id: "demo", index: 3, title: "Нажимаю «Написать»", status: "running" });
    });
  }
  if (scene === "agent") {
    at(200, () => {
      emitOverlay("agent.state", {
        run_id: "demo",
        task: "Отправь Максиму отчёт",
        status: "waiting",
        step: 4,
        total: 4,
        text: "Жду вашего решения: Нажать «Отправить» в «Почта»?",
      });
    });
  }

  if (has("plugins")) {
    every(4000, () =>
      emitOverlay("plugin.subtitles.text", { id: 7, text: "Welcome back, everyone!", translation: "С возвращением, друзья!", lang: "en", pending: false }),
    );
    at(200, () => emitOverlay("plugin.dictation.state", { state: "listening", text: "" }));
    at(250, () => emitOverlay("plugin.summary.state", { state: "recording", seconds: 192 }));
    at(300, () =>
      emitOverlay("plugin.reader.progress", {
        state: "playing",
        index: 1,
        total: 48,
        sentence: SENTENCES[1],
        title: "Утренний поезд",
        sentences: [...SENTENCES, ...Array.from({ length: 44 }, (_, i) => `Фраза ${i + 5}.`)],
      }),
    );
  }

  // мир питомца: «окно» «Документы», музыка в танцевальной сцене, полная Корзина
  if (has("pet", "pet-window", "pet-dance", "pet-eat", "pet-sleep", "pet-cat", "pet-ghost")) {
    every(2000, () => {
      const r = windowRect();
      const b = bounds();
      const k = b.scale;
      const windows = r
        ? [{ id: 4242, rect: [(r.left + b.x) * k, (r.top + b.y) * k, (r.right + b.x) * k, (r.bottom + b.y) * k], foreground: true }]
        : [];
      emitOverlay("plugin.pet.state", {
        playing: scene === "pet-dance",
        recycle_count: 12,
        windows,
        fullscreen: false,
        skin: scene === "pet-cat" ? "cat" : scene === "pet-ghost" ? "ghost" : "jelly",
        name: scene === "pet-cat" ? "Барсик" : scene === "pet-ghost" ? "Бу" : "Люкси",
        size: 64,
        eats_trash: true,
      });
    });
  }

  if (scene.startsWith("clipboard")) clipboardDemo(scene, at);
  if (scene === "focus") focusDemo(at);
  if (scene.startsWith("files")) fileSearchDemo(scene, at);

  return () => {
    for (const t of timers) {
      clearTimeout(t);
      clearInterval(t);
    }
    if (wanted.length) useApp.setState({ resolveConfirmation: original });
    delete debug.__jarvisOverlay;
  };
}

/** Тёмный «рабочий стол» демо-режима и окно «Документы», на котором сидит питомец. */
export function DemoBackdrop({ scene, windowRef }: { scene: string; windowRef: React.RefObject<HTMLDivElement | null> }) {
  const showWindow = scene.startsWith("pet") || scene === "all";
  const style = scene === "all" ? { left: "4%", top: "44%" } : { left: "44%", top: "50%" };
  return (
    <div className="ov-demo-bg" aria-hidden>
      {showWindow && (
        <div className="ov-demo-window" ref={windowRef} style={style}>
          <div className="ov-demo-window-head">
            <FileText /> Документы
          </div>
          <i />
          <i />
          <i />
          <i />
        </div>
      )}
      {scene.startsWith("clipboard") && <ClipboardDemoDoc />}
    </div>
  );
}

/** Хук: запустить сцену один раз (StrictMode вызывает эффекты дважды — повтор безопасен). */
export function useDemo(scene: string | null, windowRef: React.RefObject<HTMLDivElement | null>, bounds: Bounds) {
  const b = useRef(bounds);
  b.current = bounds;
  useEffect(() => {
    if (!scene) return;
    return startDemo(scene, () => windowRef.current?.getBoundingClientRect() ?? null, () => b.current);
  }, [scene, windowRef]);
}
