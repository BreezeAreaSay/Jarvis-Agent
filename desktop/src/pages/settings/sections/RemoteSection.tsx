// «Пульт в телефоне» (PC Control): страница в браузере телефона по Wi-Fi и Telegram-бот.
import {
  ArrowLeft,
  CircleCheck,
  ExternalLink,
  Link2,
  MapPin,
  Pause,
  RefreshCw,
  Send,
  SkipForward,
  Smartphone,
  Star,
  Unlink,
  Volume2,
  Wifi,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/api/client";
import { onEvent } from "@/api/events";
import type { CommandInfo, RemoteInfo } from "@/api/types";
import { LogoMark } from "@/components/Orb";
import { bridge } from "@/lib/bridge";
import { useApp } from "@/store/app";
import { Button, Card, cx, IconButton, notify, notifyError, Setting, Toggle } from "@/ui";
import { CopyButton, NumberField, Note, SecretField, useApi, useSettingsPatch } from "../kit";

/** Состояние пульта: контракт + то, что ядро отдаёт сверх него (статус сервера, срок кода, ссылка). */
interface RemoteState {
  lan?: NonNullable<RemoteInfo["lan"]> & { status?: string; error?: string };
  telegram?: NonNullable<RemoteInfo["telegram"]> & {
    pairing_expires?: number | null;
    pairing_link?: string;
    token_set?: boolean;
  };
}

/** QR-код от ядра: только если это действительно SVG без скриптов. */
function safeSvg(svg: string | undefined): string | null {
  const s = (svg ?? "").trim();
  if (!s.startsWith("<svg") || !s.endsWith("</svg>")) return null;
  if (/<script|<foreignObject|\son\w+\s*=|javascript:/i.test(s)) return null;
  return s;
}

/** Телефон с Telegram-ботом — как в Luxify PC Control. */
function PhoneMock({ bot, owner }: { bot: string; owner: boolean }) {
  const now = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return (
    <div className="phone" aria-hidden>
      <div className="phone-screen">
        <div className="phone-status">
          <span>{now}</span>
          <span className="phone-notch" />
          <span>5G</span>
        </div>
        <div className="phone-head">
          <span className="phone-avatar">
            <LogoMark size={22} />
          </span>
          <span className="col" style={{ gap: 0, minWidth: 0 }}>
            <b className="ellipsis">{bot ? `@${bot}` : "Jarvis PC Control"}</b>
            <span className="faint">бот</span>
          </span>
        </div>
        <div className="phone-msg">
          <b>Главное › Медиа</b>
          <span className="faint">Выберите команду</span>
        </div>
        <div className="phone-keys">
          <span>
            <Pause /> Пауза
          </span>
          <span>
            <SkipForward /> Дальше
          </span>
          <span>
            <Volume2 /> Громче
          </span>
          <span>
            <ArrowLeft /> Назад
          </span>
        </div>
        <div className="phone-msg done">
          <CircleCheck /> <b>Выполнено:</b>&nbsp;Пауза
        </div>
        {!owner && <div className="phone-hint">Привяжите телефон — и бот будет слушаться только вас</div>}
      </div>
    </div>
  );
}

const TG_STATUS: Record<string, { text: string; cls: string; dot: string }> = {
  online: { text: "В сети", cls: "ok", dot: "ok" },
  connecting: { text: "Подключаюсь…", cls: "accent", dot: "busy" },
  error: { text: "Ошибка", cls: "err", dot: "err" },
  off: { text: "Выключен", cls: "", dot: "" },
};

function LanCard({ info, missing, onInfo, reload }: { info: RemoteState["lan"]; missing: boolean; onInfo: (s: RemoteState) => void; reload: () => void }) {
  const [settings, patch] = useSettingsPatch();
  const setSecret = useApp((s) => s.setSecret);
  const hasToken = useApp((s) => !!s.secrets.remote_token);
  const [busy, setBusy] = useState(false);
  if (!settings) return null;
  const r = settings.remote;
  const svg = safeSvg(info?.qr_svg);
  const urls = info?.urls ?? [];
  const regenerate = async () => {
    setBusy(true);
    try {
      try {
        onInfo(await api.post<RemoteState>("/api/remote/token"));
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 404)) throw e;
        // старое ядро: пустой секрет — ядро создаст новый пароль
        await setSecret("remote_token", "");
        setTimeout(reload, 400);
      }
      notify("Пароль пульта сменён — отсканируйте новый QR-код", "ok");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card
      icon={<Wifi />}
      title="Пульт по Wi-Fi"
      sub="Откройте страницу в браузере телефона — компьютер и телефон должны быть в одной сети"
      actions={<Toggle checked={r.lan_enabled} onChange={(v) => patch({ remote: { lan_enabled: v } })} label="Пульт по Wi-Fi" />}
    >
      {r.lan_enabled ? (
        <>
          {info?.error && <Note tone="err">{info.error}</Note>}
          <div className="lan-layout" style={info?.error ? { marginTop: 10 } : undefined}>
            <div className="qr-box">
              {svg ? (
                <div className="qr" dangerouslySetInnerHTML={{ __html: svg }} />
              ) : (
                <div className="qr qr-empty">
                  {missing ? "QR-код появится, когда модуль пульта будет готов" : urls.length === 0 ? "Нет сети Wi-Fi или Ethernet" : <span className="spinner" />}
                </div>
              )}
              <div className="faint tiny" style={{ textAlign: "center" }}>
                Наведите камеру телефона
              </div>
            </div>
            <div className="grow col" style={{ gap: 8, minWidth: 0 }}>
              {urls.length > 0 ? (
                urls.map((u) => (
                  <div key={u} className="url-row">
                    <Link2 />
                    <span className="mono ellipsis grow" title={u}>
                      {u}
                    </span>
                    <CopyButton text={u} label="Скопировать адрес" />
                    <IconButton icon={<ExternalLink />} label="Открыть" size="sm" onClick={() => bridge.openExternal(u)} />
                  </div>
                ))
              ) : (
                <div className="faint small">{missing ? "Адреса появятся, когда модуль пульта будет готов." : "Ищу адрес компьютера в сети…"}</div>
              )}
              <Setting name="Порт" desc="Если занят другой программой — смените">
                <NumberField value={r.lan_port} min={1024} max={65535} width={100} onCommit={(v) => patch({ remote: { lan_port: v } })} />
              </Setting>
              <Setting name="Пароль пульта" desc="Зашит в ссылку и QR-код. Сменить — старые ссылки перестанут работать">
                {hasToken && <span className="chip ok">Задан</span>}
                <Button size="sm" icon={<RefreshCw />} loading={busy} onClick={regenerate}>
                  Сменить
                </Button>
              </Setting>
            </div>
          </div>
        </>
      ) : (
        <div className="faint small">Включите — и на телефоне появится пульт: команды, музыка, громкость, выключение ПК.</div>
      )}
    </Card>
  );
}

