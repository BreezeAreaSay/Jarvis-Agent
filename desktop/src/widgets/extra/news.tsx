// Новости: свежие заголовки (Google Новости / Lenta.ru), источник и время; по нажатию — статья в браузере.
import { Newspaper } from "lucide-react";
import { useState } from "react";
import { bridge } from "@/lib/bridge";
import { timeAgo, TextInput } from "@/ui";
import { useWidgetData, WidgetFrame, type WidgetProps } from "../common";
import { CfgButton, WidgetSettingsModal } from "./shared";

interface NewsItem {
  title: string;
  url: string;
  source: string;
  ts: number;
}

const TOPICS = ["", "Технологии", "Экономика", "Наука", "Спорт", "Игры", "Кино", "Авто"];

export function NewsWidget({ w, h, config, setConfig }: WidgetProps) {
  const topic = String(config.topic ?? "");
  const { data, error } = useWidgetData<{ items?: NewsItem[]; error?: string }>(
    "news",
    { topic: topic || undefined, limit: 12 },
    { refreshMs: 15 * 60_000 },
  );
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(topic);
  const items = data?.items ?? [];
  const compact = h <= 1;
  return (
    <WidgetFrame
      icon={<Newspaper />}
      title={
        <>
          {(config.title as string) || "Новости"}
          {topic && <span className="wx-topic">{topic}</span>}
        </>
      }
      actions={
        <CfgButton
          onClick={() => {
            setDraft(topic);
            setOpen(true);
          }}
        />
      }
    >
      {!items.length ? (
        <div className="sub">{error || "Загружаю новости…"}</div>
      ) : (
        <div className="wlist wx-news">
          {items.slice(0, compact ? 2 : 12).map((n) => (
            <button key={n.url + n.title} className="wx-news-item" onClick={() => bridge.openExternal(n.url)} title={n.title}>
              <span className={compact || w <= 1 ? "t ellipsis" : "t"}>{n.title}</span>
              <span className="m">
                {[n.source, n.ts ? timeAgo(n.ts) : ""].filter(Boolean).join(" · ")}
              </span>
            </button>
          ))}
        </div>
      )}
      <WidgetSettingsModal open={open} title="Новости" onClose={() => setOpen(false)} onSave={() => setConfig({ ...config, topic: draft.trim() })}>
        <label className="field-label">Тема</label>
        <TextInput value={draft} onChange={setDraft} placeholder="Пусто — главное за день" />
        <div className="wx-pick-grid" style={{ marginTop: 12 }}>
          {TOPICS.map((t) => (
            <button key={t || "main"} className={`chip${draft === t ? " accent" : ""}`} onClick={() => setDraft(t)}>
              {t || "Главное"}
            </button>
          ))}
        </div>
      </WidgetSettingsModal>
    </WidgetFrame>
  );
}
