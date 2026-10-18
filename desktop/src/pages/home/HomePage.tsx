// Главная: вкладки столов, сетка виджетов, правка стола, новый стол из заготовок.
import {
  Briefcase,
  CircleDot,
  Clock,
  Cpu,
  Gamepad2,
  GraduationCap,
  Home,
  LayoutGrid,
  Mic,
  Music2,
  PawPrint,
  Pencil,
  Plus,
  Sparkles,
  Sunrise,
  Trash2,
  TrendingUp,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { WidgetPlacement } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { bindDeskEvents, useDesks } from "@/store/desks";
import { Button, cx, IconButton, Modal, notifyError, TextInput } from "@/ui";
import { WIDGETS, WIDGET_BY_TYPE } from "@/widgets/registry";
import { DeskGrid, findSpot } from "./DeskGrid";

const PRESET_ICONS: Record<string, ReactNode> = {
  empty: <LayoutGrid />,
  morning: <Sunrise />,
  work: <Briefcase />,
  study: <GraduationCap />,
  time: <Clock />,
  finance: <TrendingUp />,
  media: <Music2 />,
  games: <Gamepad2 />,
  computer: <Cpu />,
  smarthome: <Home />,
  pet: <PawPrint />,
  avatar: <Sparkles />,
};

const FALLBACK_PRESETS = [
  { id: "empty", name: "Пустой", description: "Добавьте виджеты сами" },
  { id: "morning", name: "Утро", description: "Погода, события, задачи и курсы" },
  { id: "work", name: "Работа", description: "Задачи, календарь, файлы и программы" },
  { id: "study", name: "Учёба", description: "Таймер, отсчёт до сессии, задачи" },
  { id: "time", name: "Время и заметки", description: "Часы, календарь, задачи и заметки" },
  { id: "finance", name: "Финансы", description: "Криптовалюта, акции и курсы ЦБ" },
  { id: "media", name: "Медиа", description: "Плеер, громкость, новости и сайты" },
  { id: "games", name: "Игры", description: "Программы, команды и нагрузка ПК" },
  { id: "computer", name: "Компьютер", description: "Система, расписание, программы, файлы" },
  { id: "smarthome", name: "Умный дом", description: "Устройства, команды и погода" },
  { id: "pet", name: "Питомец", description: "Питомец и быстрые плагины" },
  { id: "avatar", name: "Только аватар", description: "Большие круги на весь стол" },
];

function MicButton() {
  const state = useApp((s) => s.voiceState);
  const active = state === "listening" || state === "processing";
  return (
    <IconButton
      icon={<Mic />}
      label="Сказать команду"
      variant="primary"
      className={cx(active && "mic-live")}
      onClick={() => api.post(active ? "/api/voice/stop" : "/api/voice/listen").catch(notifyError)}
    />
  );
}

function NewDeskModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { presets, loadPresets, createDesk } = useDesks();
  const [name, setName] = useState("");
  const [preset, setPreset] = useState("empty");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) {
      loadPresets();
      setName("");
      setPreset("empty");
    }
  }, [open, loadPresets]);
  const list = presets.length ? presets : FALLBACK_PRESETS;
  const create = async () => {
    setBusy(true);
    try {
      const chosen = list.find((p) => p.id === preset);
      await createDesk(name.trim() || chosen?.name || "Новый стол", preset);
      onClose();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Новый стол"
      width={640}
      footer={
        <>
          <Button onClick={onClose}>Отмена</Button>
          <Button variant="primary" onClick={create} loading={busy}>
            Создать стол
          </Button>
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0 }}>
        Своя страница с виджетами. Название и состав можно поменять потом.
      </p>
      <TextInput value={name} onChange={setName} placeholder="Название стола" autoFocus onEnter={create} />
      <div className="row" style={{ margin: "16px 0 10px" }}>
        <span className="small muted">С чего начать</span>
        <span className="spacer" />
        <span className="chip accent">{list.length} заготовок</span>
      </div>
      <div className="picker-grid">
        {list.map((p) => (
          <button
            key={p.id}
            className={cx("picker-item", preset === p.id && "selected")}
            onClick={() => {
              setPreset(p.id);
              if (!name.trim() || list.some((x) => x.name === name)) setName(p.id === "empty" ? "" : p.name);
            }}
          >
            <span className="pi-icon">{PRESET_ICONS[p.id] ?? <LayoutGrid />}</span>
            <span>
              <div className="pi-title">{p.name}</div>
              <div className="pi-desc">{p.description}</div>
            </span>
          </button>
        ))}
      </div>
    </Modal>
  );
}