function useCountdown(until: number | null | undefined): string {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!until) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [until]);
  if (!until) return "";
  const left = Math.max(0, Math.round(until - now));
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

function TelegramCard({ info, missing, reload }: { info: RemoteState["telegram"]; missing: boolean; reload: () => void }) {
  const [settings, patch] = useSettingsPatch();
  const hasToken = useApp((s) => !!s.secrets.telegram_token);
  const [local, setLocal] = useState<{ code: string; expires?: number; link?: string } | null>(null);
  const [hidden, setHidden] = useState("");
  const [pairing, setPairing] = useState(false);
  const owner = settings?.remote.telegram_owner_id ?? 0;
  const code = info?.pairing_code || local?.code || "";
  const expires = info?.pairing_expires ?? local?.expires;
  const link = info?.pairing_link || local?.link || "";
  const showCode = !!code && code !== hidden && !owner;
  const left = useCountdown(showCode ? expires : null);

  // ждём, пока телефон пришлёт /start <код> (если ядро не присылает remote.changed)
  useEffect(() => {
    if (!showCode) return;
    const t = setInterval(reload, 4000);
    return () => clearInterval(t);
  }, [showCode, reload]);
  // код использован — телефон привязан
  const [ownerBefore, setOwnerBefore] = useState(owner);
  useEffect(() => {
    if (owner && owner !== ownerBefore) {
      setLocal(null);
      notify("Телефон привязан — бот слушается только вас", "ok");
    }
    setOwnerBefore(owner);
  }, [owner, ownerBefore]);

  if (!settings) return null;
  const r = settings.remote;
  const status = !hasToken
    ? { text: "Нужен токен", cls: "", dot: "" }
    : (TG_STATUS[info?.status ?? (r.telegram_enabled ? "connecting" : "off")] ?? TG_STATUS.off);
  const bot = info?.bot ?? "";

  const pair = async () => {
    setPairing(true);
    try {
      const res = await api.post<{ pairing_code: string; expires?: number; link?: string }>("/api/remote/telegram/pair");
      setLocal({ code: res.pairing_code, expires: res.expires, link: res.link });
      setHidden("");
    } catch (e) {
      notifyError(e);
    } finally {
      setPairing(false);
    }
  };

  return (
    <Card
      icon={<Send />}
      title="Telegram-бот"
      sub="Управляйте компьютером откуда угодно: в дороге, на работе, из другой комнаты"
      actions={
        <div className="row" style={{ gap: 10 }}>
          {r.telegram_enabled && (
            <span className={cx("chip", status.cls)} title={info?.error || undefined}>
              <span className={cx("status-dot", status.dot)} />
              {status.text}
            </span>
          )}
          <Toggle checked={r.telegram_enabled} onChange={(v) => patch({ remote: { telegram_enabled: v } })} label="Telegram-бот" />
        </div>
      }
    >
      {missing && <Note>Состояние бота появится, когда модуль пульта ядра будет готов.</Note>}
      <Setting
        name="Токен бота"
        desc={
          <>
            Создайте бота у{" "}
            <button type="button" className="link-btn" onClick={() => bridge.openExternal("https://t.me/BotFather")}>
              @BotFather
            </button>{" "}
            командой /newbot и вставьте токен сюда
          </>
        }
        wide
      >
        <SecretField name="telegram_token" placeholder="123456789:AA…" savedText="Токен сохранён" onSaved={reload} />
      </Setting>
      {!hasToken && (
        <ol className="howto">
          <li>
            Откройте в Telegram <b>@BotFather</b> и отправьте <span className="kbd">/newbot</span>
          </li>
          <li>Придумайте имя, например «Jarvis PC Control», и адрес, который кончается на «bot»</li>
          <li>Скопируйте токен из ответа и вставьте в поле выше, затем включите бота</li>
        </ol>
      )}
      {r.telegram_enabled && hasToken && info?.status === "error" && info.error && (
        <Note tone="err">
          {info.error}
        </Note>
      )}
      {bot && (
        <Setting name="Бот" desc="Откройте его в Telegram на телефоне">
          <button type="button" className="link-btn" onClick={() => bridge.openExternal(`https://t.me/${bot}`)}>
            @{bot} <ExternalLink />
          </button>
        </Setting>
      )}
      <Setting
        name="Ваш телефон"
        desc={
          owner
            ? `Бот слушается только аккаунта Telegram с ID ${owner}`
            : !hasToken
              ? "Сначала вставьте токен бота"
              : !r.telegram_enabled
                ? "Включите бота переключателем вверху"
                : "Привяжите телефон — чужие люди не смогут управлять ПК"
        }
      >
        {owner ? (
          <>
            <span className="chip ok">
              <Smartphone /> Привязан
            </span>
            <Button size="sm" variant="ghost" icon={<Unlink />} onClick={() => patch({ remote: { telegram_owner_id: 0 } })}>
              Отвязать
            </Button>
          </>
        ) : (
          <Button size="sm" variant="primary" icon={<Smartphone />} loading={pairing} disabled={!hasToken || !r.telegram_enabled} onClick={pair}>
            Привязать телефон
          </Button>
        )}
      </Setting>
      {showCode && (
        <div className="pair-box pop-in">
          <div className="pair-code" aria-label={`Код ${code}`}>
            {code.split("").map((d, i) => (
              <span key={i}>{d}</span>
            ))}
          </div>
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="small">
              Отправьте боту <span className="kbd mono">/start {code}</span>
            </div>
            <div className="faint tiny" style={{ marginTop: 4 }}>
              {left ? `Код действует ещё ${left}. ` : ""}Жду сообщения с телефона…
            </div>
            <div className="row" style={{ gap: 8, marginTop: 10 }}>
              <CopyButton text={`/start ${code}`} label="Скопировать команду" />
              {(link || bot) && (
                <Button size="sm" icon={<Send />} onClick={() => bridge.openExternal(link || `https://t.me/${bot}?start=${code}`)}>
                  Открыть бота
                </Button>
              )}
              <Button size="sm" variant="ghost" onClick={() => setHidden(code)}>
                Скрыть
              </Button>
            </div>
          </div>
          <span className="spinner" />
        </div>
      )}
    </Card>
  );
}

