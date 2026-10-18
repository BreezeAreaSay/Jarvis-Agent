// Сквозной для мыши оверлей. Окно по умолчанию игнорирует мышь (клики уходят программам под ним). Где указатель,
// окно узнаёт двумя путями: Electron пересылает движения (forward: true), а главный процесс ещё и сам опрашивает
// курсор (overlay:pointer) — пересылка держится на хуке мыши, который Windows может молча снять. По положению
// решаем: указатель над интерактивным элементом ([data-interactive]) или над непрозрачным пикселем питомца — окно
// ловит мышь, иначе снова пропускает. Пересчёт — на каждое движение, при изменении DOM (карточка исчезла под
// неподвижным указателем) и раз в 0,6 с, пока окно ловит мышь: зависнуть в «перехвате» и заблокировать клики по
// программам нельзя.
import { useEffect } from "react";
import { bridge } from "@/lib/bridge";

type HitTest = (x: number, y: number) => boolean;

const hitTests = new Set<HitTest>();
let ignoring = true;
let holds = 0;
let px = -1;
let py = -1;
let frame = 0;

function interactiveAt(x: number, y: number): boolean {
  if (x < 0 || y < 0) return false;
  if (document.elementFromPoint(x, y)?.closest("[data-interactive]")) return true;
  for (const test of hitTests) if (test(x, y)) return true;
  return false;
}

function apply() {
  frame = 0;
  const capture = holds > 0 || interactiveAt(px, py);
  if (capture === !ignoring) return;
  ignoring = !capture;
  bridge.setIgnoreMouse(ignoring);
}

/** Пересчитать в ближайшем кадре (что-то сдвинулось под указателем). */
export function refreshMouse() {
  if (!frame) frame = requestAnimationFrame(apply);
}

/** Своя проверка попадания (питомец — по маске пикселей). */
export function addHitTest(test: HitTest): () => void {
  hitTests.add(test);
  return () => {
    hitTests.delete(test);
    refreshMouse();
  };
}

/** Держать мышь, пока идёт перетаскивание; вернёт «отпустить». */
export function holdMouse(): () => void {
  holds++;
  apply();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    holds = Math.max(0, holds - 1);
    refreshMouse();
  };
}

/** Последнее известное положение указателя (CSS-пиксели окна) или null. */
export function pointerPos(): { x: number; y: number } | null {
  return px < 0 ? null : { x: px, y: py };
}

export function useClickThrough() {
  useEffect(() => {
    ignoring = true;
    bridge.setIgnoreMouse(true);
    const move = (e: MouseEvent) => {
      px = e.clientX;
      py = e.clientY;
      apply();
    };
    const out = (e: MouseEvent) => {
      if (e.relatedTarget) return; // ушли за пределы окна
      px = py = -1;
      apply();
    };
    window.addEventListener("mousemove", move, { capture: true, passive: true });
    window.addEventListener("pointermove", move, { capture: true, passive: true });
    // опрос главного процесса: точка в CSS-пикселях окна, (-1, -1) — указатель вне оверлея
    const offPointer = bridge.on("overlay:pointer", (x, y) => {
      px = Number(x);
      py = Number(y);
      if (!Number.isFinite(px) || !Number.isFinite(py)) px = py = -1;
      apply();
    });
    document.addEventListener("mouseout", out);
    window.addEventListener("blur", refreshMouse);
    const observer = new MutationObserver(refreshMouse);
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["data-interactive", "class"],
    });
    const timer = window.setInterval(() => {
      if (!ignoring || holds) apply();
    }, 600);
    return () => {
      offPointer();
      window.removeEventListener("mousemove", move, { capture: true });
      window.removeEventListener("pointermove", move, { capture: true });
      document.removeEventListener("mouseout", out);
      window.removeEventListener("blur", refreshMouse);
      observer.disconnect();
      clearInterval(timer);
      cancelAnimationFrame(frame);
      frame = 0;
      ignoring = true;
      bridge.setIgnoreMouse(true);
    };
  }, []);
}
