// Пиксельная медуза «Люкси» — питомец Jarvis. Рисуется процедурно на сетке 24×24: купол со спиралью,
// счастливые глазки «^ ^», щёчки, антенна и волнистые щупальца. Кадр зависит от времени — так она «плывёт».
// Холст (PetCanvas) — для виджета и плитки плагина, SVG (JellyIcon) — для маленьких значков.
import { useEffect, useRef } from "react";

export type Pixel = [x: number, y: number, color: string];
export type JellyMood = "idle" | "happy" | "dance" | "sleep" | "eat";

const GRID = 24;
const C = {
  outline: "#1b3aa8",
  body: "#2f6bff",
  ring: "#5f95ff",
  ring2: "#b9d3ff",
  shade: "#2858e6",
  frill: "#8fb8ff",
  shine: "#e6f0ff",
  eye: "#0b1435",
  cheek: "#ff7ab8",
  mouth: "#ff4d7d",
  tent: "#22d3ee",
  tentDark: "#0ea5c6",
  tip: "#b6f6ff",
  ball: "#67e8f9",
  heart: "#ff5fa2",
  zzz: "#c7d2fe",
};

/** Пиксели одного кадра. t — секунды, blink — моргнуть, mood — настроение/действие. */
export function jellyFrame(t: number, { blink = false, mood = "idle" as JellyMood, still = false } = {}): Pixel[] {
  const px: Pixel[] = [];
  const amp = still ? 0 : mood === "happy" ? 2.2 : mood === "sleep" ? 0.4 : 0.9;
  const bob = Math.round(Math.sin(t * (mood === "happy" ? 6 : 2.2)) * amp);
  const sway = still ? 0 : mood === "dance" ? Math.round(Math.sin(t * 5) * 2) : 0;
  const cx = 12 + sway;
  const rx = 9;
  const ry = 7;
  const top = 6 + bob;
  const base = top + ry; // нижний край купола
  const inside = (x: number, y: number) => {
    if (y > base || y < top - 1) return false;
    const dx = (x + 0.5 - cx) / rx;
    const dy = (y + 0.5 - base) / ry;
    return dx * dx + dy * dy <= 1;
  };

  // антенна с шариком (слева сверху) и искорка
  const ax = cx - 5;
  px.push([ax, top - 1, C.tent], [ax - 1, top - 2, C.tent], [ax - 1, top - 3, C.tent]);
  px.push([ax - 2, top - 5, C.ball], [ax - 1, top - 5, C.ball], [ax - 2, top - 4, C.ball], [ax - 1, top - 4, C.tip]);
  if (!still && Math.sin(t * 3.1) > 0.2) {
    const sx = ax - 5;
    const sy = top - 5;
    px.push([sx, sy - 1, C.tip], [sx - 1, sy, C.tip], [sx, sy, C.ball], [sx + 1, sy, C.tip], [sx, sy + 1, C.tip]);
  }

  // купол: контур, светлая макушка, спираль, тень снизу и «юбочка» по краю
  const sx = cx + 0.5;
  const sy = top + 2.6;
  for (let y = top - 1; y <= base; y++) {
    for (let x = cx - rx - 1; x <= cx + rx + 1; x++) {
      if (!inside(x, y)) continue;
      const edge = !inside(x - 1, y) || !inside(x + 1, y) || !inside(x, y - 1);
      let color = C.body;
      if (y === base) color = (x + Math.round(t * 2)) % 2 === 0 ? C.frill : C.outline;
      else if (edge) color = C.outline;
      else {
        const e = Math.hypot((x + 0.5 - sx) / 3.1, (y + 0.5 - sy) / 1.9);
        if (Math.abs(e - 1) < 0.2 || (x === cx && y === top + 2)) color = C.ring2;
        else if (Math.abs(e - 0.45) < 0.22 && x > cx - 1) color = C.ring;
        else if (y >= base - 2) color = C.shade;
        else if (y <= top + 1 && x < cx - 1) color = C.ring;
      }
      px.push([x, y, color]);
    }
  }
  // блик
  px.push([cx - 6, top + 2, C.shine], [cx - 7, top + 3, C.shine], [cx - 5, top + 1, C.shine]);

  // мордочка
  const ey = base - 3;
  const eyes = [cx - 4, cx + 3];
  for (const ex of eyes) {
    if (blink || mood === "sleep") {
      px.push([ex - 1, ey + 1, C.eye], [ex, ey + 1, C.eye], [ex + 1, ey + 1, C.eye]);
    } else {
      px.push([ex, ey, C.eye], [ex - 1, ey + 1, C.eye], [ex + 1, ey + 1, C.eye]);
    }
  }
  px.push([cx - 7, ey + 2, C.cheek], [cx - 6, ey + 2, C.cheek], [cx + 5, ey + 2, C.cheek], [cx + 6, ey + 2, C.cheek]);
  if (mood === "eat") px.push([cx - 1, ey + 1, C.mouth], [cx, ey + 1, C.mouth], [cx - 1, ey + 2, C.mouth], [cx, ey + 2, C.mouth]);
  else px.push([cx - 1, ey + 2, C.mouth], [cx, ey + 2, C.mouth]);

  // щупальца
  const anchors = [-6, -3, 0, 3, 6];
  const lengths = [6, 8, 9, 8, 6];
  anchors.forEach((off, i) => {
    const len = lengths[i];
    const speed = mood === "dance" ? 6 : mood === "sleep" ? 1 : 3;
    for (let k = 0; k < len; k++) {
      const wave = still ? Math.sin(i * 1.3 + k * 0.6) * 0.8 : Math.sin(t * speed + i * 1.3 + k * 0.55);
      const x = cx + off + Math.round(wave * (k / len) * 1.8);
      const y = base + 1 + k;
      const color = k === len - 1 ? C.tip : k % 3 === 2 ? C.tentDark : C.tent;
      px.push([x, y, color]);
    }
  });

  // сердечки при радости, «z» во сне
  if (mood === "happy" && !still) {
    const p = (t * 1.4) % 1;
    const hy = Math.round(top - 3 - p * 4);
    const hx = cx + 7;
    px.push([hx, hy, C.heart], [hx + 2, hy, C.heart], [hx, hy + 1, C.heart], [hx + 1, hy + 1, C.heart], [hx + 2, hy + 1, C.heart], [hx + 1, hy + 2, C.heart]);
  }
  if (mood === "sleep" && !still) {
    const p = Math.floor(t * 1.5) % 3;
    const zx = cx + 7 + p;
    const zy = top - 2 - p * 2;
    px.push([zx, zy, C.zzz], [zx + 1, zy, C.zzz], [zx + 1, zy + 1, C.zzz], [zx, zy + 2, C.zzz], [zx + 1, zy + 2, C.zzz]);
  }
  return px.filter(([x, y]) => x >= 0 && y >= 0 && x < GRID && y < GRID);
}

