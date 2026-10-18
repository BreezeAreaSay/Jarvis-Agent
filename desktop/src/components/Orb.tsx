// Живая сфера Jarvis: концентрические «органические» слои цвета темы, дышат в покое, пульсируют в такт
// голосу пользователя (voice.level) и речи Jarvis (tts.level), вспыхивают на «Джарвис».
import { useEffect, useRef } from "react";
import { levels } from "@/lib/levels";
import { accentRgb } from "@/lib/theme";
import { useApp } from "@/store/app";

export const LOGO_PATH =
  "M58 13C66.5 11.5 72.5 18 71.6 27.2L68.4 60.5C66.6 79.6 52.2 88.6 36.9 86.4C23.6 84.5 16.4 74 18.8 63.6C20.7 55.4 29.3 51.4 36.2 55.6C41.2 58.6 41.3 65.6 45.2 66.8C50.1 68.2 52.2 63.6 52.8 57L55.1 26.6C55.5 19.4 52.7 14 58 13Z";

interface Layer {
  r: number; // доля радиуса
  alpha: number;
  light: number; // сдвиг светлоты
  waves: { k: number; amp: number; phase: number; speed: number }[];
  drift: { ax: number; ay: number; sx: number; sy: number; px: number; py: number };
}

function makeLayers(seed: number): Layer[] {
  let s = seed;
  const rnd = () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
  const spec = [
    { r: 1.0, alpha: 0.34, light: -0.56 },
    { r: 0.84, alpha: 0.5, light: -0.4 },
    { r: 0.69, alpha: 0.72, light: -0.24 },
    { r: 0.54, alpha: 0.9, light: -0.1 },
    { r: 0.4, alpha: 1, light: 0.02 },
  ];
  return spec.map((l, i) => ({
    ...l,
    waves: [2, 3, 4, 6].map((k) => ({
      k,
      amp: (0.014 + rnd() * 0.024) * (k <= 3 ? 1.3 : 0.6) * (1 + i * 0.06),
      phase: rnd() * Math.PI * 2,
      speed: (0.18 + rnd() * 0.32) * (rnd() > 0.5 ? 1 : -1),
    })),
    drift: {
      ax: 0.012 + rnd() * 0.022,
      ay: 0.012 + rnd() * 0.022,
      sx: 0.13 + rnd() * 0.2,
      sy: 0.11 + rnd() * 0.2,
      px: rnd() * 6,
      py: rnd() * 6,
    },
  }));
}

function shade(rgb: [number, number, number], amount: number): string {
  // amount < 0 — к тёмно-синему фону, > 0 — к белому
  const target = amount < 0 ? [18, 22, 36] : [255, 255, 255];
  const a = Math.abs(amount);
  return rgb.map((c, i) => Math.round(c + (target[i] - c) * a)).join(", ");
}

export interface OrbProps {
  className?: string;
  /** Показывать логотип в центре. */
  logo?: boolean;
  /** Насколько сфера реагирует на звук (0..1.5). */
  reactivity?: number;
  /** Масштаб радиуса относительно меньшей стороны холста. */
  scale?: number;
  /** Фиксированный цвет вместо цвета темы. */
  color?: string;
  onClick?: () => void;
  title?: string;
}

