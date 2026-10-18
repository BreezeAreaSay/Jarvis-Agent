// «Мозг» питомца: где он стоит (низ рабочей области или верхний край окна), что делает и с каким лицом.
// Состояния: покой (дышит), прогулка, прыжок по дуге между краями окон, падение, танец под музыку
// (наушники, ноты), сон после долгого покоя (Zz), «!» на «Джарвис», поход к Корзине и «ням-ням»,
// перетаскивание мышью. Всё в CSS-пикселях слоя питомца; частицы — в арт-пикселях холста.
import { rgb } from "./pixels";
import { CANVAS_W, type Eyes, FEET_Y, type Mouth, type Particle, type ParticleKind, type Pose } from "./sprites";

export interface Platform {
  key: string;
  win: number;
  left: number; // левый край окна (по нему видно, что окно сдвинули)
  x1: number; // видимая часть верхнего края
  x2: number;
  y: number;
  fg: boolean;
}

export interface Env {
  width: number;
  height: number;
  floor: number;
  platforms: Platform[];
  recycle: number | null;
  music: boolean;
  cs: number; // CSS-пикселей на арт-пиксель
  speed: number; // арт-пикселей в секунду
  hops: boolean;
  eats: boolean;
  onWindows: boolean;
  trashRight: number; // правый край значка Корзины
}

export type Mode =
  | "idle"
  | "walk"
  | "jump"
  | "fall"
  | "dance"
  | "sleep"
  | "drag"
  | "eatgo"
  | "eatask"
  | "eatwait"
  | "munch"
  | "poof";

export interface Bubble {
  kind: "ask" | "say";
  text: string;
  until: number;
}

interface Jump {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  dur: number;
  h: number;
  to: Platform | null;
  then: Mode;
}

const SLEEP_AFTER_S = 90;
const NOTE_COLORS = [rgb("#ff7ac8"), rgb("#ffd257"), rgb("#a5b4fc"), rgb("#7ff3ff")];
const HEART = rgb("#ff5c9a");
const ZED = rgb("#bcd7ff");
const SPARK = rgb("#7ff3ff");
const DUST = rgb("#8f97ad");
const CRUMBS = [rgb("#f4f1e8"), rgb("#c9c2b0"), rgb("#ffd257"), rgb("#9fb7ff")];
const POOF = rgb("#dfe4f5");

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
const lerp = (a: number, b: number, k: number) => a + (b - a) * k;
const rand = (a: number, b: number) => a + Math.random() * (b - a);

export class PetBrain {
  x = -1;
  y = -1;
  facing: 1 | -1 = 1;
  surface: Platform | null = null;
  mode: Mode = "idle";
  modeT = 0;
  modeDur = 2;
  t = 0;
  targetX = 0;
  jump: Jump | null = null;
  vy = 0;

  // поза
  hop = 0;
  squashBase = 0;
  spring = 0;
  springV = 0;
  sway = 0;
  walkPhase = 0;
  look = 0;
  idleFace: "open" | "happy" = "happy";
  emote: { eyes?: Eyes; mouth?: Mouth; until: number } | null = null;
  bangUntil = 0;
  hopAt = -10;
  blinkAt = 3;
  blinkUntil = 0;

  // музыка, сон, еда
  music = false;
  headphones = false;
  danceUntil = 0;
  restUntil = 0;
  lastActive = 0;
  eatDirect = false;
  eatQueued = false;
  pendingMunch = false;
  nextEatAt = 60;
  trash = false;
  shakeUntil = 0;
  bubble: Bubble | null = null;
  hearts = 0;
  nextHeart = 0;
  noteAt = 0;
  zAt = 0;

  particles: Particle[] = [];
  /** Голова/рот на холсте (арт-пиксели) — из последней отрисовки, для частиц. */
  head = { top: 40, mouth: 70, center: 76 };
  attention = false;
  frozen = false;
  gone = false;
  onFeed: (() => void) | null = null;
  private grab = { dx: 0, dy: 0 };

  // --- служебное ---------------------------------------------------------------------------------------
  private margin(env: Env) {
    return 16 * env.cs + 6;
  }

