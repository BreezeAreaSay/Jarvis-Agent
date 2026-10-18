// Пиксель-арт питомца, нарисованный кодом по точкам: медуза (как в образце: купол с бликом и кольцами,
// розовые щёчки, закрытые «^ ^» глаза, бирюзовые щупальца, антенна со светящимся шариком), призрак и котик.
// Позу задаёт «мозг» питомца (brain.ts): сжатие/вытягивание, глаза, рот, наушники, волна щупалец.
import { type Color, fade, Mask, Pixels, rgb, stamp } from "./pixels";

export const SPRITE_W = 40;
export const SPRITE_H = 46;
/** Холст шире и выше спрайта: над головой — ноты, «Zz», «!», сердечки. */
export const CANVAS_W = 76;
export const CANVAS_H = 104;
/** Строка «ног» на холсте; ниже — место для пыли при приземлении. */
export const FEET_Y = CANVAS_H - 4;
export const SPRITE_X = (CANVAS_W - SPRITE_W) / 2;
/** Центр спрайта по горизонтали (между 19-м и 20-м пикселем). */
const C = SPRITE_W / 2;

export type SkinId = "jelly" | "cat" | "ghost";
export type Eyes = "open" | "happy" | "closed" | "sleep" | "wide" | "sad";
export type Mouth = "smile" | "open" | "o" | "chew" | "flat" | "sad";

export interface Pose {
  t: number;
  squash: number; // >0 — сплющен, <0 — вытянут
  eyes: Eyes;
  mouth: Mouth;
  look: number; // -1..1 — куда смотрит
  headphones: boolean;
  disco: boolean; // фиолетовая «танцевальная» расцветка медузы
  wiggle: number; // сила волны щупалец
  dangle: number; // висит в руке (перетаскивание)
  glow: number; // свечение антенны
  sway: number; // -1..1 — наклон в танце
  walk: number; // фаза шага 0..1 или -1 — стоит
  arms: number; // 0..1 — машет лапками (призрак)
  facing: 1 | -1;
}

export interface Drawn {
  headTop: number; // верх головы (с антенной/ушами) — от верха спрайта
  mouthY: number;
  centerY: number;
}

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));

