// Питомец поверх экрана (плагин «Питомец»). Ядро раз в ~2 с присылает его «мир» (plugin.pet.state: окна,
// музыка, Корзина), реакции — plugin.pet.action (munch, eat, sad, refuse, pet, say). Рисуем пиксель-арт на
// маленьком холсте, который ездит за питомцем; кадры — ~30 в секунду (во сне реже), и только пока питомец
// виден. Мышь ловится лишь над его непрозрачными пикселями: щелчок — меню, перетаскивание — переносит.
import { Cookie, EyeOff, Hand, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { MediaInfo, PluginInfo } from "@/api/types";
import { useApp } from "@/store/app";
import { cx } from "@/ui";
import { useOverlayEvent } from "../bus";
import { addHitTest, holdMouse, pointerPos, refreshMouse } from "../clickthrough";
import { type Bounds, DEMO, Fade, pluginAction, useBounds, useUiZoom } from "../common";
import { type Bubble, type Env, PetBrain, type Platform } from "./brain";
import { Pixels } from "./pixels";
import { CANVAS_H, CANVAS_W, drawBang, drawParticles, FEET_Y, SKINS, type SkinId, SPRITE_H, SPRITE_W, SPRITE_X } from "./sprites";

export interface PetWindow {
  id: number;
  rect: number[];
  foreground?: boolean;
}

export interface PetWorld {
  playing?: boolean;
  recycle_count?: number;
  windows?: PetWindow[];
  fullscreen?: boolean;
  skin?: string;
  name?: string;
  size?: number;
  eats_trash?: boolean;
}

interface PetConfig {
  skin: SkinId;
  name: string;
  size: number;
  onWindows: boolean;
  dance: boolean;
  eats: boolean;
}

const LABEL_SPACE = 26; // под питомцем — подпись с именем
const TRASH_LEFT = 22;
const TRASH_SIZE = 52;

function readConfig(raw: Record<string, unknown> | undefined, world: PetWorld | null): PetConfig {
  const c = raw ?? {};
  const w = (world ?? {}) as unknown as Record<string, unknown>;
  const pick = <T,>(key: string, fallback: T): T => (c[key] ?? w[key] ?? fallback) as T;
  const skin = String(pick("skin", "jelly"));
  const size = Number(pick("size", 64));
  return {
    skin: skin === "cat" || skin === "ghost" ? skin : "jelly",
    name: String(pick("name", DEMO ? "Люкси" : "Медузка")).trim(),
    size: Number.isFinite(size) ? Math.max(32, Math.min(256, size)) : 64,
    onWindows: c.on_windows !== false,
    dance: c.dance_to_music !== false,
    eats: pick<unknown>("eats_trash", true) !== false,
  };
}

/** Видимые части верхних краёв окон (окна — в Z-порядке сверху вниз, прямоугольники — в физических пикселях). */
export function platformsFrom(windows: PetWindow[], b: Bounds, width: number, height: number, minWidth: number): Platform[] {
  const rects = windows
    .filter((w) => Array.isArray(w.rect) && w.rect.length >= 4)
    .map((w) => {
      const [l, t, r, bt] = w.rect.map(Number);
      return {
        id: Number(w.id) || 0,
        fg: !!w.foreground,
        l: l / b.scale - b.x,
        t: t / b.scale - b.y,
        r: r / b.scale - b.x,
        b: bt / b.scale - b.y,
      };
    });
  const out: Platform[] = [];
  rects.forEach((r, i) => {
    if (r.r - r.l < minWidth || r.t < 80 || r.t > height - 120) return;
    let segs: [number, number][] = [[Math.max(r.l, 6), Math.min(r.r, width - 6)]];
    for (let j = 0; j < i && segs.length; j++) {
      const o = rects[j];
      if (o.t > r.t + 2 || o.b < r.t - 2) continue;
      segs = segs.flatMap(([a, z]): [number, number][] => {
        if (o.r <= a || o.l >= z) return [[a, z]];
        const parts: [number, number][] = [];
        if (o.l > a) parts.push([a, o.l]);
        if (o.r < z) parts.push([o.r, z]);
        return parts;
      });
    }
    segs.forEach(([a, z], k) => {
      if (z - a >= minWidth) out.push({ key: `${r.id}:${k}`, win: r.id, left: r.l, x1: a, x2: z, y: Math.round(r.t), fg: r.fg });
    });
  });
  return out;
}

/** Демо-сцены питомца для снимков: фиксированная поза вместо прогулок. Вернёт true, когда применена. */
function applyDemo(b: PetBrain, scene: string, env: Env): boolean {
  const plat = env.platforms[0];
  const m = 16 * env.cs + 6;
  switch (scene) {
    case "pet":
    case "pet-cat":
    case "pet-ghost":
      b.x = env.width * 0.3;
      b.y = env.floor;
      b.frozen = true;
      b.setMode("idle", 1e9);
      b.idleFace = "happy";
      return true;
    case "pet-window":
    case "pet-dance":
      if (!plat || (scene === "pet-dance" && !b.music)) return false;
      b.surface = plat;
      b.x = plat.x1 + (plat.x2 - plat.x1) * 0.62;
      b.y = plat.y;
      b.frozen = true;
      if (scene === "pet-dance") {
        b.danceUntil = 1e9;
        b.setMode("dance", 0);
      } else {
        b.setMode("idle", 1e9);
        b.idleFace = "open";
      }
      return true;
    case "pet-eat":
      b.x = env.trashRight + 10 + m;
      b.y = env.floor;
      b.facing = -1;
      b.trash = true;
      b.frozen = true;
      b.bubble = { kind: "ask", text: "Можно съесть файлы из корзины?", until: 1e9 };
      b.setMode("eatask", 1e9);
      return true;
    case "pet-sleep":
      b.x = env.width * 0.4;
      b.y = env.floor;
      b.frozen = true;
      b.setMode("sleep", 0);
      return true;
    default:
      return true;
  }
}

interface Ui {
  bubble: Bubble | null;
  trash: boolean;
  shake: boolean;
  label: boolean;
  gone: boolean;
}

function PetView({
  cfg,
  world,
  paused,
  onGone,
}: {
  cfg: PetConfig;
  world: PetWorld | null;
  paused: boolean;
  onGone: () => void;
}) {
  const bounds = useBounds();
  const zoom = useUiZoom();
  const [view, setView] = useState({ w: window.innerWidth, h: window.innerHeight });
  const brain = useMemo(() => new PetBrain(), []);
  const px = useMemo(() => new Pixels(CANVAS_W, CANVAS_H), []);
  const anchorRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [ui, setUi] = useState<Ui>({ bubble: null, trash: false, shake: false, label: true, gone: false });
  const [menu, setMenu] = useState(false);
  // музыка и Корзина меняются событиями — читаем их в каждом кадре, а не при отрисовке React
  const music = useRef(!!world?.playing);
  const recycle = useRef<number | null>(typeof world?.recycle_count === "number" ? world.recycle_count : null);
  const demoPending = useRef(!!DEMO && DEMO !== "all");

  useEffect(() => {
    const resize = () => setView({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);

  // масштаб: «Размер, пикселей» 64 → ×3; целое число физических пикселей на арт-пиксель — без мыла
  const dpr = window.devicePixelRatio || 1;
  const P = Math.max(1, Math.round(((cfg.size * 3) / 64) * dpr));
  const cs = P / dpr;
  const skin = SKINS[cfg.skin];

  const platforms = useMemo(
    () => (cfg.onWindows ? platformsFrom(world?.windows ?? [], bounds, view.w, view.h, (SPRITE_W - 6) * cs + 24) : []),
    [world?.windows, bounds, view.w, view.h, cs, cfg.onWindows],
  );

  const env = useRef<Env>(null as unknown as Env);
  env.current = {
    width: view.w,
    height: view.h,
    floor: view.h - LABEL_SPACE,
    platforms,
    recycle: recycle.current,
    music: cfg.dance && music.current,
    cs,
    speed: skin.speed,
    hops: skin.hops,
    eats: cfg.eats,
    onWindows: cfg.onWindows,
    trashRight: TRASH_LEFT + TRASH_SIZE,
  };
  const skinRef = useRef(skin);
  skinRef.current = skin;

  // --- события ---------------------------------------------------------------------------------------------
  const dance = useRef(cfg.dance);
  dance.current = cfg.dance;
  useEffect(() => {
    if (!world) return;
    music.current = !!world.playing;
    recycle.current = typeof world.recycle_count === "number" ? world.recycle_count : null;
  }, [world]);
  useOverlayEvent<MediaInfo>("media.changed", (m) => {
    music.current = m?.status === "playing";
  });
  useOverlayEvent("voice.wake", () => brain.react());
  useOverlayEvent<{ action: string; text?: string; name?: string; count?: number }>("plugin.pet.action", (a) => {
    switch (a?.action) {
      case "munch":
        recycle.current = 0;
        brain.munch();
        break;
      case "eat":
        brain.munch(a.name || "файл");
        break;
      case "sad":
        brain.sad("Корзина пуста — кормить нечем");
        break;
      case "refuse":
        brain.sad("Ладно, не буду…");
        break;
      case "pet":
        brain.petted();
        break;
      case "say":
        if (a.text) brain.say(a.text);
        break;
    }
  });

  /** Покормить: ядро спросит подтверждение и ответит событием munch / refuse / sad. */
  const requestFeed = useRef(() => {
    void pluginAction("pet", "feed").then((r) => {
      if (r === null && !DEMO) brain.cancelEat();
    });
  }).current;
  useEffect(() => {
    brain.onFeed = requestFeed;
    return () => {
      brain.onFeed = null;
    };
  }, [brain, requestFeed]);

  // --- попадание мышью: только непрозрачные пиксели спрайта ----------------------------------------------------
  const hit = useRef((x: number, y: number) => {
    if (brain.gone || brain.x < 0) return false;
    const c = env.current.cs;
    const ax = Math.floor((x - (brain.x - (CANVAS_W / 2) * c)) / c);
    const ay = Math.floor((y - (brain.y - (FEET_Y + 1) * c)) / c);
    return px.hit(ax, ay, 1);
  }).current;

  useEffect(() => {
    if (paused) return;
    const off = addHitTest(hit);
    const down = (e: PointerEvent) => {
      // карточка поверх питомца (или его же облачко/меню) — щелчок ей, а не питомцу
      if (e.button !== 0 || (e.target as Element | null)?.closest?.("[data-interactive]")) return;
      if (!hit(e.clientX, e.clientY)) return;
      e.preventDefault();
      e.stopPropagation();
      const release = holdMouse();
      try {
        document.documentElement.setPointerCapture(e.pointerId);
      } catch {
        /* браузер без захвата указателя */
      }
      const sx = e.clientX;
      const sy = e.clientY;
      let dragging = false;
      const move = (ev: PointerEvent) => {
        if (!dragging && Math.hypot(ev.clientX - sx, ev.clientY - sy) > 5) {
          dragging = true;
          setMenu(false);
          brain.grabAt(sx, sy);
        }
        if (dragging) brain.dragTo(ev.clientX, ev.clientY, env.current);
      };
      const up = () => {
        window.removeEventListener("pointermove", move, true);
        window.removeEventListener("pointerup", up, true);
        window.removeEventListener("pointercancel", up, true);
        release();
        if (dragging) brain.release(env.current);
        else setMenu((m) => !m);
      };
      window.addEventListener("pointermove", move, true);
      window.addEventListener("pointerup", up, true);
      window.addEventListener("pointercancel", up, true);
    };
    window.addEventListener("pointerdown", down, true);
    return () => {
      off();
      window.removeEventListener("pointerdown", down, true);
    };
  }, [paused, brain, hit]);

  // меню: питомец замирает и смотрит на человека; закрывается само
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuHover, setMenuHover] = useState(false);
  useEffect(() => {
    if (brain.attention !== menu) brain.attend(menu);
    if (!menu || menuHover) return;
    const t = window.setTimeout(() => setMenu(false), 6000);
    const outside = (e: PointerEvent) => {
      if (!menuRef.current?.contains(e.target as Node) && !hit(e.clientX, e.clientY)) setMenu(false);
    };
    window.addEventListener("pointerdown", outside, true);
    return () => {
      clearTimeout(t);
      window.removeEventListener("pointerdown", outside, true);
    };
  }, [menu, menuHover, brain, hit]);

  useEffect(() => {
    if (ui.gone) onGone();
  }, [ui.gone, onGone]);

  // --- кадры ---------------------------------------------------------------------------------------------------
  useEffect(() => {
    if (paused) return;
    const canvas = canvasRef.current;
    const anchor = anchorRef.current;
    const ctx = canvas?.getContext("2d");
    const offscreen = document.createElement("canvas");
    offscreen.width = CANVAS_W;
    offscreen.height = CANVAS_H;
    const offCtx = offscreen.getContext("2d");
    if (!canvas || !anchor || !ctx || !offCtx) return;
    let raf = 0;
    let last = performance.now();
    let uiKey = "";
    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const e = env.current;
      e.music = dance.current && music.current;
      e.recycle = recycle.current;
      const fps = brain.mode === "sleep" ? 12 : brain.mode === "idle" && !brain.music && !brain.particles.length ? 20 : 30;
      if (now - last < 1000 / fps - 4) return;
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      brain.update(dt, e);
      if (demoPending.current && DEMO && applyDemo(brain, DEMO, e)) demoPending.current = false;

      px.clear();
      const pose = brain.pose();
      const top = FEET_Y - (SPRITE_H - 1) - Math.round(brain.hop);
      const drawn = skinRef.current.draw(px, SPRITE_X, top, pose);
      brain.head = { top: top + drawn.headTop, mouth: top + drawn.mouthY, center: top + drawn.centerY };
      drawParticles(px, brain.particles);
      if (brain.t < brain.bangUntil) drawBang(px, CANVAS_W / 2 + 7, brain.head.top - 12, Math.min(1, (brain.bangUntil - brain.t) / 0.25));
      offCtx.putImageData(px.image, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(offscreen, 0, 0, canvas.width, canvas.height);

      anchor.style.transform = `translate3d(${brain.x.toFixed(1)}px, ${brain.y.toFixed(1)}px, 0)`;
      // облачко и меню не уезжают за край экрана
      const half = 130;
      anchor.style.setProperty("--shift", `${(Math.max(half + 8, Math.min(e.width - half - 8, brain.x)) - brain.x).toFixed(0)}px`);

      const key = `${brain.bubble?.kind ?? ""}|${brain.bubble?.text ?? ""}|${brain.trash}|${brain.shaking}|${brain.label}|${brain.gone}`;
      if (key !== uiKey) {
        uiKey = key;
        setUi({ bubble: brain.bubble, trash: brain.trash, shake: brain.shaking, label: brain.label, gone: brain.gone });
      }
      const p = pointerPos();
      if (p && Math.abs(p.x - brain.x) < 200 && p.y > brain.y - 300 && p.y < brain.y + 60) refreshMouse();
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [paused, brain, px]);

  // --- действия меню -----------------------------------------------------------------------------------------------
  const petIt = () => {
    setMenu(false);
    brain.petted();
  };
  const feed = () => {
    setMenu(false);
    if (!cfg.eats) {
      brain.say("Мне пока нельзя есть файлы — это включается в настройках питомца", 4);
      return;
    }
    brain.startEat(true, env.current);
    requestFeed();
  };
  const hide = () => {
    setMenu(false);
    brain.poof();
  };

  const headPx = (SPRITE_H + 6) * cs;
  const canvasStyle = {
    width: CANVAS_W * cs,
    height: CANVAS_H * cs,
    left: -(CANVAS_W / 2) * cs,
    top: -(FEET_Y + 1) * cs,
  };
  const recycleCount = recycle.current;

  return (
    <div className={cx("ov-pet-layer", paused && "paused")} style={{ zoom: 1 / zoom }} aria-hidden={paused}>
      <Fade show={ui.trash && !paused} exitMs={320}>
        {(leaving) => (
          <button
            type="button"
            className={cx("ov-trash ov-anim-up", leaving && "leaving", ui.shake && "shake")}
            style={{ left: TRASH_LEFT, bottom: 8 }}
            data-interactive
            title="Покормить питомца файлами из Корзины"
            onClick={() => (brain.mode === "eatask" ? brain.answer(true) : undefined)}
          >
            <span className="ov-trash-tile" style={{ width: TRASH_SIZE, height: TRASH_SIZE }}>
              <Trash2 />
              {!!recycleCount && recycleCount > 0 && (
                <span className="ov-trash-count">{recycleCount > 99 ? "99+" : recycleCount}</span>
              )}
            </span>
            <span className="ov-trash-label">Корзина</span>
          </button>
        )}
      </Fade>

      <div className="ov-pet-anchor" ref={anchorRef}>
        <canvas
          ref={canvasRef}
          className="ov-pet-canvas"
          width={CANVAS_W * P}
          height={CANVAS_H * P}
          style={canvasStyle}
        />
        {ui.label && cfg.name && !menu && <div className="ov-pet-name">{cfg.name}</div>}
        {ui.bubble && !menu && (
          <div className="ov-pet-pop" style={{ bottom: headPx }}>
            <div
              key={ui.bubble.text}
              className={cx("ov-pet-bubble ov-anim-up", ui.bubble.kind === "ask" && "is-ask")}
              data-interactive={ui.bubble.kind === "ask" ? "" : undefined}
            >
              <div className="ov-pet-bubble-text">{ui.bubble.text}</div>
              {ui.bubble.kind === "ask" && (
                <div className="ov-pet-bubble-actions">
                  <button type="button" className="btn btn-sm" onClick={() => brain.answer(false)}>
                    Не надо
                  </button>
                  <button type="button" className="btn btn-sm btn-primary" onClick={() => brain.answer(true)}>
                    Можно
                  </button>
                </div>
              )}
            </div>
          </div>
        )}
        {menu && (
          <div className="ov-pet-pop" style={{ bottom: headPx }}>
            <div
              className="ov-pet-menu ov-anim-up"
              ref={menuRef}
              data-interactive
              onMouseEnter={() => setMenuHover(true)}
              onMouseLeave={() => setMenuHover(false)}
            >
              <button type="button" onClick={petIt}>
                <Hand /> Погладить
              </button>
              <button type="button" onClick={feed}>
                <Cookie /> Покормить
              </button>
              <button type="button" onClick={hide}>
                <EyeOff /> Спрятать до перезапуска
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** Слой питомца: есть, только если плагин «Питомец» включён (в демо-режиме — всегда). */
export function Pet({ hidden }: { hidden: boolean }) {
  const plugin = useApp((s) => s.settings?.plugins?.pet);
  const hideFullscreen = useApp((s) => s.settings?.avatar.hide_fullscreen ?? true);
  const enabled = !!DEMO || !!plugin?.enabled;
  const [world, setWorld] = useState<PetWorld | null>(null);
  const [away, setAway] = useState(false);
  const wasEnabled = useRef(enabled);

  useOverlayEvent<PetWorld>("plugin.pet.state", (w) => setWorld(w ?? null));
  // ядро шлёт мир только при изменениях — при подключении берём текущий из статуса плагина
  useOverlayEvent<{ items: PluginInfo[] }>("plugins.changed", (d) => {
    const pet = d?.items?.find((p) => p.id === "pet");
    if (pet?.status && "windows" in pet.status) setWorld(pet.status as PetWorld);
  });
  const connected = useApp((s) => s.connected);
  useEffect(() => {
    if (!enabled || !connected || DEMO) return;
    let alive = true;
    api
      .get<{ items: PluginInfo[] }>("/api/plugins")
      .then((r) => {
        const pet = r.items.find((p) => p.id === "pet");
        if (alive && pet?.status && "windows" in pet.status) setWorld(pet.status as PetWorld);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [enabled, connected]);

  // плагин выключили и включили снова — «спрятан до перезапуска» больше не действует
  useEffect(() => {
    if (enabled && !wasEnabled.current) setAway(false);
    if (!enabled) setWorld(null);
    wasEnabled.current = enabled;
  }, [enabled]);

  const cfg = useMemo(() => readConfig(plugin?.config, world), [plugin?.config, world]);
  const onGone = useRef(() => setAway(true)).current;
  if (!enabled || away) return null;
  const paused = hidden || (!!world?.fullscreen && hideFullscreen);
  return <PetView key={cfg.skin} cfg={cfg} world={world} paused={paused} onGone={onGone} />;
}
