// Криптовалюта (CoinGecko) и акции Мосбиржи — строки «тикер · цена · изменение» как у «Курсов валют».
import { Bitcoin, CandlestickChart } from "lucide-react";
import { useState } from "react";
import { Check, cx, Tabs, TextInput } from "@/ui";
import { Delta, useWidgetData, WidgetFrame, type WidgetProps } from "../common";
import { CfgButton, toList, WidgetSettingsModal } from "./shared";

/** Сколько строк по ~27 px помещается в тело виджета высотой h клеток. */
function rowsFor(h: number, row = 27): number {
  const body = h * 104 + (h - 1) * 12 - 24 - 26;
  return Math.max(1, Math.floor(body / row));
}

function fmtPrice(v: number | null | undefined, cur: "usd" | "rub"): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const digits = v >= 1000 ? 0 : v >= 1 ? 2 : 4;
  const s = v.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return cur === "usd" ? `$${s}` : `${s} ₽`;
}

// --- криптовалюта ----------------------------------------------------------------------------------
interface Coin {
  id: string;
  symbol: string;
  name: string;
  usd: number;
  rub: number | null;
  change_24h: number | null;
  change_24h_rub?: number | null;
}

const COINS: { id: string; symbol: string; name: string; color: string }[] = [
  { id: "bitcoin", symbol: "BTC", name: "Bitcoin", color: "#F7931A" },
  { id: "ethereum", symbol: "ETH", name: "Ethereum", color: "#627EEA" },
  { id: "the-open-network", symbol: "TON", name: "Toncoin", color: "#0098EA" },
  { id: "solana", symbol: "SOL", name: "Solana", color: "#9945FF" },
  { id: "tether", symbol: "USDT", name: "Tether", color: "#26A17B" },
  { id: "binancecoin", symbol: "BNB", name: "BNB", color: "#F3BA2F" },
  { id: "ripple", symbol: "XRP", name: "XRP", color: "#8B95A6" },
  { id: "dogecoin", symbol: "DOGE", name: "Dogecoin", color: "#C2A633" },
  { id: "tron", symbol: "TRX", name: "TRON", color: "#EF0027" },
  { id: "cardano", symbol: "ADA", name: "Cardano", color: "#3468D1" },
  { id: "litecoin", symbol: "LTC", name: "Litecoin", color: "#345D9D" },
  { id: "notcoin", symbol: "NOT", name: "Notcoin", color: "#E5E7EB" },
];

function CoinBadge({ symbol }: { symbol: string }) {
  const c = COINS.find((x) => x.symbol === symbol)?.color ?? "#64748B";
  return (
    <span className="wx-coin" style={{ background: `color-mix(in srgb, ${c} 22%, transparent)`, color: c }}>
      {symbol.slice(0, symbol.length > 3 ? 1 : 3)}
    </span>
  );
}

