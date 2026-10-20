// Сфера-орб Jarvis из частиц: точки на сфере (спираль Фибоначчи) вращаются в 3D — ближние ярче и крупнее,
// дальние тускнеют; тонкий каркас меридианов и два орбитальных кольца с бегущей искрой. Цвет — цвет темы.
// Состояния: покой — медленно дышит; слушает — поверхность волнуется от громкости микрофона (voice.level);
// думает — вращается быстрее, по сфере бежит полоса «сканирования»; говорит — волны от громкости речи
// (tts.level). Canvas 2D без зависимостей: в покое — 30 кадров в секунду, вне экрана и в свёрнутом окне —
// ничего; «Анимации» выключены или в системе «уменьшить движение» — неподвижный кадр.
import { useEffect, useRef } from "react";
import { levels } from "@/lib/levels";
import { accentRgb } from "@/lib/theme";
import { useApp } from "@/store/app";

export type OrbState = "idle" | "listening" | "thinking" | "speaking";

export interface JarvisOrbProps {
  className?: string;
  /** Состояние вместо голосового (строка голоса, чат, который сейчас отвечает). */
  state?: OrbState;
  /** Радиус сферы — доля меньшей стороны холста. */
  scale?: number;
  /** Насколько сфера реагирует на звук (0..1.5). */
  reactivity?: number;
  /** Орбитальные кольца (у маленькой сферы не нужны). */
  rings?: boolean;
  /** Фиксированный цвет вместо цвета темы. */
  color?: string;
  onClick?: () => void;
  title?: string;
}

type Rgb = [number, number, number];

const TILT = 0.38; // наклон оси вращения к зрителю, рад
const CAMERA = 3.2; // расстояние до камеры в радиусах сферы
const MERIDIANS = 9;
const PARALLELS = [-0.62, -0.32, 0, 0.32, 0.62];
const RING_STEPS = 72;

function mix(rgb: Rgb, target: number, amount: number): Rgb {
  return rgb.map((c) => Math.round(c + (target - c) * amount)) as Rgb;
}

function css([r, g, b]: Rgb, a: number): string {
  return `rgba(${r}, ${g}, ${b}, ${a.toFixed(3)})`;
}

/** Точки на сфере, равномерно: спираль Фибоначчи. */
function fibonacci(n: number): Float32Array {
  const out = new Float32Array(n * 3);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - ((i + 0.5) / n) * 2;
    const r = Math.sqrt(1 - y * y);
    const a = i * golden;
    out[i * 3] = Math.cos(a) * r;
    out[i * 3 + 1] = y;
    out[i * 3 + 2] = Math.sin(a) * r;
  }
  return out;
}

/** Светящаяся точка: белое ядро → цвет темы → прозрачный край (рисуется drawImage — быстро). */
function makeSprite(rgb: Rgb): HTMLCanvasElement {
  const size = 32;
  const c = document.createElement("canvas");
  c.width = size;
  c.height = size;
  const g = c.getContext("2d")!;
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  const hot = mix(rgb, 255, 0.78);
  grad.addColorStop(0, css(hot, 1));
  grad.addColorStop(0.2, css(mix(rgb, 255, 0.45), 0.95));
  grad.addColorStop(0.48, css(rgb, 0.42));
  grad.addColorStop(1, css(rgb, 0));
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  return c;
}