/** Статичная медуза в SVG (значок плагина «Питомец»). */
export function JellyIcon({ size = "100%" }: { size?: number | string }) {
  const pixels = jellyFrame(0, { still: true });
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} shapeRendering="crispEdges" aria-hidden>
      {pixels.map(([x, y, c], i) => (
        <rect key={i} x={x} y={y} width={1.02} height={1.02} fill={c} />
      ))}
    </svg>
  );
}

function animationsOn(): boolean {
  return document.documentElement.dataset.anim !== "off";
}

/** Живая медуза на холсте: плавает, моргает, радуется по клику (mood задаёт действие). */
export function PetCanvas({
  size,
  mood = "idle",
  className,
  onClick,
}: {
  size: number;
  mood?: JellyMood;
  className?: string;
  onClick?: () => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const moodRef = useRef(mood);
  moodRef.current = mood;

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const scale = Math.max(1, Math.floor(size / GRID));
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = GRID * scale * dpr;
    canvas.height = GRID * scale * dpr;
    let raf = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let nextBlink = performance.now() + 2500;
    let blinkUntil = 0;
    const draw = (now: number) => {
      const live = animationsOn() && !document.hidden;
      if (now > nextBlink) {
        blinkUntil = now + 160;
        nextBlink = now + 2600 + Math.random() * 2600;
      }
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const unit = scale * dpr;
      for (const [x, y, c] of jellyFrame(now / 1000, { blink: live && now < blinkUntil, mood: moodRef.current, still: !live })) {
        ctx.fillStyle = c;
        ctx.fillRect(x * unit, y * unit, unit, unit);
      }
      // ~12 кадров в секунду — «пиксельная» плавность и мало работы; без анимаций — редкая проверка
      timer = setTimeout(() => (raf = requestAnimationFrame(draw)), live ? 80 : 700);
    };
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(timer);
    };
  }, [size]);

  const box = Math.max(1, Math.floor(size / GRID)) * GRID;
  return (
    <canvas
      ref={ref}
      className={className}
      style={{ width: box, height: box, imageRendering: "pixelated", cursor: onClick ? "pointer" : undefined }}
      onClick={onClick}
      aria-label="Питомец"
    />
  );
}