function angleDist(a: number, b: number): number {
  const d = Math.abs(((a - b) % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
  return Math.min(d, Math.PI * 2 - d);
}

// --- лицо -----------------------------------------------------------------------------------------------
interface Face {
  eye: Color;
  eyeHi: Color;
  eyeHi2: Color;
  mouth: Color;
  tongue: Color;
  cheek: Color;
  tear: Color;
}

const EYES: Record<Eyes, { dy: number; rows: string[] }> = {
  open: { dy: -1, rows: [".ee.", "eWee", "eehe", ".ee."] },
  wide: { dy: -2, rows: [".ee.", "eWee", "eeee", "eehe", ".ee."] },
  happy: { dy: 0, rows: [".ee.", "e..e"] },
  closed: { dy: 1, rows: ["eeee"] },
  sleep: { dy: 1, rows: ["e..e", ".ee."] },
  sad: { dy: -1, rows: [".ee.", "eeee", "eWee", ".ee."] },
};

function drawEye(px: Pixels, x: number, ey: number, kind: Eyes, f: Face, mirror: boolean) {
  const e = EYES[kind];
  stamp(px, e.rows, x, ey + e.dy, { e: f.eye, W: f.eyeHi, h: f.eyeHi2 }, { solid: true, flip: mirror && kind === "sad" });
}

function drawMouth(px: Pixels, x: number, my: number, kind: Mouth, f: Face, t: number) {
  let rows: string[];
  switch (kind) {
    case "open":
      rows = ["mmmm", "mttm"];
      break;
    case "o":
      rows = [".mm.", ".mm."];
      break;
    case "chew":
      rows = Math.floor(t * 7) % 2 ? ["mmmm", "mttm"] : ["....", "mmmm"];
      break;
    case "flat":
      rows = ["....", ".mm."];
      break;
    case "sad":
      rows = [".mm.", "m..m"];
      break;
    default:
      rows = ["m..m", ".mm."];
  }
  stamp(px, rows, x, my, { m: f.mouth, t: f.tongue }, { solid: true });
}

/** Глаза, щёчки и рот вокруг центра лица fx (левый глаз fx-7…fx-4, правый fx+5…fx+8). */
function drawFace(px: Pixels, ox: number, oy: number, fx: number, ey: number, p: Pose, f: Face, spread = 0) {
  const look = Math.round(clamp(p.look, -1, 1));
  drawEye(px, ox + fx - 7 + spread + look, oy + ey, p.eyes, f, false);
  drawEye(px, ox + fx + 5 - spread + look, oy + ey, p.eyes, f, true);
  if (p.eyes !== "sleep") {
    px.rect(ox + fx - 10 + spread, oy + ey + 3, 3, 2, f.cheek);
    px.rect(ox + fx + 9 - spread, oy + ey + 3, 3, 2, f.cheek);
  } else {
    px.rect(ox + fx - 10 + spread, oy + ey + 3, 3, 1, f.cheek);
    px.rect(ox + fx + 9 - spread, oy + ey + 3, 3, 1, f.cheek);
  }
  drawMouth(px, ox + fx - 1, oy + ey + 3, p.mouth, f, p.t);
  if (p.eyes === "sad") px.put(ox + fx - 6 + spread + look, oy + ey + 3, f.tear);
}

// --- наушники -------------------------------------------------------------------------------------------
const PHONES = {
  band: rgb("#a7adbd"),
  bandDark: rgb("#5f6678"),
  cup: rgb("#ff6fae"),
  cupHi: rgb("#ffc6df"),
  cupShade: rgb("#d4428a"),
  outline: rgb("#4a1431"),
};

function cup(px: Pixels, x: number, y: number, w: number, h: number, mirrored: boolean) {
  for (let yy = -1; yy <= h; yy++) {
    for (let xx = -1; xx <= w; xx++) {
      const corner = (xx === -1 || xx === w) && (yy === -1 || yy === h);
      if (corner) continue;
      const edge = xx === -1 || xx === w || yy === -1 || yy === h;
      const inCorner = (xx === 0 || xx === w - 1) && (yy === 0 || yy === h - 1);
      let c = PHONES.cup;
      if (edge || inCorner) c = PHONES.outline;
      else if (yy === h - 1 || xx === (mirrored ? 0 : w - 1)) c = PHONES.cupShade;
      px.put(x + xx, y + yy, c);
    }
  }
  const hx = mirrored ? x + w - 3 : x + 1;
  px.rect(hx, y + 1, 2, 2, PHONES.cupHi);
}

/** Дужка вокруг маски головы (от верха до чашек) и чашки по бокам на уровне глаз. */
function drawHeadphones(px: Pixels, ox: number, oy: number, mask: Mask, top: number, cupTop: number) {
  for (let y = Math.max(0, top - 4); y < cupTop; y++) {
    for (let x = 0; x < mask.w; x++) {
      if (mask.has(x, y)) continue;
      const d = mask.distance(x, y, 4);
      if (d > 1.5 && d <= 2.5) px.put(ox + x, oy + y, PHONES.band);
      else if (d > 2.5 && d <= 3.3) px.put(ox + x, oy + y, PHONES.bandDark);
    }
  }
  const row = cupTop + 3;
  let left = -1;
  let right = -1;
  for (let x = 0; x < mask.w; x++) {
    if (mask.has(x, row)) {
      if (left < 0) left = x;
      right = x;
    }
  }
  if (left < 0) return;
  cup(px, ox + left - 3, oy + cupTop, 5, 8, false);
  cup(px, ox + right - 1, oy + cupTop, 5, 8, true);
}

/** Контур по 4 соседям вокруг маски (строки y0…y1). */
function outline(px: Pixels, ox: number, oy: number, mask: Mask, c: Color, y0: number, y1: number) {
  for (let y = Math.max(0, y0); y <= Math.min(mask.h - 1, y1); y++) {
    for (let x = 0; x < mask.w; x++) {
      if (mask.has(x, y)) continue;
      if (mask.has(x - 1, y) || mask.has(x + 1, y) || mask.has(x, y - 1) || mask.has(x, y + 1)) px.put(ox + x, oy + y, c);
    }
  }
}

// --- медуза ---------------------------------------------------------------------------------------------------
function jellyPalette(disco: boolean) {
  return disco
    ? {
        outline: rgb("#2b0c5e"),
        base: rgb("#8b45f7"),
        light: rgb("#a86bff"),
        lighter: rgb("#c193ff"),
        ring: rgb("#dcb8ff"),
        ringHi: rgb("#f3e2ff"),
        white: rgb("#fdf6ff"),
        shadow: rgb("#7330e3"),
        deep: rgb("#5a1fc0"),
        inner: rgb("#6a29d6"),
        rim: rgb("#ff8fdc"),
        rimGap: rgb("#4a1699"),
        tent: rgb("#f472d0"),
        tentHi: rgb("#ffbdf0"),
        tentDark: rgb("#c03a9f"),
        tentTip: rgb("#fff0fb"),
        arm: rgb("#9b5cff"),
        armDark: rgb("#6a2bd6"),
        orb: rgb("#ff9be6"),
        orbHi: rgb("#fff4fd"),
        orbDark: rgb("#d55bb7"),
        glow: rgb("#ff9be6"),
        face: {
          eye: rgb("#1e0838"),
          eyeHi: rgb("#ffffff"),
          eyeHi2: rgb("#8b66c9"),
          mouth: rgb("#2c0828"),
          tongue: rgb("#ff7aa6"),
          cheek: rgb("#ff79ae"),
          tear: rgb("#9fe3ff"),
        },
      }
    : {
        outline: rgb("#0f1a52"),
        base: rgb("#2f66f3"),
        light: rgb("#4f86ff"),
        lighter: rgb("#6c9cff"),
        ring: rgb("#8ab9ff"),
        ringHi: rgb("#c4e1ff"),
        white: rgb("#f3faff"),
        shadow: rgb("#2552d8"),
        deep: rgb("#1c3db1"),
        inner: rgb("#244ac7"),
        rim: rgb("#42e5f6"),
        rimGap: rgb("#19348f"),
        tent: rgb("#33d8f0"),
        tentHi: rgb("#a6f8ff"),
        tentDark: rgb("#1595bc"),
        tentTip: rgb("#e7fdff"),
        arm: rgb("#3b70ff"),
        armDark: rgb("#2347c6"),
        orb: rgb("#3ef0dc"),
        orbHi: rgb("#f0fffd"),
        orbDark: rgb("#13b2a4"),
        glow: rgb("#4ff7e6"),
        face: {
          eye: rgb("#0c1230"),
          eyeHi: rgb("#ffffff"),
          eyeHi2: rgb("#5a6db8"),
          mouth: rgb("#2a0a26"),
          tongue: rgb("#ff7aa6"),
          cheek: rgb("#ff6f99"),
          tear: rgb("#9fe3ff"),
        },
      };
}
const JELLY = jellyPalette(false);
const JELLY_DISCO = jellyPalette(true);

function drawJelly(px: Pixels, ox: number, oy: number, p: Pose): Drawn {
  const P = p.disco ? JELLY_DISCO : JELLY;
  const s = clamp(p.squash, -1, 1);
  const put = (x: number, y: number, c: Color) => px.put(ox + x, oy + y, c);
  const bottom = SPRITE_H - 1;
  const tentLen = Math.max(5, Math.round(14 * (1 - 0.36 * s) + p.dangle * 3));
  const domeH = Math.max(12, Math.round(17 * (1 - 0.11 * s)));
  const R = 15 + (s > 0.3 ? 1 : 0) + (s > 0.75 ? 1 : 0) - (s < -0.45 ? 1 : 0);
  const rim = bottom - tentLen;
  const top = rim - domeH + 1;
  const lean = Math.round(clamp(p.sway, -1, 1));
  const cx = C + lean; // непрерывный центр
  const fx = cx - 1; // опорный столбец лица

  // щупальца — позади купола; «покадровая» волна: 10 кадров в секунду
  const ty = rim + 1;
  const len = bottom - rim;
  const q = Math.floor(p.t * 10) / 10;
  const speed = p.dangle > 0.5 ? 3.4 : 5.6;
  const wig = p.wiggle;
  for (const [bx, ph] of [
    [cx - 3, 0],
    [cx + 1, Math.PI],
  ] as const) {
    for (let j = 0; j < len - 2; j++) {
      const amp = (0.5 + j * 0.16) * wig;
      const x = bx + Math.round(Math.sin(q * speed + ph - j * 0.55) * amp - lean * (j / len));
      put(x, ty + j, P.arm);
      put(x + 1, ty + j, P.armDark);
    }
  }
  for (const [bx, ph, dir] of [
    [cx - 11, 0.3, -1],
    [cx - 6, 1.9, -1],
    [cx + 5, 3.8, 1],
    [cx + 10, 5.2, 1],
  ] as const) {
    for (let j = 0; j < len; j++) {
      const amp = (0.3 + j * 0.12) * wig;
      const splay = Math.max(0, s) * j * 0.32 * dir;
      const x = bx + Math.round(Math.sin(q * speed + ph - j * 0.62) * amp + splay - lean * (j / len));
      const tip = j === len - 1;
      put(x, ty + j, tip ? P.tentTip : j % 4 === 1 ? P.tentHi : P.tent);
      if (j < 4) put(x - dir, ty + j, P.tentDark);
    }
  }

  // купол
  const dome = new Mask(SPRITE_W, SPRITE_H);
  const rows = rim - top + 1;
  const k = 0.64;
  for (let y = top; y <= rim; y++) {
    const u = (y - top + 0.5) / rows;
    const hw = u < k ? R * Math.sqrt(Math.max(0, 1 - ((k - u) / k) ** 2)) : R;
    for (let x = Math.ceil(cx - 0.5 - hw); x <= Math.floor(cx - 0.5 + hw); x++) dome.set(x, y);
  }
  for (let y = top; y <= rim; y++) {
    const u = (y - top) / Math.max(1, rows - 1);
    for (let x = 0; x < SPRITE_W; x++) {
      if (!dome.has(x, y)) continue;
      const nx = (x + 0.5 - cx) / R;
      let c = P.base;
      const d = nx * 0.8 + u * 0.95;
      if (d > 1.18) c = P.deep;
      else if (d > 0.86) c = P.shadow;
      if ((!dome.has(x - 1, y) || !dome.has(x, y - 1)) && nx < 0.4 && u < 0.72) c = P.light;
      if (y === rim - 1) c = nx > 0.05 ? P.deep : P.shadow;
      if (y === rim) c = Math.floor(Math.abs(x + 0.5 - cx)) % 3 === 2 ? P.rimGap : P.rim;
      put(x, y, c);
    }
  }
  outline(px, ox, oy, dome, P.outline, top - 1, rim);

  // кольца-завиток на макушке и блик
  const rcy = top + Math.max(4, Math.round(domeH * 0.36));
  const inRing = (x: number, y: number) => dome.has(x, y) && dome.has(x, y - 1) && y < rim - 1;
  for (let y = rcy - 2; y <= rcy + 2; y++) {
    for (let x = Math.floor(cx - 4); x <= Math.ceil(cx + 4); x++) {
      const e = ((x + 0.5 - cx) / 3.7) ** 2 + ((y + 0.5 - (rcy + 0.5)) / 1.9) ** 2;
      if (e <= 1 && inRing(x, y)) put(x, y, P.inner);
    }
  }
  const ring = (rx: number, ry: number, c: Color, keep: (a: number) => boolean) => {
    const steps = Math.ceil(Math.max(rx, ry) * 9);
    for (let i = 0; i < steps; i++) {
      const a = (i / steps) * Math.PI * 2;
      if (!keep(a)) continue;
      const x = Math.floor(cx + Math.cos(a) * rx);
      const y = Math.floor(rcy + 0.5 + Math.sin(a) * ry);
      if (inRing(x, y)) put(x, y, c);
    }
  };
  const spin = p.t * 0.7;
  ring(3.8, 1.9, P.ringHi, () => true);
  ring(7, 3.4, P.ring, (a) => angleDist(a, spin) > 0.55);
  ring(10.4, 5.1, P.lighter, (a) => Math.sin(a) < 0.2 && angleDist(a, spin + 2.4) > 0.7);
  const hx = Math.floor(cx - R * 0.58);
  const hy = top + 3;
  put(hx, hy, P.white);
  put(hx + 1, hy, P.ringHi);
  put(hx - 1, hy + 1, P.ringHi);
  put(hx, hy + 1, P.ringHi);

  // лицо
  const ey = top + Math.round(domeH * 0.6);
  drawFace(px, ox, oy, fx, ey, p, P.face);

  // антенна: светящийся шарик парит над макушкой
  const bob = Math.round(Math.sin(p.t * 2.4) * 0.8);
  const orbY = top - 6 + bob;
  const glow = p.glow * (0.65 + 0.35 * Math.sin(p.t * 3.1));
  if (glow > 0.02) {
    // мягкий ореол: только «плюс» вокруг шарика — без квадратной рамки
    for (const [dx, dy] of [
      [-1, 0],
      [2, 0],
      [0, -1],
      [1, -1],
      [-1, 3],
      [2, 3],
      [0, 4],
      [1, 4],
      [-2, 1],
      [-2, 2],
      [3, 1],
      [3, 2],
    ] as const) {
      const far = dx === -2 || dx === 3 || dy === -1 || dy === 4;
      px.put(ox + fx + dx, oy + orbY + dy, fade(P.glow, (far ? 0.16 : 0.3) * glow), false);
    }
  }
  stamp(px, [".dd.", "dHod", "dood", ".dd."], ox + fx - 1, oy + orbY, { d: P.orbDark, o: P.orb, H: P.orbHi }, { solid: true });

  if (p.headphones) drawHeadphones(px, ox, oy, dome, top, ey - 3);
  return { headTop: Math.min(orbY - 1, top - 4), mouthY: ey + 3, centerY: top + Math.round(domeH / 2) };
}

// --- призрак ----------------------------------------------------------------------------------------------------
const GHOST = {
  outline: rgb("#2b2e58"),
  body: rgb("#eef1ff"),
  light: rgb("#ffffff"),
  shade: rgb("#ccd2f5"),
  deep: rgb("#a9b1e7"),
  shadow: rgb("#05060c"),
  face: {
    eye: rgb("#1c1f40"),
    eyeHi: rgb("#ffffff"),
    eyeHi2: rgb("#7a83c2"),
    mouth: rgb("#2a1030"),
    tongue: rgb("#ff7aa6"),
    cheek: rgb("#ff9cc0"),
    tear: rgb("#8fd8ff"),
  },
};

function drawGhost(px: Pixels, ox: number, oy: number, p: Pose): Drawn {
  const s = clamp(p.squash, -1, 1);
  const put = (x: number, y: number, c: Color) => px.put(ox + x, oy + y, c);
  const bottom = SPRITE_H - 1;
  const hover = p.dangle > 0.5 ? 1 : 4 + Math.round(Math.sin(p.t * 2.2) * 1.3);
  // тень на «полу»
  const shadowA = Math.max(0.08, 0.3 - hover * 0.03);
  for (let x = C - 8; x < C + 8; x++) px.put(ox + x, oy + bottom, fade(GHOST.shadow, shadowA * (Math.abs(x + 0.5 - C) < 6 ? 1 : 0.5)), false);
  const bodyBottom = bottom - 1 - hover;
  const H = Math.max(18, Math.round(29 * (1 - 0.12 * s)));
  const R = 12 + (s > 0.4 ? 1 : 0) - (s < -0.5 ? 1 : 0);
  const top = bodyBottom - H + 1;
  const lean = Math.round(clamp(p.sway, -1, 1));
  const cx = C + lean;
  const body = new Mask(SPRITE_W, SPRITE_H);
  const q = Math.floor(p.t * 10) / 10;
  for (let y = top; y <= bodyBottom; y++) {
    const u = (y - top + 0.5) / H;
    const hw = u < 0.46 ? R * Math.sqrt(Math.max(0, 1 - ((0.46 - u) / 0.46) ** 2)) : R + (u > 0.82 ? 1 : 0);
    for (let x = Math.ceil(cx - 0.5 - hw); x <= Math.floor(cx - 0.5 + hw); x++) {
      // волнистый подол
      const wave = Math.round(1.4 + 1.4 * Math.sin((x - cx) * 0.95 - q * 6 * p.wiggle));
      if (y > bodyBottom - wave) continue;
      body.set(x, y);
    }
  }
  // лапки
  const armY = top + Math.round(H * 0.52) - Math.round(p.arms * 3);
  const left = Math.ceil(cx - 0.5 - R);
  const right = Math.floor(cx - 0.5 + R);
  for (let i = 0; i < 2; i++) {
    body.set(left - 1, armY + i);
    body.set(left - 2, armY + i - (p.arms > 0.5 ? 1 : 0));
    body.set(right + 1, armY + i);
    body.set(right + 2, armY + i - (p.arms > 0.5 ? 1 : 0));
  }
  for (let y = 0; y < SPRITE_H; y++) {
    for (let x = 0; x < SPRITE_W; x++) {
      if (!body.has(x, y)) continue;
      const nx = (x + 0.5 - cx) / R;
      const u = (y - top) / H;
      let c = GHOST.body;
      if (nx > 0.78 || (u > 0.86 && nx > -0.2)) c = GHOST.deep;
      else if (nx > 0.5 || u > 0.84) c = GHOST.shade;
      if ((!body.has(x - 1, y) || !body.has(x, y - 1)) && nx < 0.2 && u < 0.6) c = GHOST.light;
      put(x, y, c);
    }
  }
  outline(px, ox, oy, body, GHOST.outline, top - 1, bodyBottom + 1);
  const ey = top + Math.round(H * 0.42);
  drawFace(px, ox, oy, cx - 1, ey, p, GHOST.face, 1);
  if (p.headphones) drawHeadphones(px, ox, oy, body, top, ey - 3);
  return { headTop: top - (p.headphones ? 4 : 1), mouthY: ey + 3, centerY: top + Math.round(H / 2) };
}

// --- котик -----------------------------------------------------------------------------------------------------
const CAT = {
  outline: rgb("#4a2410"),
  base: rgb("#f6a24e"),
  light: rgb("#ffc987"),
  shade: rgb("#dd8432"),
  deep: rgb("#bf6a22"),
  stripe: rgb("#c76a1f"),
  belly: rgb("#fff0db"),
  ear: rgb("#ff9fb7"),
  nose: rgb("#ff7d9b"),
  whisker: rgb("#fff3e0", 0.85),
  face: {
    eye: rgb("#2a1408"),
    eyeHi: rgb("#ffffff"),
    eyeHi2: rgb("#8a5a3a"),
    mouth: rgb("#4a1d10"),
    tongue: rgb("#ff7aa6"),
    cheek: rgb("#ff8fa6"),
    tear: rgb("#8fd8ff"),
  },
};

function drawCat(px: Pixels, ox: number, oy: number, p: Pose): Drawn {
  const s = clamp(p.squash, -1, 1);
  const put = (x: number, y: number, c: Color) => px.put(ox + x, oy + y, c);
  const bottom = SPRITE_H - 1;
  const bodyBottom = bottom - 2;
  const H = Math.max(14, Math.round(21 * (1 - 0.1 * s)));
  const R = 13 + (s > 0.4 ? 1 : 0);
  const top = bodyBottom - H + 1;
  const lean = Math.round(clamp(p.sway, -1, 1));
  const cx = C + lean;
  const back = p.facing >= 0 ? -1 : 1; // хвост — со стороны спины

  // хвост (позади тела)
  const edge = back < 0 ? Math.ceil(cx - 0.5 - R) - 1 : Math.floor(cx - 0.5 + R) + 1;
  const q = Math.floor(p.t * 8) / 8;
  for (let j = 0; j < 9; j++) {
    const x = edge + back * (Math.min(j, 3) + Math.round(1.4 * Math.sin(j * 0.45 + q * 3.2 * p.wiggle)));
    const y = bodyBottom - 3 - j;
    put(x, y, j === 8 ? CAT.deep : CAT.base);
    put(x + back, y, CAT.shade);
    put(x - back, y, CAT.outline);
    put(x + 2 * back, y, CAT.outline);
    if (j === 8) {
      put(x, y - 1, CAT.outline);
      put(x + back, y - 1, CAT.outline);
    }
  }

  // тело-«булочка» и уши
  const body = new Mask(SPRITE_W, SPRITE_H);
  for (let y = top; y <= bodyBottom; y++) {
    const u = (y - top + 0.5) / H;
    const hw = u < 0.36 ? R * Math.sqrt(Math.max(0, 1 - ((0.36 - u) / 0.36) ** 2 * 0.62)) : R;
    for (let x = Math.ceil(cx - 0.5 - hw); x <= Math.floor(cx - 0.5 + hw); x++) body.set(x, y);
  }
  const earRows = ["#", "##", "###", "####"];
  const earL = Math.round(cx - 12);
  const earR = Math.round(cx + 11); // зеркально earL
  for (let r = 0; r < earRows.length; r++) {
    for (let i = 0; i < earRows[r].length; i++) {
      body.set(earL + i, top - 4 + r);
      body.set(earR - i, top - 4 + r);
    }
  }
  for (let y = 0; y < SPRITE_H; y++) {
    for (let x = 0; x < SPRITE_W; x++) {
      if (!body.has(x, y)) continue;
      const nx = (x + 0.5 - cx) / R;
      const u = (y - top) / H;
      let c = CAT.base;
      const d = nx * 0.8 + u * 0.9;
      if (d > 1.15) c = CAT.deep;
      else if (d > 0.82) c = CAT.shade;
      if ((!body.has(x - 1, y) || !body.has(x, y - 1)) && nx < 0.3 && u < 0.6 && y >= top) c = CAT.light;
      put(x, y, c);
    }
  }
  // внутренность ушей, полоски, животик
  put(earL + 1, top - 2, CAT.ear);
  put(earL + 1, top - 1, CAT.ear);
  put(earL + 2, top - 1, CAT.ear);
  put(earR - 1, top - 2, CAT.ear);
  put(earR - 1, top - 1, CAT.ear);
  put(earR - 2, top - 1, CAT.ear);
  for (const x of [cx - 3, cx - 1, cx, cx + 2]) {
    const len = x === cx - 1 || x === cx ? 3 : 2;
    for (let i = 0; i < len; i++) put(x, top + 1 + i, CAT.stripe);
  }
  for (let y = bodyBottom - 5; y <= bodyBottom; y++) {
    for (let x = Math.floor(cx - 8); x <= Math.ceil(cx + 8); x++) {
      const e = ((x + 0.5 - cx) / 7.5) ** 2 + ((y + 0.5 - (bodyBottom + 0.5)) / 5) ** 2;
      if (e <= 1 && body.has(x, y)) put(x, y, CAT.belly);
    }
  }
  outline(px, ox, oy, body, CAT.outline, top - 5, bodyBottom + 1);

  // лапки: при ходьбе пары поочерёдно приподнимаются
  const step = p.walk >= 0 ? (Math.sin(p.walk * Math.PI * 2) > 0 ? 1 : 2) : 0;
  const paws: [number, number][] = [
    [cx - 10, 1],
    [cx - 5, 2],
    [cx + 3, 1],
    [cx + 8, 2],
  ];
  for (const [x, pair] of paws) {
    const lift = step === pair ? 1 : 0;
    const y = bottom - 1 - lift;
    put(x, y, CAT.belly);
    put(x + 1, y, CAT.belly);
    put(x, y + 1, CAT.outline);
    put(x + 1, y + 1, CAT.outline);
    put(x - 1, y, CAT.outline);
    put(x + 2, y, CAT.outline);
  }

  // мордочка: носик, усы
  const ey = top + Math.round(H * 0.44);
  drawFace(px, ox, oy, cx - 1, ey, p, CAT.face, 1);
  put(cx - 1, ey + 2, CAT.nose);
  put(cx, ey + 2, CAT.nose);
  const wl = Math.ceil(cx - 0.5 - R);
  const wr = Math.floor(cx - 0.5 + R);
  for (const dy of [2, 4]) {
    for (let i = 1; i <= 3; i++) {
      put(wl - i, ey + dy + (i === 3 ? (dy === 2 ? -1 : 1) : 0), CAT.whisker);
      put(wr + i, ey + dy + (i === 3 ? (dy === 2 ? -1 : 1) : 0), CAT.whisker);
    }
  }
  if (p.headphones) drawHeadphones(px, ox, oy, body, top, ey - 3);
  return { headTop: top - 5, mouthY: ey + 3, centerY: top + Math.round(H / 2) };
}

export interface SkinDef {
  draw: (px: Pixels, ox: number, oy: number, pose: Pose) => Drawn;
  /** Скорость прогулки, арт-пикселей в секунду. */
  speed: number;
  /** Ходит вприпрыжку (медуза) или плывёт/шагает ровно. */
  hops: boolean;
}

export const SKINS: Record<SkinId, SkinDef> = {
  jelly: { draw: drawJelly, speed: 12, hops: true },
  ghost: { draw: drawGhost, speed: 10, hops: false },
  cat: { draw: drawCat, speed: 15, hops: false },
};

// --- частицы ----------------------------------------------------------------------------------------------------
export type ParticleKind = "note" | "note2" | "heart" | "z" | "spark" | "crumb" | "poof" | "dust";

export interface Particle {
  kind: ParticleKind;
  x: number;
  y: number;
  vx: number;
  vy: number;
  age: number;
  life: number;
  color: Color;
  gravity: number;
  wobble: number;
}

const BITMAPS: Partial<Record<ParticleKind, string[]>> = {
  note: ["..##.", "..#.#", "..#..", "###..", "###.."],
  note2: ["..#####", "..#...#", "..#...#", "###.###", "###.###"],
  heart: [".##.##.", "#h#####", "#######", ".#####.", "..###..", "...#..."],
  z: ["#####", "...#.", "..#..", ".#...", "#####"],
  spark: [".#.", "#h#", ".#."],
};

const HI = rgb("#ffffff", 0.9);

export function drawParticles(px: Pixels, list: Particle[]) {
  for (const p of list) {
    const k = p.age / p.life;
    const alpha = k < 0.12 ? k / 0.12 : k > 0.62 ? Math.max(0, 1 - (k - 0.62) / 0.38) : 1;
    const x = Math.round(p.x + (p.wobble ? Math.sin(p.age * 5 + p.wobble) * 1.4 : 0));
    const y = Math.round(p.y);
    const bitmap = BITMAPS[p.kind];
    if (bitmap) {
      stamp(px, bitmap, x - Math.floor(bitmap[0].length / 2), y - Math.floor(bitmap.length / 2), { "#": p.color, h: HI }, { alpha });
    } else if (p.kind === "poof") {
      const r = 1 + k * 3;
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const d = Math.hypot(dx, dy);
          if (d <= r && d > r - 1.2) px.put(x + dx, y + dy, fade(p.color, alpha * 0.9), false);
        }
      }
    } else {
      px.put(x, y, fade(p.color, alpha), false);
      if (p.kind === "crumb" && p.wobble > 1) px.put(x + 1, y, fade(p.color, alpha), false);
    }
  }
}

/** «!» над головой — пиксельное облачко. */
export function drawBang(px: Pixels, x: number, y: number, alpha: number) {
  stamp(
    px,
    [".ooooooo.", "oWWWWWWWo", "oWWWrWWWo", "oWWWrWWWo", "oWWWrWWWo", "oWWWWWWWo", "oWWWrWWWo", "oWWWWWWWo", ".ooWooooo", "..oWo....", "...o....."],
    x,
    y,
    { o: rgb("#1b2040"), W: rgb("#ffffff"), r: rgb("#ff4d6d") },
    { alpha },
  );
}