  setMode(mode: Mode, dur = 2) {
    this.mode = mode;
    this.modeT = 0;
    this.modeDur = dur;
    if (mode === "idle") this.idleFace = Math.random() < 0.55 ? "happy" : "open";
    if (mode !== "dance") this.sway = 0;
  }

  private spawn(env: Env) {
    this.x = env.width * rand(0.25, 0.75);
    this.y = env.floor;
    this.lastActive = this.t;
    this.nextEatAt = this.t + rand(45, 90);
    this.setMode("idle", 2);
    this.burst("spark", 6);
  }

  private add(kind: ParticleKind, x: number, y: number, vx: number, vy: number, life: number, color: number, gravity = 0, wobble = 0) {
    if (this.particles.length > 60) this.particles.shift();
    this.particles.push({ kind, x, y, vx, vy, age: 0, life, color, gravity, wobble });
  }

  burst(kind: "spark" | "poof" | "dust" | "heart", n: number) {
    const cx = CANVAS_W / 2;
    for (let i = 0; i < n; i++) {
      if (kind === "spark") this.add("spark", cx + rand(-16, 16), this.head.top + rand(0, 16), rand(-6, 6), rand(-10, -2), rand(0.5, 0.9), SPARK);
      else if (kind === "poof") this.add("poof", cx + rand(-12, 12), this.head.center + rand(-10, 10), rand(-4, 4), rand(-6, 0), rand(0.4, 0.6), POOF);
      else if (kind === "dust") this.add("dust", cx + (i % 2 ? 1 : -1) * rand(6, 14), FEET_Y - rand(0, 2), (i % 2 ? 1 : -1) * rand(8, 16), rand(-4, -1), rand(0.3, 0.5), DUST);
      else this.add("heart", cx + rand(-12, 12), this.head.top + rand(-2, 6), rand(-3, 3), rand(-12, -8), rand(1.2, 1.7), HEART, 0, rand(0, 6));
    }
  }

  // --- главный шаг ---------------------------------------------------------------------------------------
  update(dt: number, env: Env) {
    this.t += dt;
    this.modeT += dt;
    if (this.x < 0) this.spawn(env);
    if (this.bubble && this.t > this.bubble.until) this.bubble = null;
    if (this.emote && this.t > this.emote.until) this.emote = null;
    if (this.t > this.blinkAt) {
      this.blinkUntil = this.t + 0.13;
      this.blinkAt = this.t + rand(2.4, 6);
    }
    this.syncMusic(env.music);
    this.followSurface(env);
    this.hop = 0;
    this.squashBase = 0;

    switch (this.mode) {
      case "idle":
        this.breathe(env);
        if (!this.frozen && !this.attention && this.modeT >= this.modeDur) this.decide(env);
        break;
      case "walk":
      case "eatgo":
        this.walk(dt, env);
        break;
      case "jump":
        this.updateJump();
        break;
      case "fall":
        this.updateFall(dt, env);
        break;
      case "dance":
        this.dance(env);
        break;
      case "sleep":
        this.squashBase = 0.4 + 0.08 * Math.sin(this.t * 1.7);
        if (this.t >= this.zAt) {
          this.zAt = this.t + 1.5;
          this.add("z", CANVAS_W / 2 + 9, this.head.top + 4, 3.5, -6, 2.4, ZED, 0, 1);
        }
        if (!this.frozen && this.modeT > 240) this.setMode("idle", 3);
        break;
      case "drag":
        break;
      case "eatask":
      case "eatwait":
        this.breathe(env);
        this.look = -1;
        if (!this.frozen && this.modeT > this.modeDur) this.giveUp();
        break;
      case "munch":
        this.squashBase = 0.18 + 0.14 * Math.abs(Math.sin(this.t * 11));
        this.look = -1;
        if (Math.random() < dt * 9) {
          const c = CRUMBS[Math.floor(Math.random() * CRUMBS.length)];
          this.add("crumb", CANVAS_W / 2 + rand(-3, 3), this.head.mouth, rand(-14, 14), rand(-14, -6), rand(0.6, 0.9), c, 60, rand(0, 2));
        }
        if (this.modeT > this.modeDur) {
          this.hearts = 3;
          this.emote = { eyes: "happy", mouth: "open", until: this.t + 1.8 };
          this.shakeUntil = 0;
          this.trash = false;
          this.nextEatAt = this.t + rand(300, 480);
          this.setMode("idle", 2.5);
        }
        break;
      case "poof":
        if (this.modeT > 0.5) this.gone = true;
        break;
    }
    if (this.eatQueued && this.mode !== "jump" && this.mode !== "fall") {
      this.eatQueued = false;
      if (this.trash) this.goEat(env);
    }

    // «!» — подскок на месте
    const k = (this.t - this.hopAt) / 0.45;
    if (k >= 0 && k <= 1) this.hop += 7 * Math.sin(Math.PI * k);

    // пружина приземлений
    const a = -190 * this.spring - 13 * this.springV;
    this.springV += a * dt;
    this.spring += this.springV * dt;

    // сердечки по одному
    if (this.hearts > 0 && this.t >= this.nextHeart) {
      this.hearts--;
      this.nextHeart = this.t + 0.2;
      this.burst("heart", 1);
    }
    // ноты в танце
    if (this.mode === "dance" && this.t >= this.noteAt) {
      this.noteAt = this.t + rand(0.35, 0.55);
      const side = Math.random() < 0.5 ? -1 : 1;
      const kind = Math.random() < 0.35 ? "note2" : "note";
      const color = NOTE_COLORS[Math.floor(Math.random() * NOTE_COLORS.length)];
      this.add(kind, CANVAS_W / 2 + side * rand(12, 20), this.head.top + rand(4, 10), side * rand(2, 6), rand(-14, -9), rand(1.6, 2.2), color, 0, rand(0, 6));
    }
    for (const p of this.particles) {
      p.age += dt;
      p.vy += p.gravity * dt;
      p.x += p.vx * dt;
      p.y += p.vy * dt;
    }
    if (this.particles.length) this.particles = this.particles.filter((p) => p.age < p.life);
  }

