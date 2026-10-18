// Редактор команды «НОВАЯ КОМАНДА» / «РЕДАКТИРОВАНИЕ»: название, «Связывать» / «Подтверждать», действия
// (вручную, записью или с помощью AI), фразы для активации и необязательные фразы.
import {
  Check as CheckIcon,
  ChevronDown,
  CirclePlus,
  Clock3,
  Globe,
  Keyboard,
  Rocket,
  Scan,
  Sparkles,
  Square,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { CommandAction, CommandIn, CommandInfo } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { PageHeader } from "@/components/PageHeader";
import { navigate } from "@/lib/router";
import { Button, Check, cx, Empty, notify, notifyError } from "@/ui";
import { AiBuilder, type BuildResult } from "./AiBuilder";
import { ActionList, type DraftAction } from "./ActionList";
import {
  actionIcon,
  countText,
  ICON_CHOICES,
  isRequired,
  newKey,
  parseLines,
  SLOTS,
  W_ACTIONS,
  W_STEPS,
} from "./model";
import { ConfirmDialog, Popover } from "./Overlays";
import { listMemory, useApps, useCatalog, useCommands } from "./store";

interface Draft {
  name: string;
  chain: boolean;
  confirm: boolean;
  icon: string;
  say: string;
  phrases: string;
  optional: string;
  actions: DraftAction[];
  category?: string;
}

const EMPTY: Draft = {
  name: "",
  chain: true,
  confirm: false,
  icon: "",
  say: "",
  phrases: "",
  optional: "",
  actions: [],
};

function fromCommand(cmd: CommandInfo, copy: boolean): Draft {
  return {
    name: copy ? `${cmd.name} (копия)` : cmd.name,
    chain: cmd.chain ?? true,
    confirm: cmd.confirm ?? false,
    icon: cmd.icon ?? "",
    say: cmd.say ?? "",
    phrases: (cmd.phrases ?? []).join("\n"),
    optional: (cmd.optional ?? []).join("\n"),
    actions: (cmd.actions ?? []).map((a) => ({ key: newKey(), type: a.type, value: a.value ?? "" })),
    category: !copy && cmd.source === "user" ? cmd.category : undefined,
  };
}

function snapshot(d: Draft): string {
  return JSON.stringify({ ...d, actions: d.actions.map((a) => [a.type, a.value]) });
}

const QUICK: { type: string; label: string; icon: ReactNode }[] = [
  { type: "Launch.App", label: "Запустить программу", icon: <Rocket /> },
  { type: "Keys.Press", label: "Нажать клавиши", icon: <Keyboard /> },
  { type: "Wait", label: "Пауза", icon: <Clock3 /> },
  { type: "Launch.Url", label: "Открыть сайт", icon: <Globe /> },
];

export function CommandEditor({ mode, id }: { mode: "new" | "edit" | "copy"; id?: string }) {
  const catalog = useCatalog((s) => s.items);
  const byType = useCatalog((s) => s.byType);
  const apps = useApps((s) => s.apps);
  const commands = useCommands((s) => s.items);

  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [state, setState] = useState<"loading" | "ready" | "missing">(mode === "new" ? "ready" : "loading");
  const [source, setSource] = useState<CommandInfo | null>(null);
  const baseline = useRef(snapshot(EMPTY));
  const [tried, setTried] = useState(false);
  const [saving, setSaving] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);
  const [leave, setLeave] = useState<(() => void) | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [autoOpenKey, setAutoOpenKey] = useState<string | null>(null);

  const nameRef = useRef<HTMLInputElement>(null);
  const phrasesRef = useRef<HTMLTextAreaElement>(null);
  const actionsRef = useRef<HTMLDivElement>(null);
  const editBtn = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    useCatalog.getState().load();
    useApps.getState().load();
    if (useCommands.getState().status === "idle") useCommands.getState().load();
  }, []);

  // --- загрузка команды для правки или копии ---
  useEffect(() => {
    setTried(false);
    if (mode === "new") {
      setSource(null);
      setDraft(EMPTY);
      baseline.current = snapshot(EMPTY);
      setState("ready");
      requestAnimationFrame(() => nameRef.current?.focus());
      return;
    }
    if (!id) {
      setState("missing");
      return;
    }
    let alive = true;
    const apply = (cmd: CommandInfo) => {
      const d = fromCommand(cmd, mode === "copy" || cmd.editable === false);
      setSource(cmd);
      setDraft(d);
      baseline.current = snapshot(d);
      setState("ready");
    };
    const local = useCommands.getState().items.find((c) => c.id === id);
    if (local) apply(local);
    else {
      setState("loading");
      api
        .get<CommandInfo>(`/api/commands/${encodeURIComponent(id)}`)
        .then((cmd) => alive && apply(cmd))
        .catch(() => alive && setState("missing"));
    }
    return () => {
      alive = false;
    };
  }, [mode, id]);

  const creating = mode !== "edit" || source?.editable === false;
  const dirty = snapshot(draft) !== baseline.current;
  const phrasesList = useMemo(() => parseLines(draft.phrases), [draft.phrases]);
  const userCommands = useMemo(() => commands.filter((c) => c.source === "user"), [commands]);
  const defaultName = `Новая команда ${userCommands.length + 1}`;
  const errors = {
    name: !draft.name.trim(),
    phrases: phrasesList.length === 0,
    actions: draft.actions.length === 0,
    values: draft.actions.some((a) => isRequired(byType.get(a.type)) && !a.value.trim()),
  };

  const set = useCallback(<K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v })), []);

  // --- действия ---
  const addAction = (type = "Launch.App", open = true) => {
    const t = byType.has(type) ? type : (catalog[0]?.type ?? type);
    const key = newKey();
    setDraft((d) => ({ ...d, actions: [...d.actions, { key, type: t, value: "" }] }));
    if (open) setAutoOpenKey(key);
    requestAnimationFrame(() => {
      const el = actionsRef.current;
      if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    });
  };
  const appendActions = (list: CommandAction[]) =>
    setDraft((d) => ({
      ...d,
      actions: [...d.actions, ...list.map((a) => ({ key: newKey(), type: a.type, value: a.value ?? "" }))],
    }));
  const changeAction = useCallback(
    (key: string, patch: Partial<DraftAction>) =>
      setDraft((d) => ({ ...d, actions: d.actions.map((a) => (a.key === key ? { ...a, ...patch } : a)) })),
    [],
  );
  const moveAction = useCallback(
    (from: number, to: number) =>
      setDraft((d) => {
        const list = d.actions.slice();
        const [it] = list.splice(from, 1);
        list.splice(to, 0, it);
        return { ...d, actions: list };
      }),
    [],
  );
  const removeAction = useCallback(
    (key: string) => setDraft((d) => ({ ...d, actions: d.actions.filter((a) => a.key !== key) })),
    [],
  );
  const duplicateAction = useCallback(
    (key: string) =>
      setDraft((d) => {
        const i = d.actions.findIndex((a) => a.key === key);
        if (i < 0) return d;
        const list = d.actions.slice();
        list.splice(i + 1, 0, { ...d.actions[i], key: newKey() });
        return { ...d, actions: list };
      }),
    [],
  );

  // --- запись действий ---
  const [rec, setRec] = useState<{ count: number; since: number } | null>(null);
  const recRef = useRef(rec);
  recRef.current = rec;
  const stopping = useRef(false);

  const stopRecording = useCallback(async () => {
    if (stopping.current) return;
    stopping.current = true;
    try {
      const res = await api.post<{ actions: CommandAction[] }>("/api/commands/record/stop");
      const list = Array.isArray(res?.actions) ? res.actions : [];
      if (list.length) {
        appendActions(list);
        notify(`Записано: ${countText(list.length, W_ACTIONS)}`, "ok");
      } else notify("Ничего не записано — повторите действия после нажатия «Запись действий»", "info");
    } catch (e) {
      notifyError(e);
    } finally {
      stopping.current = false;
      setRec(null);
    }
  }, []);

  const startRecording = async () => {
    try {
      await api.post("/api/commands/record/start");
      setRec({ count: 0, since: Date.now() });
    } catch (e) {
      notifyError(e);
    }
  };

  useEffect(
    () =>
      onEvent<{ recording: boolean; count: number }>("record.state", (e) => {
        if (e.data.recording) setRec((r) => ({ count: e.data.count ?? 0, since: r?.since ?? Date.now() }));
        else if (recRef.current && !stopping.current) stopRecording();
      }),
    [stopRecording],
  );
  useEffect(
    () => () => {
      // ушли со страницы во время записи — останавливаем её
      if (recRef.current && !stopping.current) api.post("/api/commands/record/stop").catch(() => undefined);
    },
    [],
  );

  // --- AI ---
  const applyAi = (r: BuildResult) => {
    const added = r.actions.length;
    setDraft((d) => {
      const actions = r.actions.map((a) => ({ key: newKey(), type: a.type, value: a.value ?? "" }));
      return {
        ...d,
        name: d.name.trim() ? d.name : r.name || d.name,
        phrases: parseLines([d.phrases, ...(r.phrases ?? [])].join("\n")).join("\n"),
        optional: r.optional?.length ? parseLines([d.optional, ...r.optional].join("\n")).join("\n") : d.optional,
        actions: [...d.actions, ...actions],
        icon: d.icon || r.icon || "",
        say: d.say || r.say || "",
      };
    });
    setAiOpen(false);
    notify(`Команда собрана: ${countText(added, W_STEPS)}`, "ok");
  };

  // --- сохранение и выход ---
  const save = async () => {
    setTried(true);
    if (errors.name || errors.phrases || errors.actions || errors.values) {
      if (errors.name) {
        notify("Укажите название команды", "error");
        nameRef.current?.focus();
      } else if (errors.actions) {
        notify("Добавьте хотя бы одно действие", "error");
      } else if (errors.values) {
        notify("Заполните значения действий, отмеченные красным", "error");
        requestAnimationFrame(() =>
          actionsRef.current?.querySelector(".act-row.invalid")?.scrollIntoView({ block: "nearest", behavior: "smooth" }),
        );
      } else {
        notify("Добавьте хотя бы одну фразу для активации", "error");
        phrasesRef.current?.focus();
      }
      return;
    }
    // enabled есть в CommandIn ядра (по умолчанию true): при правке сохраняем как было
    const body: CommandIn & { enabled: boolean } = {
      enabled: creating ? true : (source?.enabled ?? true),
      name: draft.name.trim(),
      phrases: phrasesList,
      optional: parseLines(draft.optional),
      actions: draft.actions.map((a) => ({ type: a.type, value: a.value.trim() })),
      confirm: draft.confirm,
      chain: draft.chain,
      icon: draft.icon,
      say: draft.say.trim(),
      ...(draft.category ? { category: draft.category } : {}),
    };
    setSaving(true);
    try {
      const res =
        !creating && id
          ? await api.put<CommandInfo>(`/api/commands/${encodeURIComponent(id)}`, body)
          : await api.post<CommandInfo>("/api/commands", body);
      if (res && typeof res === "object" && "id" in res) {
        useCommands.getState().upsert(res);
        listMemory.highlight = res.id;
      }
      baseline.current = snapshot(draft);
      notify("Команда сохранена", "ok");
      navigate("commands");
    } catch (e) {
      notifyError(e);
    } finally {
      setSaving(false);
    }
  };

  const guard = (go: () => void) => {
    if (dirty && !saving) setLeave(() => go);
    else go();
  };
  const cancel = () => guard(() => navigate("commands"));

  // Ctrl+S — сохранить (кроме окна «Создать с помощью AI»)
  const saveRef = useRef(save);
  saveRef.current = save;
  const aiOpenRef = useRef(aiOpen);
  aiOpenRef.current = aiOpen;
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (aiOpenRef.current) return;
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.code === "KeyS") {
        e.preventDefault();
        saveRef.current();
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);

  const insertSlot = (slot: string) => {
    const el = phrasesRef.current;
    const ins = `{${slot}}`;
    const v = draft.phrases;
    const start = el?.selectionStart ?? v.length;
    const end = el?.selectionEnd ?? v.length;
    const before = v.slice(0, start);
    const pad = before && !/[\s\n]$/.test(before) ? " " : "";
    set("phrases", before + pad + ins + v.slice(end));
    requestAnimationFrame(() => {
      el?.focus();
      const pos = start + pad.length + ins.length;
      el?.setSelectionRange(pos, pos);
    });
  };

  const autoIcon = draft.actions[0]
    ? actionIcon(draft.actions[0], byType.get(draft.actions[0].type), apps)
    : "command";
  const pillName = draft.name.trim() || (creating ? defaultName : source?.name || defaultName);

  if (state !== "ready") {
    return (
      <div className="page">
        <PageHeader title={mode === "edit" ? "Редактирование" : "Новая команда"} />
        <div className="page-body">
          {state === "loading" ? (
            <div className="ed-loading">
              <span className="spinner" /> Загружаю команду…
            </div>
          ) : (
            <Empty icon={<Sparkles />}>
              Команда не найдена — возможно, её удалили.
              <div style={{ marginTop: 12 }}>
                <Button onClick={() => navigate("commands")}>К списку команд</Button>
              </div>
            </Empty>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="page cmd-editor-page">
      <PageHeader title={creating ? "Новая команда" : "Редактирование"} />
      <div className="ed-bar">
        <button
          ref={editBtn}
          type="button"
          className="btn ed-bar-btn"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((o) => !o)}
        >
          Редактировать
          <ChevronDown className="ed-chev" />
        </button>
        <button
          type="button"
          className="btn btn-primary ed-bar-btn ed-pill"
          title="Название команды"
          onClick={() => nameRef.current?.focus()}
        >
          <span className="ellipsis">{pillName}</span>
        </button>
        <span className="ed-dot" aria-hidden />
        <button type="button" className="btn ed-bar-btn" onClick={cancel}>
          Отмена
        </button>
        <Button variant="primary" className="ed-bar-btn" loading={saving} onClick={save} title="Сохранить (Ctrl+S)">
          Сохранить
        </Button>
        <span className="spacer" />
        {dirty && <span className="ed-dirty">Есть несохранённые изменения</span>}
        {source && creating && source.source !== "user" && (
          <span className="ed-dirty">Копия: {source.name}</span>
        )}
      </div>
      <Popover anchor={editBtn} open={menuOpen} onClose={() => setMenuOpen(false)} width={300} maxHeight={360}>
        <div className="pop-list" role="menu" aria-label="Мои команды">
          <button
            type="button"
            role="menuitem"
            className="pop-item"
            onClick={() => {
              setMenuOpen(false);
              guard(() => {
                if (mode !== "new") return navigate("commands", "new");
                // уже на «Новой команде» — просто начинаем заново
                setDraft(EMPTY);
                baseline.current = snapshot(EMPTY);
                setTried(false);
                requestAnimationFrame(() => nameRef.current?.focus());
              });
            }}
          >
            <span className="pop-plus">
              <CirclePlus />
            </span>
            <span className="grow pop-title">Новая команда</span>
          </button>
          <div className="pop-group">Мои команды</div>
          {userCommands.length === 0 && <div className="pop-empty">Своих команд пока нет</div>}
          {userCommands.map((c) => (
            <button
              key={c.id}
              type="button"
              role="menuitem"
              className={cx("pop-item", c.id === id && !creating && "selected")}
              onClick={() => {
                setMenuOpen(false);
                if (c.id !== id || creating) guard(() => navigate("commands", "edit", c.id));
              }}
            >
              <AppIcon name={c.icon || "command"} size={26} radius={8} />
              <span className="grow">
                <span className="pop-title ellipsis">{c.name}</span>
                <span className="pop-sub ellipsis">{c.phrases[0] ?? "без фраз"}</span>
              </span>
              {c.id === id && !creating && <CheckIcon className="pop-check" />}
            </button>
          ))}
        </div>
      </Popover>

      <div className="page-body fill">
        <div className="cmd-editor">
          <div className="ed-name">
            <div className="ed-field ed-field-name">
              <label className="field-label" htmlFor="cmd-name">
                Название
              </label>
              <div className="ed-name-input">
                <IconPicker value={draft.icon} auto={autoIcon} onChange={(v) => set("icon", v)} />
                <input
                  id="cmd-name"
                  ref={nameRef}
                  className={cx("input", tried && errors.name && "invalid")}
                  value={draft.name}
                  placeholder="Например: Режим стрима"
                  spellCheck={false}
                  maxLength={80}
                  onChange={(e) => set("name", e.target.value)}
                />
              </div>
            </div>
            <div className="ed-field ed-field-say">
              <label className="field-label" htmlFor="cmd-say">
                Что сказать после выполнения
              </label>
              <input
                id="cmd-say"
                className="input"
                value={draft.say}
                placeholder="Пусто — короткое «Готово»"
                spellCheck={false}
                onChange={(e) => set("say", e.target.value)}
              />
            </div>
            <div className="ed-checks">
              <span title="Команду можно соединять с другими в одной фразе: «сверни все окна и включи музыку»">
                <Check chip checked={draft.chain} onChange={(v) => set("chain", v)}>
                  Связывать
                </Check>
              </span>
              <span title="Спрашивать подтверждение перед выполнением">
                <Check chip checked={draft.confirm} onChange={(v) => set("confirm", v)}>
                  Подтверждать
                </Check>
              </span>
            </div>
          </div>

          <div className="ed-divider" />

          <div className="ed-tools">
            <Button icon={<CirclePlus />} onClick={() => addAction()}>
              Действие
            </Button>
            <Button
              icon={rec ? <Square /> : <Scan />}
              className={cx(rec && "rec-btn")}
              onClick={rec ? stopRecording : startRecording}
            >
              {rec ? "Остановить запись" : "Запись действий"}
            </Button>
            <Button icon={<Sparkles />} onClick={() => setAiOpen(true)}>
              Создать с помощью AI
            </Button>
            <span className="spacer" />
            {draft.actions.length > 0 && (
              <span className={cx("ed-count", tried && errors.values && "err")}>
                {countText(draft.actions.length, W_ACTIONS)}
              </span>
            )}
          </div>

          <div className={cx("ed-actions", tried && errors.actions && "invalid")} ref={actionsRef}>
            {rec && <RecordBanner count={rec.count} since={rec.since} onStop={stopRecording} />}
            {draft.actions.length > 0 ? (
              <ActionList
                actions={draft.actions}
                catalog={catalog}
                byType={byType}
                phrases={draft.phrases}
                tried={tried}
                autoOpenKey={autoOpenKey}
                onAutoOpened={() => setAutoOpenKey(null)}
                onChange={changeAction}
                onMove={moveAction}
                onRemove={removeAction}
                onDuplicate={duplicateAction}
              />
            ) : (
              !rec && (
                <div className="ed-empty">
                  <div className="ed-empty-text">
                    Добавьте действие, запишите свои действия или опишите команду словами — Jarvis соберёт её сам.
                  </div>
                  <div className="ed-quick">
                    {QUICK.map((q) => (
                      <button key={q.type} type="button" className="ed-quick-btn" onClick={() => addAction(q.type, false)}>
                        {q.icon}
                        {q.label}
                      </button>
                    ))}
                  </div>
                </div>
              )
            )}
          </div>

          <div className="ed-phrases">
            <div className="ed-field">
              <label className="field-label" htmlFor="cmd-phrases">
                Фразы для активации
              </label>
              <textarea
                id="cmd-phrases"
                ref={phrasesRef}
                className={cx("textarea", tried && errors.phrases && "invalid")}
                value={draft.phrases}
                placeholder="Необходимо произнести для активации команды"
                spellCheck={false}
                onChange={(e) => set("phrases", e.target.value)}
              />
              <div className="ed-slots" aria-label="Вставить слот во фразу">
                <span className="faint">По одной на строке. Слоты:</span>
                {SLOTS.map((s) => (
                  <button
                    key={s.key}
                    type="button"
                    className="slot-chip"
                    title={`${s.label}: ${s.hint}`}
                    onClick={() => insertSlot(s.key)}
                  >{`{${s.key}}`}</button>
                ))}
              </div>
            </div>
            <div className="ed-field">
              <label className="field-label" htmlFor="cmd-optional">
                Необязательные фразы
              </label>
              <textarea
                id="cmd-optional"
                className="textarea"
                value={draft.optional}
                placeholder="Можно произнести вместе с обязательной частью команды"
                spellCheck={false}
                onChange={(e) => set("optional", e.target.value)}
              />
              <div className="ed-slots">
                <span className="faint">Например: «пожалуйста», «быстро», «Джарвис»</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <AiBuilder open={aiOpen} onClose={() => setAiOpen(false)} onApply={applyAi} />
      <ConfirmDialog
        open={!!leave}
        title="Выйти без сохранения?"
        confirmText="Выйти"
        cancelText="Остаться"
        danger
        onClose={() => setLeave(null)}
        onConfirm={() => {
          const go = leave;
          setLeave(null);
          baseline.current = snapshot(draft);
          go?.();
        }}
      >
        Изменения в команде «{pillName}» пропадут.
      </ConfirmDialog>
    </div>
  );
}

// --- запись действий ----------------------------------------------------------------------------------
function RecordBanner({ count, since, onStop }: { count: number; since: number; onStop: () => void }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, []);
  const s = Math.max(0, Math.floor((now - since) / 1000));
  const time = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  return (
    <div className="rec-banner" role="status" aria-live="polite">
      <span className="rec-dot" aria-hidden />
      <div className="grow">
        <div className="rec-title">
          Идёт запись действий <span className="rec-time">{time}</span>
        </div>
        <div className="rec-sub">
          Запускайте программы, нажимайте клавиши и кликайте — Jarvis повторит это по команде.
        </div>
      </div>
      <span className="rec-count">
        <b>{count}</b> {countText(count, W_ACTIONS).replace(/^\S+\s/, "")}
      </span>
      <button type="button" className="btn btn-danger btn-sm" onClick={onStop}>
        <Square />
        Стоп
      </button>
    </div>
  );
}

// --- значок команды ------------------------------------------------------------------------------------
function IconPicker({ value, auto, onChange }: { value: string; auto: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button
        ref={btn}
        type="button"
        className="ed-icon-btn"
        title="Значок команды"
        aria-label="Значок команды"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <AppIcon name={value || auto} size={38} radius={10} />
        {!value && <span className="ed-icon-auto">A</span>}
      </button>
      <Popover anchor={btn} open={open} onClose={() => setOpen(false)} width={318} maxHeight={330} className="icon-pop">
        <div className="icon-pop-head">Значок команды</div>
        <div className="icon-grid" role="listbox" aria-label="Значки">
          {ICON_CHOICES.map((c) => (
            <button
              key={c.key || "auto"}
              type="button"
              role="option"
              aria-selected={value === c.key}
              title={c.label}
              aria-label={c.label}
              className={cx("icon-cell", value === c.key && "selected")}
              onClick={() => {
                onChange(c.key);
                setOpen(false);
                btn.current?.focus();
              }}
            >
              <AppIcon name={c.key || auto} size={32} radius={9} />
              {!c.key && <span className="ed-icon-auto">A</span>}
            </button>
          ))}
        </div>
      </Popover>
    </>
  );
}