function WidgetPicker({
  open,
  onClose,
  onPick,
}: {
  open: boolean;
  onClose: () => void;
  onPick: (type: string) => void;
}) {
  const [q, setQ] = useState("");
  const list = useMemo(
    () => WIDGETS.filter((w) => (w.title + w.description).toLowerCase().includes(q.toLowerCase())),
    [q],
  );
  return (
    <Modal open={open} onClose={onClose} title="Добавить виджет" width={680}>
      <TextInput value={q} onChange={setQ} placeholder="Поиск виджета" autoFocus />
      <div className="picker-grid" style={{ marginTop: 14 }}>
        {list.map((w) => (
          <button key={w.type} className="picker-item" onClick={() => onPick(w.type)}>
            <span className="pi-icon">{w.icon}</span>
            <span>
              <div className="pi-title">{w.title}</div>
              <div className="pi-desc">
                {w.description} · {w.size[0]}×{w.size[1]}
              </div>
            </span>
          </button>
        ))}
      </div>
    </Modal>
  );
}

function WidgetSettings({
  widget,
  onClose,
  onSave,
}: {
  widget: WidgetPlacement | null;
  onClose: () => void;
  onSave: (config: Record<string, unknown>) => void;
}) {
  const [city, setCity] = useState("");
  const [title, setTitle] = useState("");
  useEffect(() => {
    setCity(String(widget?.config.city ?? ""));
    setTitle(String(widget?.config.title ?? ""));
  }, [widget]);
  if (!widget) return null;
  const def = WIDGET_BY_TYPE[widget.type];
  return (
    <Modal
      open={!!widget}
      onClose={onClose}
      title={def?.title ?? "Виджет"}
      width={440}
      footer={
        <>
          <Button onClick={onClose}>Отмена</Button>
          <Button
            variant="primary"
            onClick={() => {
              onSave({ ...widget.config, ...(widget.type === "weather" ? { city } : {}), ...(title ? { title } : {}) });
              onClose();
            }}
          >
            Сохранить
          </Button>
        </>
      }
    >
      {widget.type === "weather" ? (
        <>
          <label className="field-label">Город</label>
          <TextInput value={city} onChange={setCity} placeholder="Как в профиле" />
          <div className="field-hint">Пусто — город из профиля.</div>
        </>
      ) : (
        <>
          <label className="field-label">Заголовок</label>
          <TextInput value={title} onChange={setTitle} placeholder={def?.title} />
          <div className="field-hint">Размер меняется уголком справа внизу, место — перетаскиванием.</div>
        </>
      )}
    </Modal>
  );
}