  private breathe(env: Env) {
    if (env.hops) {
      this.squashBase = 0.12 * Math.sin(this.t * 2.4);
      this.hop = 1.2 + 1.2 * Math.sin(this.t * 2.4 - 0.9);
    } else {
      this.squashBase = 0.05 * Math.sin(this.t * 2);
    }
    if (this.music) {
      // в паузах между танцами — покачивается в такт
      const beat = this.t * 2.1;
      this.hop += Math.abs(Math.sin(Math.PI * beat)) * 1.5;
    }
    if (!this.attention && this.mode === "idle" && Math.floor(this.t / 3) % 2 === 0) this.look = 0;
  }

  private decide(env: Env) {
    if (this.music && this.t >= this.restUntil) return this.startDance();
    if (this.wantsToEat(env)) return this.startEat(false, env);
    if (this.t - this.lastActive > SLEEP_AFTER_S && Math.random() < 0.55) return this.setMode("sleep", 0);
    const r = Math.random();
    const others = env.onWindows ? env.platforms.filter((p) => p.key !== this.surface?.key) : [];
    if (this.surface) {
      if (r < 0.4) return this.walkOn(env);
      if (r < 0.62) return this.jumpToFloor(env, "idle");
      if (r < 0.8 && others.length) return this.jumpTo(env, this.pick(others), "idle");
      return this.setMode("idle", rand(2, 5));
    }
    if (r < 0.55) return this.walkOn(env);
    if (r < 0.82 && others.length) return this.jumpTo(env, this.pick(others), "idle");
    this.setMode("idle", rand(2, 6));
  }

  private pick(list: Platform[]): Platform {
    const weights = list.map((p) => (p.fg ? 2 : 1) / (1 + Math.abs((p.x1 + p.x2) / 2 - this.x) / 700));
    let r = Math.random() * weights.reduce((a, b) => a + b, 0);
    for (let i = 0; i < list.length; i++) {
      r -= weights[i];
      if (r <= 0) return list[i];
    }
    return list[list.length - 1];
  }

  private range(env: Env): [number, number] {
    const m = this.margin(env);
    if (this.surface) return [this.surface.x1 + m, this.surface.x2 - m];
    return [m, env.width - m];
  }

