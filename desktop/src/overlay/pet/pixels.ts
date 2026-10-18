// Пиксельный холст питомца: буфер RGBA в «арт-пикселях» (рисуем по точке) и маска пикселей самого спрайта —
// по ней мышь попадает в питомца, а не в прозрачный прямоугольник вокруг. На экран буфер выводится одним
// drawImage с целочисленным увеличением без сглаживания.

/** Цвет: 0xAABBGGRR (так байты RGBA лежат в Uint32Array на little-endian). */
export type Color = number;

export function rgb(hex: string, alpha = 1): Color {
  const n = Number.parseInt(hex.replace("#", "").slice(0, 6), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  const a = Math.round(Math.max(0, Math.min(1, alpha)) * 255);
  return ((a << 24) | (b << 16) | (g << 8) | r) >>> 0;
}

export function fade(c: Color, alpha: number): Color {
  const a = Math.round(((c >>> 24) / 255) * Math.max(0, Math.min(1, alpha)) * 255);
  return ((c & 0x00ffffff) | (a << 24)) >>> 0;
}

function blend(dst: number, src: number): number {
  const sa = (src >>> 24) / 255;
  if (sa <= 0) return dst;
  const da = (dst >>> 24) / 255;
  const oa = sa + da * (1 - sa);
  if (oa <= 0) return 0;
  const mix = (shift: number) => {
    const s = (src >>> shift) & 255;
    const d = (dst >>> shift) & 255;
    return Math.round((s * sa + d * da * (1 - sa)) / oa) & 255;
  };
  return ((Math.round(oa * 255) << 24) | (mix(16) << 16) | (mix(8) << 8) | mix(0)) >>> 0;
}

export class Pixels {
  readonly image: ImageData;
  readonly data: Uint32Array;
  /** 1 — пиксель спрайта (попадание мышью), частицы сюда не пишутся. */
  readonly solid: Uint8Array;

  constructor(
    readonly w: number,
    readonly h: number,
  ) {
    this.image = new ImageData(w, h);
    this.data = new Uint32Array(this.image.data.buffer);
    this.solid = new Uint8Array(w * h);
  }

  clear() {
    this.data.fill(0);
    this.solid.fill(0);
  }

  put(x: number, y: number, c: Color, solid = true) {
    x = Math.floor(x);
    y = Math.floor(y);
    if (x < 0 || y < 0 || x >= this.w || y >= this.h || !(c >>> 24)) return;
    const i = y * this.w + x;
    this.data[i] = c >>> 24 === 255 ? c : blend(this.data[i], c);
    if (solid) this.solid[i] = 1;
  }

  rect(x: number, y: number, w: number, h: number, c: Color, solid = true) {
    for (let yy = 0; yy < h; yy++) for (let xx = 0; xx < w; xx++) this.put(x + xx, y + yy, c, solid);
  }

  /** Попадание с допуском в r пикселей. */
  hit(x: number, y: number, r = 1): boolean {
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        const xx = x + dx;
        const yy = y + dy;
        if (xx >= 0 && yy >= 0 && xx < this.w && yy < this.h && this.solid[yy * this.w + xx]) return true;
      }
    }
    return false;
  }
}

/** Маска фигуры (купол, тело) — для заливки, контура и наушников по её краю. */
export class Mask {
  readonly bits: Uint8Array;
  constructor(
    readonly w: number,
    readonly h: number,
  ) {
    this.bits = new Uint8Array(w * h);
  }
  set(x: number, y: number) {
    if (x >= 0 && y >= 0 && x < this.w && y < this.h) this.bits[y * this.w + x] = 1;
  }
  has(x: number, y: number): boolean {
    return x >= 0 && y >= 0 && x < this.w && y < this.h && this.bits[y * this.w + x] === 1;
  }
  /** Евклидово расстояние до ближайшего пикселя маски (до limit). */
  distance(x: number, y: number, limit = 4): number {
    let best = limit + 1;
    for (let dy = -limit; dy <= limit; dy++) {
      for (let dx = -limit; dx <= limit; dx++) {
        if (!this.has(x + dx, y + dy)) continue;
        const d = Math.hypot(dx, dy);
        if (d < best) best = d;
      }
    }
    return best;
  }
}

/** Картинка из строк: символ → цвет (точка — прозрачно). */
export function stamp(
  px: Pixels,
  rows: readonly string[],
  x: number,
  y: number,
  colors: Record<string, Color>,
  opts: { solid?: boolean; alpha?: number; flip?: boolean } = {},
) {
  const { solid = false, alpha = 1, flip = false } = opts;
  for (let r = 0; r < rows.length; r++) {
    const row = rows[r];
    for (let c = 0; c < row.length; c++) {
      const color = colors[row[c]];
      if (color === undefined) continue;
      const cx = flip ? row.length - 1 - c : c;
      px.put(x + cx, y + r, alpha >= 1 ? color : fade(color, alpha), solid);
    }
  }
}