export default function HomePage(_: { route: string[] }) {
  const { desks, current, editing, load, select, setEditing, setWidgets, deleteDesk } = useDesks();
  const [newDesk, setNewDesk] = useState(false);
  const [pickerAt, setPickerAt] = useState<{ x: number; y: number } | null>(null);
  const [configuring, setConfiguring] = useState<WidgetPlacement | null>(null);

  useEffect(() => {
    bindDeskEvents();
    load();
  }, [load]);

  const desk = desks.find((d) => d.id === current) ?? desks[0];
  const tabs = desks.map((d) => ({ value: d.id, label: d.name }));
  // столов бывает больше, чем помещается: вкладки прокручиваются (и колесом), справа — затухание, пока есть что листать
  const tabsRef = useRef<HTMLDivElement>(null);
  const [tabsMore, setTabsMore] = useState(false);
  const checkTabs = useCallback(() => {
    const el = tabsRef.current;
    if (el) setTabsMore(el.scrollLeft + el.clientWidth < el.scrollWidth - 2);
  }, []);
  useEffect(() => {
    const el = tabsRef.current;
    if (!el) return;
    const ro = new ResizeObserver(checkTabs);
    ro.observe(el);
    return () => ro.disconnect();
  }, [checkTabs]);
  useEffect(() => {
    // «открой стол Игры» голосом — выбранная вкладка не должна оказаться за краем
    tabsRef.current?.querySelector<HTMLElement>(".tab.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    checkTabs();
  }, [current, desks.length, checkTabs]);

  const addWidget = (type: string) => {
    if (!desk) return;
    const def = WIDGET_BY_TYPE[type];
    const [w, h] = def?.size ?? [1, 1];
    const spot = findSpot(desk.widgets, w, h, pickerAt ?? undefined);
    setWidgets(desk.id, [
      ...desk.widgets,
      { id: `w-${type}-${Date.now().toString(36)}`, type, ...spot, config: {} },
    ]);
    setPickerAt(null);
  };

  return (
    <div className="page">
      <PageHeader
        actions={
          <div className="row" style={{ gap: 8 }}>
            {editing && (
              <>
                {desks.length > 1 && (
                  <Button
                    size="sm"
                    variant="ghost"
                    icon={<Trash2 />}
                    onClick={() => desk && deleteDesk(desk.id).catch(notifyError)}
                  >
                    Удалить стол
                  </Button>
                )}
                <Button size="sm" icon={<Plus />} onClick={() => setPickerAt({ x: 0, y: 0 })}>
                  Добавить виджет
                </Button>
              </>
            )}
            <IconButton
              icon={<Pencil />}
              label={editing ? "Готово" : "Изменить стол"}
              active={editing}
              onClick={() => setEditing(!editing)}
            />
            <MicButton />
          </div>
        }
      >
        <div
          className={cx("tabs desk-tabs", tabsMore && "more")}
          ref={tabsRef}
          onScroll={checkTabs}
          onWheel={(e) => {
            if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) e.currentTarget.scrollLeft += e.deltaY;
          }}
        >
          {tabs.map((t) => (
            <button
              key={t.value}
              className={cx("tab", t.value === current && "active")}
              onClick={() => select(t.value)}
            >
              {t.label}
            </button>
          ))}
          <button className="tab" onClick={() => navigate("ai")}>
            <CircleDot style={{ width: 13, height: 13, color: "var(--accent-text)" }} />
            Jarvis AI
          </button>
          <button className="tab" style={{ padding: "0 9px" }} onClick={() => setNewDesk(true)} aria-label="Новый стол">
            <Plus />
          </button>
        </div>
      </PageHeader>
      <div className="page-body">
        {desk && (
          <DeskGrid
            widgets={desk.widgets}
            editing={editing}
            onChange={(widgets) => setWidgets(desk.id, widgets)}
            onAddAt={(cell) => setPickerAt(cell)}
            onConfigure={setConfiguring}
          />
        )}
      </div>
      <NewDeskModal open={newDesk} onClose={() => setNewDesk(false)} />
      <WidgetPicker open={!!pickerAt} onClose={() => setPickerAt(null)} onPick={addWidget} />
      <WidgetSettings
        widget={configuring}
        onClose={() => setConfiguring(null)}
        onSave={(config) =>
          desk && configuring && setWidgets(desk.id, desk.widgets.map((w) => (w.id === configuring.id ? { ...w, config } : w)))
        }
      />
    </div>
  );
}