  private walkOn(env: Env) {
    const [a, b] = this.range(env);
    if (b - a < 40) return this.setMode("idle", 3);
    let tx = rand(a, b);
    if (Math.abs(tx - this.x) < 60) tx = clamp(this.x + (tx > this.x ? 1 : -1) * rand(60, 160), a, b);
    this.targetX = tx;
    this.setMode("walk", 30);
  }

  private walk(dt: number, env: Env) {
    if (this.mode === "eatgo" && this.surface) return this.goEat(env);
    const d = this.targetX - this.x;
    if (this.mode === "eatgo" && Math.abs(d) > 240 && this.modeT > 0.12) {
      // Корзина далеко — несётся к ней большими прыжками
      const hop = Math.sign(d) * Math.min(Math.abs(d) - 60, 400);
      return this.startJump(this.x + hop, env.floor, null, "eatgo");
    }
    const speed = env.speed * env.cs * (this.mode === "eatgo" ? 1.8 : 1);
    const step = Math.sign(d) * Math.min(Math.abs(d), speed * dt);
    this.x += step;
    if (Math.abs(d) > 0.5) this.facing = d > 0 ? 1 : -1;
    this.look = this.facing;
    this.walkPhase += Math.abs(step) / (env.cs * 8);
    if (env.hops) {
      const s = Math.abs(Math.sin(this.walkPhase * Math.PI));
      this.hop = 3 * s;
      this.squashBase = 0.34 * (1 - s) - 0.12;
    } else {
      this.squashBase = 0.03 * Math.sin(this.walkPhase * Math.PI * 2);
      this.sway = env.speed < 12 ? this.facing : 0; // призрак наклоняется по ходу
    }
    if (Math.abs(this.targetX - this.x) < 1 || this.modeT > this.modeDur) {
      this.sway = 0;
      if (this.mode === "eatgo") this.arriveAtTrash();
      else this.setMode("idle", rand(1.5, 4));
    }
  }

  private startJump(x1: number, y1: number, to: Platform | null, then: Mode) {
    const dist = Math.hypot(x1 - this.x, y1 - this.y);
    const dur = 0.45 + Math.min(0.6, dist / 1500);
    const h = Math.abs(this.y - y1) / 2 + rand(36, 56);
    this.jump = { x0: this.x, y0: this.y, x1, y1, dur, h, to, then };
    this.surface = null;
    if (Math.abs(x1 - this.x) > 2) this.facing = x1 > this.x ? 1 : -1;
    this.springV -= 5;
    this.setMode("jump", dur);
  }

  private jumpTo(env: Env, p: Platform, then: Mode) {
    const m = this.margin(env);
    const tx = clamp(clamp(this.x, p.x1 + m, p.x2 - m) + rand(-80, 80), p.x1 + m, p.x2 - m);
    this.startJump(tx, p.y, p, then);
  }

  private jumpToFloor(env: Env, then: Mode, x?: number) {
    const m = this.margin(env);
    const tx = clamp(x ?? this.x + rand(-140, 140), m, env.width - m);
    this.startJump(tx, env.floor, null, then);
  }

  private updateJump() {
    const j = this.jump;
    if (!j) return this.setMode("idle", 1);
    const k = Math.min(1, this.modeT / j.dur);
    this.x = lerp(j.x0, j.x1, k);
    this.y = lerp(j.y0, j.y1, k) - j.h * 4 * k * (1 - k);
    this.squashBase = k < 0.5 ? -0.42 : -0.12;
    if (k >= 1) this.land(j.to, j.y1, 1, j.then);
  }

  private updateFall(dt: number, env: Env) {
    const prev = this.y;
    this.vy = Math.min(1800, this.vy + 2600 * dt);
    const ny = this.y + this.vy * dt;
    this.squashBase = -0.3;
    let best: Platform | null = null;
    if (env.onWindows) {
      for (const p of env.platforms) {
        if (p.y >= prev - 1 && p.y <= ny && this.x >= p.x1 + 8 && this.x <= p.x2 - 8 && (!best || p.y < best.y)) best = p;
      }
    }
    if (best) return this.land(best, best.y, Math.min(1.5, this.vy / 900), "idle");
    if (ny >= env.floor) return this.land(null, env.floor, Math.min(1.5, this.vy / 900), "idle");
    this.y = ny;
  }

