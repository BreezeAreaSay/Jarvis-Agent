// «Подключения · MCP»: в центре — плитка цвета темы со штекером, вокруг на двух орбитах кружат значки
// серверов (подключённые — ярко, витрина каталога — приглушённо), по краям дрейфуют частицы.
// Анимации — только CSS; при data-anim="off" всё стоит на месте.
import { Plug } from "lucide-react";
import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import type { McpServer } from "@/api/types";
import { cx } from "@/ui";
import { McpIcon, mcpBrandKey, SHOWCASE } from "./mcpKit";

interface OrbitItem {
  key: string;
  brand: string;
  title: string;
  server?: McpServer;
}

function seeded(seed: number) {
  let s = seed;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

export function McpOrbit({ servers, size = 320 }: { servers: McpServer[]; size?: number }) {
  const items = useMemo(() => {
    const own: OrbitItem[] = servers.map((s) => ({ key: s.id, brand: mcpBrandKey(s), title: s.name, server: s }));
    const used = new Set(own.map((o) => o.brand).filter(Boolean));
    const show: OrbitItem[] = SHOWCASE.filter((b) => !used.has(b)).map((b) => ({ key: `show-${b}`, brand: b, title: "" }));
    return [...own, ...show];
  }, [servers]);
  const inner = items.slice(0, 6);
  const outer = items.slice(6, 16);
  const r1 = size * 0.25;
  const r2 = size * 0.41;
  const tile = Math.round(size * 0.12);

  const particles = useMemo(() => {
    const rnd = seeded(7);
    return Array.from({ length: 44 }, (_, i) => {
      const a = rnd() * Math.PI * 2;
      const r = size * (0.36 + rnd() * 0.3);
      return {
        i,
        x: size / 2 + Math.cos(a) * r,
        y: size / 2 + Math.sin(a) * r,
        s: 1.5 + rnd() * 2,
        dx: (rnd() - 0.5) * 18,
        dy: (rnd() - 0.5) * 18,
        d: 5 + rnd() * 7,
        delay: -rnd() * 10,
      };
    });
  }, [size]);

  const place = (list: OrbitItem[], r: number, offset: number) =>
    list.map((it, i) => {
      const angle = offset + (360 / Math.max(list.length, 1)) * i;
      const live = it.server?.status === "connected";
      const bad = it.server?.status === "error";
      return (
        <div
          key={it.key}
          className="mo-item"
          style={{ transform: `translate(-50%, -50%) rotate(${angle}deg) translateX(${r}px) rotate(${-angle}deg)` } as CSSProperties}
        >
          <div className="mo-upright">
            <div
              className={cx("mo-tile", !it.server && "showcase", live && "live", bad && "bad")}
              title={it.server ? `${it.title} — ${live ? "подключён" : bad ? "ошибка" : "не подключён"}` : undefined}
            >
              <McpIcon item={it.server} brandKey={it.brand || undefined} size={tile} />
              {it.server && <span className={cx("mo-dot", live && "ok", bad && "err")} />}
            </div>
          </div>
        </div>
      );
    });

  return (
    <div className="mcp-orbit" style={{ "--size": `${size}px` } as CSSProperties}>
      <div className="mo-disc d1" />
      <div className="mo-disc d2" />
      <div className="mo-disc d3" />
      <svg className="mo-lines" viewBox={`0 0 ${size} ${size}`} aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r1} />
        <circle cx={size / 2} cy={size / 2} r={r2} />
      </svg>
      <div className="mo-particles" aria-hidden>
        {particles.map((p) => (
          <i
            key={p.i}
            style={
              {
                left: p.x,
                top: p.y,
                width: p.s,
                height: p.s,
                "--dx": `${p.dx}px`,
                "--dy": `${p.dy}px`,
                animationDuration: `${p.d}s`,
                animationDelay: `${p.delay}s`,
              } as CSSProperties
            }
          />
        ))}
      </div>
      <div className="mo-ring r1">{place(inner, r1, -90)}</div>
      <div className="mo-ring r2">{place(outer, r2, -72)}</div>
      <div className="mo-core">
        <Plug />
        <span>MCP</span>
      </div>
    </div>
  );
}

/** Число, которое «набегает» от нуля (при выключенных анимациях — сразу). */
export function CountUp({ value, className }: { value: number; className?: string }) {
  const [shown, setShown] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    if (document.documentElement.dataset.anim === "off") {
      setShown(value);
      from.current = value;
      return;
    }
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - start) / 1300);
      const e = 1 - (1 - k) ** 3;
      const v = Math.round(a + (value - a) * e);
      setShown(v);
      if (k < 1) raf = requestAnimationFrame(step);
      else from.current = value;
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value]);
  return <span className={className}>{shown.toLocaleString("ru-RU")}</span>;
}