export function CryptoWidget({ w, h, config, setConfig }: WidgetProps) {
  const ids = toList(config.ids);
  const cur: "usd" | "rub" = config.currency === "rub" ? "rub" : "usd";
  const { data, error } = useWidgetData<{ items?: Coin[]; error?: string }>(
    "crypto",
    { ids: ids.join(",") || undefined },
    { refreshMs: 120_000 },
  );
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState<string[]>([]);
  const [pickCur, setPickCur] = useState<"usd" | "rub">(cur);
  const items = data?.items ?? [];
  const wide = w >= 2;
  return (
    <WidgetFrame
      icon={<Bitcoin />}
      title={(config.title as string) || "Криптовалюта"}
      actions={
        <CfgButton
          onClick={() => {
            setPick(ids.length ? ids : COINS.slice(0, 4).map((c) => c.id));
            setPickCur(cur);
            setOpen(true);
          }}
        />
      }
    >
      {!items.length ? (
        <div className="sub">{error || "Загружаю курсы…"}</div>
      ) : (
        <div className="wx-rows">
          {items.slice(0, rowsFor(h)).map((c) => {
            const price = cur === "rub" ? c.rub : c.usd;
            const delta = cur === "rub" ? (c.change_24h_rub ?? c.change_24h) : c.change_24h;
            return (
              <div key={c.id} className={cx("wx-quote", wide && "wide")}>
                {wide && <CoinBadge symbol={c.symbol} />}
                <span className="code">{wide ? c.name : c.symbol}</span>
                <span className="val">{fmtPrice(price, cur)}</span>
                <Delta value={delta} suffix="%" />
              </div>
            );
          })}
        </div>
      )}
      <WidgetSettingsModal
        open={open}
        title="Криптовалюта"
        onClose={() => setOpen(false)}
        onSave={() => setConfig({ ...config, ids: pick, currency: pickCur })}
      >
        <label className="field-label">Монеты</label>
        <div className="wx-pick-grid">
          {COINS.map((c) => (
            <Check
              key={c.id}
              chip
              checked={pick.includes(c.id)}
              onChange={(v) => setPick((p) => (v ? [...p, c.id] : p.filter((x) => x !== c.id)))}
            >
              {c.symbol} <span className="faint">{c.name}</span>
            </Check>
          ))}
        </div>
        <label className="field-label" style={{ marginTop: 14 }}>
          Цена в
        </label>
        <Tabs
          value={pickCur}
          onChange={setPickCur}
          items={[
            { value: "usd", label: "Долларах" },
            { value: "rub", label: "Рублях" },
          ]}
        />
      </WidgetSettingsModal>
    </WidgetFrame>
  );
}

// --- акции ------------------------------------------------------------------------------------------
interface Stock {
  ticker: string;
  name: string;
  last: number;
  change: number;
  change_pct: number;
  trading?: boolean;
}

const POPULAR = ["SBER", "GAZP", "LKOH", "YDEX", "T", "VTBR", "ROSN", "GMKN", "NVTK", "MGNT", "PLZL", "MTSS", "OZON", "AFLT"];

export function StocksWidget({ w, h, config, setConfig }: WidgetProps) {
  const tickers = toList(config.tickers).map((t) => t.toUpperCase());
  const { data, error } = useWidgetData<{ items?: Stock[]; error?: string }>(
    "stocks",
    { tickers: tickers.join(",") || undefined },
    { refreshMs: 120_000 },
  );
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const items = data?.items ?? [];
  const wide = w >= 2;
  const chosen = toList(draft).map((t) => t.toUpperCase());
  return (
    <WidgetFrame
      icon={<CandlestickChart />}
      title={(config.title as string) || "Акции"}
      actions={
        <CfgButton
          onClick={() => {
            setDraft((tickers.length ? tickers : POPULAR.slice(0, 6)).join(", "));
            setOpen(true);
          }}
        />
      }
    >
      {!items.length ? (
        <div className="sub">{error || "Загружаю котировки…"}</div>
      ) : (
        <div className="wx-rows">
          {items.slice(0, rowsFor(h)).map((s) => (
            <div key={s.ticker} className={cx("wx-quote", wide && "wide", "stock")} title={s.name}>
              <span className="code">{s.ticker}</span>
              {wide && <span className="name ellipsis">{s.name}</span>}
              <span className="val">{s.last.toLocaleString("ru-RU", { maximumFractionDigits: s.last >= 1000 ? 1 : 2 })} ₽</span>
              <Delta value={s.change_pct} suffix="%" />
            </div>
          ))}
        </div>
      )}
      <WidgetSettingsModal open={open} title="Акции Мосбиржи" onClose={() => setOpen(false)} onSave={() => setConfig({ ...config, tickers: chosen })}>
        <label className="field-label">Тикеры через запятую</label>
        <TextInput value={draft} onChange={setDraft} placeholder="SBER, GAZP, LKOH" className="mono" />
        <div className="wx-pick-grid" style={{ marginTop: 12 }}>
          {POPULAR.map((t) => (
            <Check
              key={t}
              chip
              checked={chosen.includes(t)}
              onChange={(v) => setDraft((v ? [...chosen, t] : chosen.filter((x) => x !== t)).join(", "))}
            >
              {t}
            </Check>
          ))}
        </div>
      </WidgetSettingsModal>
    </WidgetFrame>
  );
}