  private land(to: Platform | null, y: number, impact: number, then: Mode) {
    this.y = y;
    this.surface = to;
    this.vy = 0;
    this.jump = null;
    this.springV += 5 + impact * 5;
    this.burst("dust", 4);
    if (then === "eatgo") this.setMode("eatgo", 30);
    else this.setMode(then === "jump" || then === "fall" ? "idle" : then, then === "idle" ? rand(1.5, 3.5) : 2);
  }

  private followSurface(env: Env) {
    const s = this.surface;
    if (!s || this.mode === "jump" || this.mode === "drag" || this.mode === "poof" || this.mode === "fall") return;
    const p =
      (env.onWindows && env.platforms.find((q) => q.key === s.key)) ||
      (env.onWindows && env.platforms.find((q) => q.win === s.win && this.x >= q.x1 && this.x <= q.x2)) ||
      null;
    const m = this.margin(env);
    if (!p || p.x2 - p.x1 < m * 2) {
      // окно закрыли, свернули или заслонили — падаем
      this.surface = null;
      this.vy = -120;
      if (this.trash) this.giveUp();
      this.setMode("fall", 5);
      return;
    }
    this.x += p.left - s.left;
    this.y = p.y;
    this.surface = p;
    this.x = clamp(this.x, p.x1 + m, p.x2 - m);
    this.targetX = clamp(this.targetX, p.x1 + m, p.x2 - m);
  }

  // --- музыка и танец ---------------------------------------------------------------------------------
  private syncMusic(on: boolean) {
    if (on === this.music) return;
    this.music = on;
    if (on) {
      this.headphones = true;
      this.burst("spark", 8);
      this.restUntil = this.t + 0.8;
      this.lastActive = this.t;
      if (this.mode === "sleep") this.setMode("idle", 0.8);
    } else {
      this.headphones = false;
      this.burst("poof", 4);
      if (this.mode === "dance") this.setMode("idle", 2);
    }
  }

  private startDance() {
    this.danceUntil = this.t + rand(18, 32);
    this.lastActive = this.t;
    this.setMode("dance", 0);
  }

  private dance(env: Env) {
    if (!this.music) return this.setMode("idle", 2);
    if (!this.frozen && this.t >= this.danceUntil) {
      this.restUntil = this.t + rand(8, 16);
      return this.setMode("idle", 2);
    }
    const beat = this.t * 2.1;
    const ph = beat % 1;
    this.sway = Math.floor(beat) % 2 === 0 ? -1 : 1;
    const up = Math.sin(Math.PI * ph);
    this.hop = (env.hops ? 3.4 : 2) * up;
    this.squashBase = 0.4 * (1 - up) - 0.12;
    this.look = this.sway;
    this.lastActive = this.t;
  }

  // --- еда -------------------------------------------------------------------------------------------------
  private wantsToEat(env: Env) {
    return env.eats && (env.recycle ?? 0) > 0 && this.t >= this.nextEatAt && !this.music;
  }

  startEat(direct: boolean, env: Env) {
    if (this.mode === "drag" || this.mode === "poof") return;
    this.eatDirect = direct;
    this.pendingMunch = false;
    this.trash = true;
    this.lastActive = this.t;
    this.attention = false;
    if (this.mode === "sleep") this.setMode("idle", 1);
    this.goEat(env);
  }

  /** Идти к Корзине: с окна — сначала спрыгнуть, в полёте — после приземления. */
  private goEat(env: Env) {
    const m = this.margin(env);
    this.targetX = clamp(env.trashRight + 10 + m, m, env.width - m);
    if (this.mode === "jump" || this.mode === "fall") {
      this.eatQueued = true;
      return;
    }
    if (this.surface) this.jumpToFloor(env, "eatgo", clamp(this.x, m, env.width - m));
    else this.setMode("eatgo", 30);
  }

  private arriveAtTrash() {
    this.facing = -1;
    if (this.pendingMunch) return this.startMunch();
    if (this.eatDirect) {
      this.setMode("eatwait", 45);
      return;
    }
    this.bubble = { kind: "ask", text: "Можно съесть файлы из корзины?", until: this.t + 22 };
    this.setMode("eatask", 22);
  }