export function Orb({ className, logo = true, reactivity = 1, scale = 0.46, color, onClick, title }: OrbProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const accent = useApp((s) => s.settings?.interface.accent ?? "#3B82F6");
  const animated = useApp((s) => (s.settings ? s.settings.interface.animations && s.settings.avatar.animated : true));
  const opts = useRef({ accent: color ?? accent, animated, reactivity, scale, logo });
  opts.current = { accent: color ?? accent, animated, reactivity, scale, logo };

  useEffect(() => {
    const canvas = canvasRef.current!;
    const ctx = canvas.getContext("2d")!;
    const layers = makeLayers(7919);
    const logoPath = new Path2D(LOGO_PATH);
    let raf = 0;
    let w = 0;
    let h = 0;
    let energy = 0;
    let flash = 0;
    let last = performance.now();
    let t = Math.random() * 100;

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas.getBoundingClientRect();
      w = Math.max(1, rect.width);
      h = Math.max(1, rect.height);
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const blob = (cx: number, cy: number, radius: number, layer: Layer, time: number, wobble: number) => {
      const n = 56;
      const pts: [number, number][] = [];
      for (let i = 0; i < n; i++) {
        const a = (i / n) * Math.PI * 2;
        let d = 1;
        for (const wv of layer.waves) d += wv.amp * wobble * Math.sin(wv.k * a + wv.phase + time * wv.speed);
        pts.push([cx + Math.cos(a) * radius * d, cy + Math.sin(a) * radius * d]);
      }
      ctx.beginPath();
      const mid = (p: [number, number], q: [number, number]) => [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2];
      const start = mid(pts[n - 1], pts[0]);
      ctx.moveTo(start[0], start[1]);
      for (let i = 0; i < n; i++) {
        const p = pts[i];
        const m = mid(p, pts[(i + 1) % n]);
        ctx.quadraticCurveTo(p[0], p[1], m[0], m[1]);
      }
      ctx.closePath();
    };

    const draw = (now: number) => {
      const o = opts.current;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const rgb = accentRgb(o.accent);
      const state = levels.state;
      const listening = state === "listening";
      const speaking = levels.speaking || state === "speaking";
      const processing = state === "processing";
      const target =
        0.25 +
        (listening ? 0.25 + levels.mic * 1.6 : levels.mic * 0.5) * o.reactivity +
        (speaking ? 0.2 + levels.tts * 1.4 : 0) * o.reactivity +
        (processing ? 0.35 : 0);
      energy += (Math.min(1.8, target) - energy) * Math.min(1, dt * (target > energy ? 10 : 3));
      flash = Math.max(0, 1 - (now - levels.wakeAt) / 700);
      t += dt * (0.55 + energy * 0.9 + (processing ? 0.8 : 0));

      ctx.clearRect(0, 0, w, h);
      const cx = w / 2;
      const cy = h / 2;
      const R = Math.min(w, h) * o.scale;
      const breathe = 1 + 0.018 * Math.sin(t * 0.9) + 0.07 * energy + 0.06 * flash;

      // ореол
      // ореол гаснет до края холста, иначе виден квадрат
      const glowR = Math.min(R * 1.45, Math.min(w, h) / 2 - 1);
      const glow = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, glowR);
      glow.addColorStop(0, `rgba(${shade(rgb, -0.1)}, ${0.22 + 0.12 * energy + 0.2 * flash})`);
      glow.addColorStop(1, `rgba(${shade(rgb, -0.4)}, 0)`);
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, w, h);

      const wobble = 0.9 + energy * 1.3 + flash * 0.6;
      for (const layer of layers) {
        const d = layer.drift;
        const ox = Math.sin(t * d.sx + d.px) * d.ax * R;
        const oy = Math.cos(t * d.sy + d.py) * d.ay * R;
        const radius = R * layer.r * breathe;
        blob(cx + ox, cy + oy, radius, layer, t, wobble);
        const g = ctx.createRadialGradient(cx + ox - radius * 0.25, cy + oy - radius * 0.3, radius * 0.1, cx + ox, cy + oy, radius);
        const lift = layer.light + 0.1 * flash + 0.06 * energy;
        g.addColorStop(0, `rgba(${shade(rgb, lift + 0.05)}, ${layer.alpha})`);
        g.addColorStop(1, `rgba(${shade(rgb, lift - 0.03)}, ${layer.alpha})`);
        ctx.fillStyle = g;
        ctx.fill();
      }

      // ядро
      const coreR = R * 0.24 * (1 + 0.05 * energy + 0.1 * flash);
      const cg = ctx.createRadialGradient(cx, cy - coreR * 0.3, coreR * 0.1, cx, cy, coreR);
      cg.addColorStop(0, `rgba(${shade(rgb, 0.18 + 0.15 * flash)}, 1)`);
      cg.addColorStop(1, `rgba(${shade(rgb, 0.04)}, 1)`);
      ctx.save();
      ctx.shadowColor = `rgba(${rgb.join(",")}, ${0.55 + 0.3 * energy})`;
      ctx.shadowBlur = R * 0.22;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fillStyle = cg;
      ctx.fill();
      ctx.restore();

      if (o.logo) {
        const size = coreR * 1.25;
        ctx.save();
        ctx.translate(cx - size / 2, cy - size / 2 + size * 0.02);
        ctx.scale(size / 100, size / 100);
        ctx.fillStyle = "#ffffff";
        ctx.shadowColor = "rgba(0,0,0,0.18)";
        ctx.shadowBlur = 6;
        ctx.fill(logoPath);
        ctx.restore();
      }

      if (opts.current.animated && !document.hidden) raf = requestAnimationFrame(draw);
      else raf = 0;
    };

    resize();
    const ro = new ResizeObserver(() => {
      resize();
      if (!raf) draw(performance.now());
    });
    ro.observe(canvas);
    const onVisible = () => {
      if (!document.hidden && !raf) {
        last = performance.now();
        raf = requestAnimationFrame(draw);
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  // включили анимации обратно — перезапустить цикл
  useEffect(() => {
    if (animated) window.dispatchEvent(new Event("visibilitychange"));
  }, [animated]);

  return (
    <canvas
      ref={canvasRef}
      className={className}
      onClick={onClick}
      title={title}
      style={{ width: "100%", height: "100%", display: "block", cursor: onClick ? "pointer" : undefined }}
    />
  );
}

export function LogoMark({ size = 36, className }: { size?: number; className?: string }) {
  return (
    <span
      className={className}
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        background: "radial-gradient(circle at 35% 30%, var(--accent-hover), var(--accent) 60%, var(--accent-press))",
        boxShadow: "0 4px 16px rgba(var(--accent-rgb), 0.45)",
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        flex: "none",
      }}
    >
      <svg viewBox="0 0 100 100" width={size * 0.6} height={size * 0.6} aria-hidden>
        <path d={LOGO_PATH} fill="var(--on-accent)" />
      </svg>
    </span>
  );
}