function reducedMotion(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** Состояние сферы по голосу Jarvis (если его не задали снаружи). */
function voiceState(): OrbState {
  if (levels.speaking || levels.state === "speaking") return "speaking";
  if (levels.state === "listening") return "listening";
  if (levels.state === "processing") return "thinking";
  return "idle";
}

export function JarvisOrb({
  className,
  state,
  scale = 0.32,
  reactivity = 1,
  rings = true,
  color,
  onClick,
  title,
}: JarvisOrbProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const accent = useApp((s) => s.settings?.interface.accent ?? "#3B82F6");
  const animated = useApp((s) => s.settings?.interface.animations ?? true);
  const opts = useRef({ accent: color ?? accent, animated, state, scale, reactivity, rings });
  opts.current = { accent: color ?? accent, animated, state, scale, reactivity, rings };
  const kick = useRef<() => void>(() => undefined);

  useEffect(() => {
    const canvas = canvasRef.current!;
    const ctx = canvas.getContext("2d")!;
    let raf = 0;
    let w = 0;
    let h = 0;
    let points = fibonacci(400);
    let count = 400;
    let sprite: HTMLCanvasElement | null = null;
    let spriteFor = "";
    let energy = 0.2;
    let level = 0;
    let spin = Math.random() * Math.PI * 2;
    let t = Math.random() * 50;
    let last = performance.now();
    let lastDraw = 0;
    let onScreen = true;
    let still = reducedMotion();

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas.getBoundingClientRect();
      w = Math.max(1, rect.width);
      h = Math.max(1, rect.height);
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // точек — по размеру: маленькой сфере в строке голоса хватит полутора сотен
      const R = Math.min(w, h) * opts.current.scale;
      const want = Math.max(140, Math.min(1300, Math.round(R * 7.5)));
      if (want !== count) {
        count = want;
        points = fibonacci(count);
      }
    };

    const draw = (now: number) => {
      raf = 0;
      const o = opts.current;
      const live = o.animated && !still;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const mode = o.state ?? voiceState();
      // покой без вспышки — 30 кадров в секунду: сфера медленная, разница не видна, а работы вдвое меньше
      const calm = mode === "idle" && energy < 0.32 && now - levels.wakeAt > 900;
      if (live && calm && now - lastDraw < 31) {
        schedule();
        return;
      }
      const step = lastDraw ? Math.min(0.066, (now - lastDraw) / 1000) : dt;
      lastDraw = now;

      const rgb = accentRgb(o.accent);
      const key = rgb.join(",");
      if (!sprite || spriteFor !== key) {
        sprite = makeSprite(rgb);
        spriteFor = key;
      }

      // громкость: микрофон — пока слушает, речь Jarvis — пока говорит; текст в чате — ровная «речь»
      const synthetic = 0.22 + 0.16 * Math.sin(now / 160) * Math.sin(now / 410);
      const raw =
        mode === "listening"
          ? levels.mic
          : mode === "speaking"
            ? levels.speaking
              ? levels.tts
              : Math.max(levels.tts, synthetic)
            : 0;
      level += (raw - level) * Math.min(1, step * (raw > level ? 14 : 5));
      const flash = Math.max(0, 1 - (now - levels.wakeAt) / 800);
      const target =
        mode === "listening"
          ? 0.55 + level * 1.1 * o.reactivity
          : mode === "speaking"
            ? 0.5 + level * 1.2 * o.reactivity
            : mode === "thinking"
              ? 0.62
              : 0.22;
      energy += (Math.min(1.6, target + flash * 0.5) - energy) * Math.min(1, step * (target > energy ? 6 : 2.2));
      const speed = mode === "thinking" ? 1.25 : 0.32 + energy * 0.35;
      if (live) {
        spin += step * speed * 0.55;
        t += step;
      }

      ctx.clearRect(0, 0, w, h);
      if (w < 4 || h < 4) return;
      const cx = w / 2;
      const cy = h / 2;
      const half = Math.min(w, h) / 2;
      const breathe = 1 + 0.022 * Math.sin(t * 1.15) + 0.05 * energy * (mode === "idle" ? 0.4 : 1) + 0.05 * flash;
      const R = Math.min(w, h) * o.scale * breathe;

      // ореол: гаснет до края холста, иначе виден квадрат
      const haloR = Math.max(R * 0.4, Math.min(R * 1.75, half - 1));
      const halo = ctx.createRadialGradient(cx, cy, R * 0.15, cx, cy, haloR);
      halo.addColorStop(0, css(rgb, 0.3 + 0.18 * energy + 0.2 * flash));
      halo.addColorStop(0.45, css(rgb, 0.1 + 0.08 * energy));
      halo.addColorStop(1, css(rgb, 0));
      ctx.fillStyle = halo;
      ctx.fillRect(0, 0, w, h);

      // ядро: мягкий свет изнутри — ярче, когда говорит
      const coreR = R * (0.78 + 0.12 * level);
      const core = ctx.createRadialGradient(cx - R * 0.12, cy - R * 0.16, 0, cx, cy, coreR);
      core.addColorStop(0, css(mix(rgb, 255, 0.55), 0.34 + 0.3 * level + 0.2 * flash));
      core.addColorStop(0.55, css(rgb, 0.12 + 0.1 * energy));
      core.addColorStop(1, css(rgb, 0));
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fill();

      ctx.save();
      ctx.globalCompositeOperation = "lighter";

      const cosS = Math.cos(spin);
      const sinS = Math.sin(spin);
      const cosT = Math.cos(TILT);
      const sinT = Math.sin(TILT);
      // поворот вокруг оси Y (вращение) и наклон к зрителю (вокруг X); z > 0 — к зрителю
      const rotate = (x: number, y: number, z: number): [number, number, number] => {
        const x1 = x * cosS + z * sinS;
        const z1 = -x * sinS + z * cosS;
        const y2 = y * cosT - z1 * sinT;
        const z2 = y * sinT + z1 * cosT;
        return [x1, y2, z2];
      };
      const project = (x: number, y: number, z: number, radius: number): [number, number, number] => {
        const k = CAMERA / (CAMERA - z);
        return [cx + x * radius * k, cy + y * radius * k, k];
      };

      // каркас: меридианы и параллели, ближняя половина ярче дальней
      const wireA = Math.min(0.22, (0.07 + 0.08 * energy) * (R > 30 ? 1 : 0.6));
      const front = new Path2D();
      const back = new Path2D();
      const segment = (pts: [number, number, number][]) => {
        for (let i = 1; i < pts.length; i++) {
          const a = pts[i - 1];
          const b = pts[i];
          const path = a[2] + b[2] > 0 ? front : back;
          const pa = project(a[0], a[1], a[2], R);
          const pb = project(b[0], b[1], b[2], R);
          path.moveTo(pa[0], pa[1]);
          path.lineTo(pb[0], pb[1]);
        }
      };
      const seg = R > 60 ? 40 : 24;
      for (let m = 0; m < MERIDIANS; m++) {
        const lon = (m / MERIDIANS) * Math.PI;
        const pts: [number, number, number][] = [];
        for (let i = 0; i <= seg; i++) {
          const a = (i / seg) * Math.PI * 2;
          pts.push(rotate(Math.cos(a) * Math.cos(lon), Math.sin(a), Math.cos(a) * Math.sin(lon)));
        }
        segment(pts);
      }
      for (const lat of PARALLELS) {
        const r = Math.sqrt(1 - lat * lat);
        const pts: [number, number, number][] = [];
        for (let i = 0; i <= seg; i++) {
          const a = (i / seg) * Math.PI * 2;
          pts.push(rotate(Math.cos(a) * r, lat, Math.sin(a) * r));
        }
        segment(pts);
      }
      ctx.lineWidth = R > 60 ? 0.8 : 0.6;
      ctx.strokeStyle = css(rgb, wireA * 0.45);
      ctx.stroke(back);
      ctx.strokeStyle = css(mix(rgb, 255, 0.25), wireA);
      ctx.stroke(front);

      // частицы
      const amp =
        mode === "listening"
          ? 0.035 + level * 0.2 * o.reactivity
          : mode === "speaking"
            ? 0.03 + level * 0.16 * o.reactivity
            : mode === "thinking"
              ? 0.03
              : 0.018;
      const scan = mode === "thinking" ? Math.sin(t * 2.2) * 0.9 : 2; // высота полосы «сканирования»
      const dotBase = Math.max(1.7, R * 0.044) * (1 + 0.25 * energy);
      const img = sprite;
      for (let i = 0; i < count; i++) {
        const px = points[i * 3];
        const py = points[i * 3 + 1];
        const pz = points[i * 3 + 2];
        let d: number;
        if (mode === "speaking") {
          // волны бегут от «лица» сферы к краям
          d = 1 + amp * Math.sin(pz * 7 - t * 9) * 0.8 + amp * 0.35 * Math.sin(px * 5 + t * 3);
        } else {
          d = 1 + amp * (Math.sin(px * 3.1 + t * 1.7) * Math.sin(py * 4.3 - t * 1.3) + 0.6 * Math.sin(pz * 5.2 + t * 2.1));
        }
        const [x, y, z] = rotate(px * d, py * d, pz * d);
        const [sx, sy, k] = project(x, y, z, R);
        const depth = (z + 1) / 2; // 0 — дальняя сторона, 1 — ближняя
        let alpha = 0.08 + 0.92 * depth * depth;
        let size = dotBase * (0.45 + 0.8 * depth) * k;
        if (mode === "thinking") {
          const near = Math.max(0, 1 - Math.abs(y - scan) * 4.5);
          alpha = Math.min(1, alpha + near * 0.8);
          size *= 1 + near * 0.9;
        }
        alpha = Math.min(1, alpha * (0.85 + 0.4 * energy + 0.3 * flash));
        ctx.globalAlpha = alpha;
        ctx.drawImage(img, sx - size / 2, sy - size / 2, size, size);
      }
      ctx.globalAlpha = 1;

      // кромка: тонкий свет по краю — сфера читается как объём, а не плоское облако точек
      const rim = ctx.createRadialGradient(cx, cy, R * 0.86, cx, cy, R * 1.1);
      rim.addColorStop(0, css(rgb, 0));
      rim.addColorStop(0.55, css(mix(rgb, 255, 0.2), 0.16 + 0.12 * energy + 0.15 * flash));
      rim.addColorStop(1, css(rgb, 0));
      ctx.fillStyle = rim;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 1.1, 0, Math.PI * 2);
      ctx.fill();

      // орбитальные кольца с искрой
      if (o.rings && R > 34) {
        const orbits = [
          { r: 1.24, tiltX: 1.18, tiltZ: 0.32, speed: 0.9, phase: 0 },
          { r: 1.38, tiltX: 1.32, tiltZ: -0.44, speed: -0.62, phase: 2.1 },
        ];
        for (const orbit of orbits) {
          const cz = Math.cos(orbit.tiltZ);
          const sz = Math.sin(orbit.tiltZ);
          const cxx = Math.cos(orbit.tiltX);
          const sxx = Math.sin(orbit.tiltX);
          const at = (a: number): [number, number, number] => {
            const x = Math.cos(a) * orbit.r;
            const z = Math.sin(a) * orbit.r;
            const y1 = -z * sxx;
            const z1 = z * cxx;
            return [x * cz - y1 * sz, x * sz + y1 * cz, z1];
          };
          const ringFront = new Path2D();
          const ringBack = new Path2D();
          let prev = at(0);
          for (let i = 1; i <= RING_STEPS; i++) {
            const cur = at((i / RING_STEPS) * Math.PI * 2);
            const path = prev[2] + cur[2] > 0 ? ringFront : ringBack;
            const pa = project(prev[0], prev[1], prev[2], R);
            const pb = project(cur[0], cur[1], cur[2], R);
            path.moveTo(pa[0], pa[1]);
            path.lineTo(pb[0], pb[1]);
            prev = cur;
          }
          ctx.lineWidth = 1;
          ctx.strokeStyle = css(rgb, 0.07 + 0.06 * energy);
          ctx.stroke(ringBack);
          ctx.strokeStyle = css(mix(rgb, 255, 0.2), 0.14 + 0.12 * energy);
          ctx.stroke(ringFront);
          // искра с хвостом
          const head = t * orbit.speed * (mode === "thinking" ? 2.2 : 1) + orbit.phase;
          for (let j = 0; j < 14; j++) {
            const a = head - j * 0.045 * Math.sign(orbit.speed);
            const p = at(a);
            const [sx, sy, k] = project(p[0], p[1], p[2], R);
            const fade = 1 - j / 14;
            const size = dotBase * 1.9 * fade * k;
            ctx.globalAlpha = (p[2] > 0 ? 0.95 : 0.35) * fade * (0.6 + 0.4 * energy);
            ctx.drawImage(img, sx - size / 2, sy - size / 2, size, size);
          }
          ctx.globalAlpha = 1;
        }
      }
      ctx.restore();

      if (live) schedule();
    };

    function schedule() {
      if (!raf && onScreen && !document.hidden) raf = requestAnimationFrame(draw);
    }

    const restart = () => {
      if (raf) return;
      last = performance.now();
      lastDraw = 0;
      raf = requestAnimationFrame(draw);
    };
    kick.current = restart;

    resize();
    const ro = new ResizeObserver(() => {
      resize();
      restart();
    });
    ro.observe(canvas);
    const io = new IntersectionObserver((entries) => {
      onScreen = entries.some((e) => e.isIntersecting);
      if (onScreen) restart();
    });
    io.observe(canvas);
    const onVisible = () => {
      if (!document.hidden) restart();
    };
    document.addEventListener("visibilitychange", onVisible);
    const motion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const onMotion = () => {
      still = reducedMotion();
      restart();
    };
    motion?.addEventListener?.("change", onMotion);
    // неподвижная сфера всё равно показывает смену состояния (слушает / думает)
    const slow = window.setInterval(() => {
      if (!opts.current.animated || still) restart();
    }, 500);
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      clearInterval(slow);
      ro.disconnect();
      io.disconnect();
      document.removeEventListener("visibilitychange", onVisible);
      motion?.removeEventListener?.("change", onMotion);
    };
  }, []);

  // включили анимации, сменили состояние или цвет — перезапустить цикл
  useEffect(() => kick.current(), [animated, state, accent, color, scale]);

  return (
    <canvas
      ref={canvasRef}
      className={className}
      onClick={onClick}
      title={title}
      aria-hidden={onClick ? undefined : true}
      style={{ width: "100%", height: "100%", display: "block", cursor: onClick ? "pointer" : undefined }}
    />
  );
}