  /** Ответ человека на «Можно съесть?». */
  answer(yes: boolean) {
    if (this.mode !== "eatask") return;
    this.bubble = null;
    this.lastActive = this.t;
    if (yes) {
      this.onFeed?.();
      this.emote = { eyes: "happy", mouth: "open", until: this.t + 1.2 };
      this.setMode("eatwait", 45);
    } else {
      this.say("Ладно, потом…", 1.8);
      this.emote = { eyes: "sad", mouth: "sad", until: this.t + 1.6 };
      this.trash = false;
      this.nextEatAt = this.t + rand(420, 600);
      this.setMode("idle", 2);
    }
  }

  /** Кормление не состоялось (ошибка ядра) — убрать Корзину и вернуться к своим делам. */
  cancelEat() {
    this.pendingMunch = false;
    this.eatQueued = false;
    this.trash = false;
    if (this.bubble?.kind === "ask") this.bubble = null;
    if (this.mode === "eatask" || this.mode === "eatwait" || this.mode === "eatgo") this.setMode("idle", 2);
  }

  /** Открыли меню: питомец останавливается и смотрит на человека; закрыли — снова занят собой. */
  attend(on: boolean) {
    this.attention = on;
    if (on) {
      this.lastActive = this.t;
      if (this.mode === "walk" || this.mode === "sleep") this.setMode("idle", 1e9);
    } else if (this.mode === "idle" && this.modeDur > 1e6) {
      this.modeDur = this.modeT + rand(1, 3);
    }
  }

  private giveUp() {
    if (this.bubble?.kind === "ask") this.bubble = null;
    this.trash = false;
    this.nextEatAt = this.t + rand(360, 540);
    if (this.mode === "eatask" || this.mode === "eatwait" || this.mode === "eatgo") this.setMode("idle", 2);
  }

  private startMunch(seconds = 2.8) {
    this.pendingMunch = false;
    this.bubble = null;
    this.shakeUntil = this.t + seconds;
    this.lastActive = this.t;
    this.setMode("munch", seconds);
  }

  /** Ядро очистило Корзину (plugin.pet.action munch) или питомец съел брошенный файл (eat). */
  munch(name?: string) {
    if (this.mode === "drag" || this.mode === "poof") return;
    if (this.mode === "sleep") this.setMode("idle", 1);
    if (name) {
      this.say(`Ням! ${name}`, 2.2);
      this.startMunch(1.6);
      return;
    }
    if (this.mode === "eatgo") {
      this.pendingMunch = true;
      return;
    }
    this.startMunch();
  }

  /** Отказ (человек не разрешил) или пустая Корзина. */
  sad(text: string) {
    this.emote = { eyes: "sad", mouth: "sad", until: this.t + 2.2 };
    this.say(text, 2.8);
    if (this.trash || this.mode === "eatask" || this.mode === "eatwait" || this.mode === "eatgo") {
      this.trash = false;
      this.nextEatAt = this.t + rand(480, 900);
      this.setMode("idle", 2.5);
    }
  }

  // --- реакции ---------------------------------------------------------------------------------------------
  /** «Джарвис» — подпрыгнуть с «!». */
  react() {
    if (this.mode === "drag" || this.mode === "poof") return;
    this.bangUntil = this.t + 1.4;
    this.hopAt = this.t;
    this.emote = { eyes: "wide", mouth: "o", until: this.t + 1 };
    this.lastActive = this.t;
    if (this.mode === "sleep") this.setMode("idle", 2);
    else if (this.mode === "idle") this.modeDur = Math.max(this.modeDur, this.modeT + 2);
  }

  petted() {
    if (this.mode === "poof") return;
    this.hearts = 5;
    this.emote = { eyes: "happy", mouth: "open", until: this.t + 2.2 };
    this.lastActive = this.t;
    this.springV += 3;
    if (this.mode === "sleep") this.setMode("idle", 2.5);
  }

  say(text: string, seconds?: number) {
    const s = seconds ?? clamp(text.length * 0.08, 2.5, 8);
    this.bubble = { kind: "say", text, until: this.t + s };
  }