/** Избранные команды — первые кнопки на главном экране пульта. Список команд грузится по требованию. */
function FavoritesCard() {
  const [settings, patch] = useSettingsPatch();
  const favCount = settings?.remote.favorites.length ?? 0;
  const [want, setWant] = useState(favCount > 0);
  const commands = useApi<{ items: CommandInfo[] }>(want ? "/api/commands" : null);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const byId = useMemo(() => new Map((commands.data?.items ?? []).map((c) => [c.id, c])), [commands.data]);
  if (!settings) return null;
  const favs = settings.remote.favorites;
  const query = q.trim().toLowerCase();
  const found = (commands.data?.items ?? [])
    .filter((c) => c.enabled && !favs.includes(c.id))
    .filter((c) => !query || c.name.toLowerCase().includes(query) || c.phrases.some((p) => p.includes(query)))
    .slice(0, 30);
  const set = (list: string[]) => patch({ remote: { favorites: list } });
  return (
    <Card icon={<Star />} title="Избранное на пульте" sub="Эти команды будут первыми на главном экране пульта и в боте">
      <div className="chips-edit">
        {favs.length === 0 && <span className="faint small">Пока пусто — добавьте любимые команды</span>}
        {favs.map((id) => (
          <span key={id} className="chip word-chip">
            {byId.get(id)?.name ?? id}
            <button type="button" aria-label="Убрать" onClick={() => set(favs.filter((x) => x !== id))}>
              <X />
            </button>
          </span>
        ))}
      </div>
      {commands.missing ? (
        <div className="faint small" style={{ marginTop: 10 }}>
          Выбрать команды можно будет, когда модуль команд ядра будет готов.
        </div>
      ) : (
        <div className="select combo" style={{ marginTop: 10, maxWidth: 380 }}>
          <input
            className="input"
            value={q}
            placeholder="Найти команду…"
            onFocus={() => {
              setWant(true);
              setOpen(true);
            }}
            onBlur={() => setTimeout(() => setOpen(false), 150)}
            onChange={(e) => {
              setQ(e.target.value);
              setOpen(true);
            }}
          />
          {open && want && !commands.data && (
            <div className="select-menu">
              <div className="row faint small" style={{ padding: 8 }}>
                <span className="spinner" /> Загружаю команды…
              </div>
            </div>
          )}
          {open && found.length > 0 && (
            <div className="select-menu">
              {found.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  className="select-option"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    set([...favs, c.id]);
                    setQ("");
                  }}
                >
                  <span className="ellipsis">{c.name}</span>
                  <span className="hint ellipsis">{c.phrases[0]}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

export default function RemoteSection() {
  const [settings] = useSettingsPatch();
  const remote = useApi<RemoteState>("/api/remote");
  const { reload, setData } = remote;
  // ядро присылает remote.changed — обновляемся без опроса
  useEffect(() => onEvent<RemoteState>("remote.changed", (e) => setData(e.data)), [setData]);
  const r = settings?.remote;
  // после смены настроек ядро перезапускает пульт — перечитать состояние
  const key = r ? `${r.lan_enabled}:${r.lan_port}:${r.telegram_enabled}` : "";
  const [lastKey, setLastKey] = useState(key);
  useEffect(() => {
    if (key === lastKey) return;
    setLastKey(key);
    const t = setTimeout(() => void reload(), 800);
    return () => clearTimeout(t);
  }, [key, lastKey, reload]);
  // пока бот подключается — следим за ним
  const tgStatus = remote.data?.telegram?.status;
  useEffect(() => {
    if (tgStatus !== "connecting") return;
    const t = setInterval(() => void reload(), 3000);
    return () => clearInterval(t);
  }, [tgStatus, reload]);
  if (!settings || !r) return null;
  return (
    <div className="stack">
      <div className="set-hero remote-hero">
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="eyebrow">PC Control</div>
          <h2 className="hero-title">Пульт в телефоне</h2>
          <p className="muted small hero-text">
            Музыка, громкость, команды и выключение компьютера — с телефона. Дома по Wi-Fi или откуда угодно через
            Telegram.
          </p>
          <div className="hero-chips">
            <span className="hero-chip">
              <MapPin /> Из дома
            </span>
            <span className="hero-chip">
              <MapPin /> В дороге
            </span>
            <span className="hero-chip">
              <MapPin /> С работы
            </span>
          </div>
        </div>
        <PhoneMock bot={remote.data?.telegram?.bot ?? ""} owner={!!r.telegram_owner_id} />
      </div>
      <LanCard info={remote.data?.lan} missing={remote.missing} onInfo={setData} reload={reload} />
      <TelegramCard info={remote.data?.telegram} missing={remote.missing} reload={reload} />
      <FavoritesCard />
    </div>
  );
}