  poof() {
    this.burst("poof", 10);
    this.bubble = null;
    this.trash = false;
    this.setMode("poof", 0.5);
  }

  // --- перетаскивание --------------------------------------------------------------------------------------
  grabAt(px: number, py: number) {
    if (this.mode === "poof") return;
    this.grab = { dx: this.x - px, dy: this.y - py };
    this.surface = null;
    this.jump = null;
    this.bubble = this.bubble?.kind === "say" ? this.bubble : null;
    if (this.trash && this.mode !== "munch") this.giveUp();
    this.lastActive = this.t;
    this.setMode("drag", 0);
  }

  dragTo(px: number, py: number, env: Env) {
    if (this.mode !== "drag") return;
    const m = this.margin(env);
    const nx = clamp(px + this.grab.dx, m, env.width - m);
    const ny = clamp(py + this.grab.dy, 40 * env.cs, env.floor);
    if (Math.abs(nx - this.x) > 0.5) this.facing = nx > this.x ? 1 : -1;
    this.look = clamp((nx - this.x) / 6, -1, 1);
    this.x = nx;
    this.y = ny;
  }

  release(env: Env) {
    if (this.mode !== "drag") return;
    if (this.y >= env.floor - 1) return this.land(null, env.floor, 0.5, "idle");
    this.vy = 0;
    this.setMode("fall", 5);
  }

  // --- поза для отрисовки ------------------------------------------------------------------------------------
  pose(): Pose {
    let eyes: Eyes = this.idleFace === "happy" ? "happy" : "open";
    let mouth: Mouth = this.idleFace === "happy" ? "open" : "smile";
    switch (this.mode) {
      case "walk":
      case "eatgo":
        eyes = "open";
        mouth = "smile";
        break;
      case "jump":
      case "fall":
        eyes = "wide";
        mouth = "o";
        break;
      case "dance":
        eyes = "happy";
        mouth = "open";
        break;
      case "sleep":
        eyes = "sleep";
        mouth = "flat";
        break;
      case "drag":
        eyes = this.modeT < 0.7 ? "wide" : "happy";
        mouth = this.modeT < 0.7 ? "o" : "open";
        break;
      case "eatask":
      case "eatwait":
        eyes = "open";
        mouth = "open";
        break;
      case "munch":
        eyes = "happy";
        mouth = "chew";
        break;
      case "poof":
        eyes = "closed";
        mouth = "o";
        break;
    }
    if (this.attention && this.mode === "idle") {
      eyes = "open";
      mouth = "smile";
    }
    if (this.emote) {
      if (this.emote.eyes) eyes = this.emote.eyes;
      if (this.emote.mouth) mouth = this.emote.mouth;
    }
    if (eyes === "open" && this.t < this.blinkUntil) eyes = "closed";
    const m = this.mode;
    return {
      t: this.t,
      squash: clamp(this.squashBase + this.spring, -1, 1),
      eyes,
      mouth,
      look: this.attention ? 0 : this.look,
      headphones: this.headphones,
      disco: this.headphones,
      wiggle: m === "dance" ? 1.6 : m === "drag" ? 1.8 : m === "walk" || m === "eatgo" ? 1.25 : m === "sleep" ? 0.45 : 1,
      dangle: m === "drag" ? 1 : m === "fall" || m === "jump" ? 0.4 : 0,
      glow: m === "sleep" ? 0.25 : this.music ? 1 : 0.75,
      sway: this.sway,
      walk: m === "walk" || m === "eatgo" ? this.walkPhase % 1 : -1,
      arms: m === "dance" || (this.emote?.eyes === "happy" && this.hearts > 0) ? 0.5 + 0.5 * Math.sin(this.t * 9) : m === "drag" ? 1 : 0,
      facing: this.facing,
    };
  }

  /** Подпись с именем — только когда стоит внизу (на окне она закрыла бы заголовок). */
  get label(): boolean {
    return !this.surface && this.mode !== "jump" && this.mode !== "fall" && this.mode !== "drag" && this.mode !== "poof";
  }

  get shaking(): boolean {
    return this.t < this.shakeUntil;
  }
}
