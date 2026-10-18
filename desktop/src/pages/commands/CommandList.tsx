// Список команд: категории слева, поиск, «Проверить фразу», строки команд (включатель, «Выполнить»,
// правка / копия в «Мои», удаление). Показываем по 100 строк — дальше подгружаем при прокрутке.
import {
  AudioLines,
  CirclePlay,
  CopyPlus,
  Package,
  Pencil,
  Play,
  Plus,
  RotateCw,
  Search,
  SearchX,
  Star,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react";
import { memo, type RefObject, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { ActionType, AppEntry, CommandInfo, ExecResult } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { navigate } from "@/lib/router";
import { Button, cx, IconButton, notify, notifyError, Tabs, Toggle, useDebounced } from "@/ui";
import {
  actionIcon,
  actionLine,
  buildCategories,
  categoryIcon,
  categoryKey,
  countText,
  inCategory,
  matchesQuery,
  norm,
  slotLabel,
  sourceLabel,
  valueText,
  W_COMMANDS,
} from "./model";
import { ConfirmDialog, Popover } from "./Overlays";
import { SectionHeader } from "./SectionHeader";
import { listMemory, PAGE, useApps, useCatalog, useCommands, usePackNames, usePacks } from "./store";

type StateFilter = "all" | "on" | "off";

const SOURCE_ORDER: Record<CommandInfo["source"], number> = { user: 0, pack: 1, dynamic: 2, builtin: 3 };

/** Тост по итогу выполнения: «Включить музыку — Выполнено ✓». */
function notifyResult(res: ExecResult | undefined, fallbackTitle: string) {
  const title = res?.title || fallbackTitle;
  if (res?.data?.cancelled) notify(`${title} — отменено`, "info");
  else if (res?.ok === false) notify(`${title} — ${res.message || "не получилось"}`, "error");
  else notify(`${title} — ${res?.message || "Выполнено"} ✓`, "ok");
}

export function CommandList() {
  const items = useCommands((s) => s.items);
  const serverCats = useCommands((s) => s.categories);
  const status = useCommands((s) => s.status);
  const error = useCommands((s) => s.error);
  const byType = useCatalog((s) => s.byType);
  const apps = useApps((s) => s.apps);
  const packNames = usePackNames();

  const [category, setCategory] = useState(listMemory.category);
  const [query, setQuery] = useState(listMemory.query);
  const [stateFilter, setStateFilter] = useState<StateFilter>(listMemory.state);
  const [shown, setShown] = useState(listMemory.shown);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [highlight, setHighlight] = useState("");
  const [deleting, setDeleting] = useState<CommandInfo | null>(null);
  const [busyDelete, setBusyDelete] = useState(false);
  const [running, setRunning] = useState<Record<string, boolean>>({});
  const [focusTick, setFocusTick] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinel = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const st = useCommands.getState();
    st.load(st.status === "ready");
    useCatalog.getState().load();
    useApps.getState().load();
    if (usePacks.getState().status === "idle") usePacks.getState().load();
  }, []);

  // запоминаем фильтры и прокрутку до возвращения из редактора
  useEffect(() => {
    Object.assign(listMemory, { category, query, state: stateFilter, shown });
  }, [category, query, stateFilter, shown]);
  useEffect(() => {
    const el = scrollRef.current;
    return () => {
      listMemory.scrollTop = el?.scrollTop ?? 0;
    };
  }, []);

  const words = useMemo(() => norm(query).split(/\s+/).filter(Boolean), [query]);
  // свои команды — первыми, затем паки, динамические и встроенные (внутри — порядок ядра)
  const ordered = useMemo(() => [...items].sort((a, b) => (SOURCE_ORDER[a.source] ?? 9) - (SOURCE_ORDER[b.source] ?? 9)), [items]);
  const base = useMemo(() => {
    const found = ordered.filter(
      (c) =>
        (stateFilter === "all" || (stateFilter === "on" ? c.enabled : !c.enabled)) &&
        matchesQuery(c, words, c.pack ? packNames.get(c.pack) : undefined),
    );
    if (!words.length) return found;
    // при поиске выше — совпадения в названии, потом во фразах
    const score = (c: CommandInfo) => {
      const name = norm(c.name);
      if (words.every((w) => name.includes(w))) return name.startsWith(words[0]) ? 0 : 1;
      return c.phrases.some((p) => words.every((w) => norm(p).includes(w))) ? 2 : 3;
    };
    return found.map((c) => ({ c, s: score(c) })).sort((x, y) => x.s - y.s).map((x) => x.c);
  }, [ordered, words, stateFilter, packNames]);
  const cats = useMemo(
    () =>
      buildCategories(items, serverCats).map((c) => ({ ...c, count: base.filter((x) => inCategory(x, c.id)).length })),
    [items, serverCats, base],
  );
  const activeCat = cats.some((c) => c.id === category) ? category : "all";
  const filtered = useMemo(() => base.filter((c) => inCategory(c, activeCat)), [base, activeCat]);
  const visible = filtered.slice(0, shown);

  // новый фильтр — снова первые 100 строк
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setShown(PAGE);
    scrollRef.current?.scrollTo({ top: 0 });
  }, [activeCat, query, stateFilter]);

  // вернуть прокрутку после редактора
  const restored = useRef(false);
  useLayoutEffect(() => {
    if (restored.current || status !== "ready") return;
    restored.current = true;
    if (scrollRef.current && listMemory.scrollTop && !listMemory.highlight) scrollRef.current.scrollTop = listMemory.scrollTop;
  }, [status]);

  // подсветить только что сохранённую команду
  useEffect(() => {
    const id = listMemory.highlight;
    if (!id || status !== "ready") return;
    const idx = filtered.findIndex((c) => c.id === id);
    if (idx < 0) {
      if (!items.some((c) => c.id === id) || (activeCat === "all" && !query && stateFilter === "all")) {
        listMemory.highlight = "";
        return;
      }
      setCategory("all");
      setQuery("");
      setStateFilter("all");
      return;
    }
    listMemory.highlight = "";
    if (idx >= shown) setShown(Math.ceil((idx + 1) / PAGE) * PAGE);
    setHighlight(id);
    setTimeout(
      () =>
        scrollRef.current
          ?.querySelector(`[data-cmd="${CSS.escape(id)}"]`)
          ?.scrollIntoView({ block: "center", behavior: "smooth" }),
      60,
    );
    const t = setTimeout(() => setHighlight(""), 2400);
    return () => clearTimeout(t);
  }, [status, filtered, items, activeCat, query, stateFilter, shown, focusTick]);

  // подгрузка при прокрутке
  useEffect(() => {
    const el = sentinel.current;
    const root = scrollRef.current;
    if (!el || !root || visible.length >= filtered.length) return;
    const io = new IntersectionObserver(
      (entries) => entries[0]?.isIntersecting && setShown((s) => Math.min(filtered.length, s + PAGE)),
      { root, rootMargin: "500px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [filtered.length, visible.length]);

  const run = useCallback(async (cmd: CommandInfo, slots?: Record<string, string | number>) => {
    setRunning((r) => ({ ...r, [cmd.id]: true }));
    try {
      const res = await api.post<ExecResult>(`/api/commands/${encodeURIComponent(cmd.id)}/run`, slots ? { slots } : {});
      notifyResult(res, cmd.name);
    } catch (e) {
      notifyError(e);
    } finally {
      setRunning((r) => {
        const next = { ...r };
        delete next[cmd.id];
        return next;
      });
    }
  }, []);
  const toggle = useCallback((cmd: CommandInfo, v: boolean) => {
    useCommands
      .getState()
      .setEnabled(cmd.id, v)
      .catch((e) => notifyError(e));
  }, []);
  const edit = useCallback((cmd: CommandInfo) => {
    const own = cmd.source === "user" && cmd.editable !== false;
    navigate("commands", own ? "edit" : "copy", cmd.id);
  }, []);
  const expand = useCallback((id: string) => setExpanded((e) => (e === id ? null : id)), []);

  const showCommand = useCallback(
    (id: string) => {
      listMemory.highlight = id;
      if (!filtered.some((c) => c.id === id)) {
        setCategory("all");
        setQuery("");
        setStateFilter("all");
      }
      setFocusTick((t) => t + 1);
      setExpanded(id);
    },
    [filtered],
  );

  const confirmDelete = async () => {
    if (!deleting) return;
    setBusyDelete(true);
    try {
      await useCommands.getState().remove(deleting.id);
      notify(`Команда «${deleting.name}» удалена`, "ok");
      setDeleting(null);
    } catch (e) {
      notifyError(e);
    } finally {
      setBusyDelete(false);
    }
  };

  const activeTitle = cats.find((c) => c.id === activeCat)?.title ?? "Все";
  const mineCat = categoryKey(activeCat) === "user";

  return (
    <div className="page">
      <SectionHeader
        tab="list"
        actions={
          <Button variant="primary" icon={<Plus />} onClick={() => navigate("commands", "new")}>
            Новая команда
          </Button>
        }
      />
      <div className="page-body fill cmd-layout">
        <nav className="cmd-cats" aria-label="Категории команд">
          {status === "loading" && !items.length
            ? Array.from({ length: 10 }, (_, i) => <div key={i} className="cmd-cat skeleton" />)
            : cats.map((c) => {
                const Icon = categoryIcon(c.id, c.title);
                const on = activeCat === c.id;
                return (
                  <button
                    key={c.id}
                    type="button"
                    className={cx("cmd-cat", on && "active", c.count === 0 && "zero")}
                    aria-current={on ? "true" : undefined}
                    onClick={() => setCategory(c.id)}
                  >
                    <Icon />
                    <span className="grow ellipsis">{c.title}</span>
                    <span className="n">{c.count}</span>
                  </button>
                );
              })}
        </nav>

        <section className="cmd-main" aria-label={`Команды: ${activeTitle}`}>
          <div className="cmd-toolbar">
            <div className="input-wrap grow">
              <Search />
              <input
                ref={searchRef}
                className="input"
                value={query}
                placeholder="Поиск по названию и фразам"
                spellCheck={false}
                aria-label="Поиск команд"
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Escape" && query && (e.stopPropagation(), setQuery(""))}
              />
              {query && (
                <button type="button" className="cmd-clear" aria-label="Очистить поиск" onClick={() => setQuery("")}>
                  <X />
                </button>
              )}
            </div>
            <Tabs<StateFilter>
              value={stateFilter}
              onChange={setStateFilter}
              items={[
                { value: "all", label: "Все" },
                { value: "on", label: "Включены" },
                { value: "off", label: "Выключены" },
              ]}
            />
          </div>

          <TestBox onShow={showCommand} />

          <div className="cmd-scroll" ref={scrollRef}>
            {status === "error" && !items.length ? (
              <div className="cmd-state">
                <TriangleAlert />
                <div className="cmd-state-title">Не удалось загрузить команды</div>
                <div className="faint small">{error}</div>
                <Button size="sm" icon={<RotateCw />} onClick={() => useCommands.getState().load()}>
                  Повторить
                </Button>
              </div>
            ) : status !== "ready" && !items.length ? (
              <div className="cmd-rows">
                {Array.from({ length: 8 }, (_, i) => (
                  <div key={i} className="cmd-row skeleton" style={{ animationDelay: `${i * 60}ms` }} />
                ))}
              </div>
            ) : filtered.length === 0 ? (
              <div className="cmd-state">
                {query ? <SearchX /> : mineCat ? <Star /> : <Package />}
                <div className="cmd-state-title">
                  {query
                    ? `Ничего не нашлось по запросу «${query.trim()}»`
                    : mineCat
                      ? "Своих команд пока нет"
                      : stateFilter !== "all"
                        ? "Здесь пусто"
                        : "В этой категории пока нет команд"}
                </div>
                <div className="faint small">
                  {query
                    ? "Попробуйте другое слово или поищите во всех категориях."
                    : mineCat
                      ? "Соберите свою: запуск программ, клавиши, клики, громкость — в любом порядке."
                      : "Установите паки команд — в них десятки готовых команд для программ."}
                </div>
                {query ? (
                  <Button size="sm" onClick={() => setQuery("")}>
                    Сбросить поиск
                  </Button>
                ) : mineCat ? (
                  <Button size="sm" variant="primary" icon={<Plus />} onClick={() => navigate("commands", "new")}>
                    Новая команда
                  </Button>
                ) : (
                  <Button size="sm" icon={<Package />} onClick={() => navigate("commands", "packs")}>
                    Паки команд
                  </Button>
                )}
              </div>
            ) : (
              <div className="cmd-rows">
                {visible.map((cmd) => (
                  <CommandRow
                    key={cmd.id}
                    cmd={cmd}
                    packName={cmd.pack ? packNames.get(cmd.pack) : undefined}
                    expanded={expanded === cmd.id}
                    highlight={highlight === cmd.id}
                    running={!!running[cmd.id]}
                    byType={byType}
                    apps={apps}
                    onRun={run}
                    onToggle={toggle}
                    onEdit={edit}
                    onDelete={setDeleting}
                    onExpand={expand}
                  />
                ))}
                <div ref={sentinel} className="cmd-foot">
                  <span className="faint small">
                    Показано {visible.length.toLocaleString("ru-RU")} из {countText(filtered.length, W_COMMANDS)}
                  </span>
                  {visible.length < filtered.length && (
                    <Button size="sm" onClick={() => setShown((s) => s + PAGE)}>
                      Показать ещё
                    </Button>
                  )}
                </div>
              </div>
            )}
          </div>
        </section>
      </div>

      <ConfirmDialog
        open={!!deleting}
        title="Удалить команду?"
        confirmText="Удалить"
        danger
        busy={busyDelete}
        onClose={() => setDeleting(null)}
        onConfirm={confirmDelete}
      >
        Команда «{deleting?.name}» удалится насовсем.
      </ConfirmDialog>
    </div>
  );
}

// --- строка команды ------------------------------------------------------------------------------------
const CommandRow = memo(function CommandRow({
  cmd,
  packName,
  expanded,
  highlight,
  running,
  byType,
  apps,
  onRun,
  onToggle,
  onEdit,
  onDelete,
  onExpand,
}: {
  cmd: CommandInfo;
  packName?: string;
  expanded: boolean;
  highlight: boolean;
  running: boolean;
  byType: Map<string, ActionType>;
  apps: AppEntry[];
  onRun: (cmd: CommandInfo, slots?: Record<string, string | number>) => void;
  onToggle: (cmd: CommandInfo, v: boolean) => void;
  onEdit: (cmd: CommandInfo) => void;
  onDelete: (cmd: CommandInfo) => void;
  onExpand: (id: string) => void;
}) {
  const src = sourceLabel(cmd, packName);
  const firstAction = cmd.actions[0];
  const at = firstAction ? byType.get(firstAction.type) : undefined;
  const summary = firstAction
    ? actionLine(firstAction, at, apps)
    : cmd.source === "dynamic"
      ? "Выполняет модуль Jarvis"
      : "Без действий";
  const more = cmd.actions.length - 1;
  const icon = cmd.icon || (firstAction ? actionIcon(firstAction, at, apps) : "command");
  const own = cmd.source === "user" && cmd.editable !== false;
  const detailsId = `cmd-d-${cmd.id}`;
  const needs = useMemo(() => slotsUsed(cmd), [cmd]);
  const runBtn = useRef<HTMLButtonElement>(null);
  const [ask, setAsk] = useState(false);
  return (
    <div className={cx("cmd-row", !cmd.enabled && "off", expanded && "open", highlight && "flash")} data-cmd={cmd.id}>
      <div className="cmd-row-main">
        <button
          type="button"
          className="cmd-row-hit"
          aria-expanded={expanded}
          aria-controls={expanded ? detailsId : undefined}
          title={expanded ? "Свернуть" : "Подробнее"}
          onClick={() => onExpand(cmd.id)}
        >
          <span className="cmd-row-icon">
            <AppIcon name={icon} size={38} radius={11} />
          </span>
          <span className="cmd-row-text">
            <span className="cmd-row-title">
              <span className="cmd-name ellipsis">{cmd.name}</span>
              <span className={cx("cmd-src", src.tone)} title={src.text}>
                {src.tone === "pack" && <Package />}
                {src.tone === "mine" && <Star />}
                <span className="ellipsis">{src.text}</span>
              </span>
            </span>
            <PhraseChips phrases={cmd.phrases} />
          </span>
          <span className="cmd-row-summary" title={cmd.actions.map((a) => actionLine(a, byType.get(a.type), apps)).join(" → ")}>
            <span className="ellipsis">{summary}</span>
            {more > 0 && <span className="cmd-more-n">+{more}</span>}
          </span>
        </button>
        <div className="cmd-row-actions">
          <button
            ref={runBtn}
            type="button"
            className="btn btn-sm btn-ghost cmd-run"
            disabled={running}
            aria-haspopup={needs.length ? "dialog" : undefined}
            aria-expanded={needs.length ? ask : undefined}
            onClick={() => (needs.length ? setAsk((a) => !a) : onRun(cmd))}
            title={`Выполнить «${cmd.name}»`}
          >
            {running ? <span className="spinner" /> : <Play />}
            Выполнить
          </button>
          {needs.length > 0 && (
            <SlotsPopover
              anchor={runBtn}
              open={ask}
              title={cmd.name}
              slots={needs}
              onClose={() => setAsk(false)}
              onRun={(slots) => {
                setAsk(false);
                onRun(cmd, slots);
              }}
            />
          )}
          {own ? (
            <IconButton size="sm" variant="ghost" icon={<Pencil />} label="Изменить" onClick={() => onEdit(cmd)} />
          ) : (
            <IconButton
              size="sm"
              variant="ghost"
              icon={<CopyPlus />}
              label="Скопировать в «Мои» и изменить"
              disabled={!cmd.actions.length}
              onClick={() => onEdit(cmd)}
            />
          )}
          {own && (
            <IconButton size="sm" variant="ghost" icon={<Trash2 />} label="Удалить" onClick={() => onDelete(cmd)} />
          )}
          <Toggle
            checked={cmd.enabled}
            onChange={(v) => onToggle(cmd, v)}
            label={cmd.enabled ? `Выключить «${cmd.name}»` : `Включить «${cmd.name}»`}
          />
        </div>
      </div>
      {expanded && <CommandDetails id={detailsId} cmd={cmd} byType={byType} apps={apps} />}
    </div>
  );
});

/** Слоты, которые команда берёт из фразы ({number}, {app}…), — их спросим перед «Выполнить». */
function slotsUsed(cmd: CommandInfo): string[] {
  const out = new Set<string>();
  for (const a of cmd.actions) for (const m of (a.value ?? "").matchAll(/\{(\w+)\}/g)) out.add(m[1]);
  return [...out];
}

function SlotsPopover({
  anchor,
  open,
  title,
  slots,
  onClose,
  onRun,
}: {
  anchor: RefObject<HTMLButtonElement | null>;
  open: boolean;
  title: string;
  slots: string[];
  onClose: () => void;
  onRun: (slots: Record<string, string | number>) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  useEffect(() => {
    if (open) setValues({});
  }, [open]);
  const submit = () => {
    const out: Record<string, string | number> = {};
    for (const k of slots) {
      const v = (values[k] ?? "").trim();
      if (!v) continue;
      out[k] = /^-?\d+([.,]\d+)?$/.test(v) ? Number(v.replace(",", ".")) : v;
    }
    onRun(out);
  };
  return (
    <Popover anchor={anchor} open={open} onClose={onClose} width={280} align="end" className="slots-pop">
      <form
        className="slots-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="slots-title ellipsis">Выполнить «{title}»</div>
        {slots.map((k, i) => (
          <label key={k} className="slots-field">
            <span className="slots-label">
              {slotLabel(k)} <span className="slot-tag">{`{${k}}`}</span>
            </span>
            <input
              className="input"
              autoFocus={i === 0}
              value={values[k] ?? ""}
              inputMode={k === "number" ? "decimal" : undefined}
              placeholder={k === "number" ? "50" : k === "app" ? "Spotify" : k === "site" ? "ютуб" : "текст"}
              spellCheck={false}
              onChange={(e) => setValues((v) => ({ ...v, [k]: e.target.value }))}
            />
          </label>
        ))}
        <button type="submit" className="btn btn-primary btn-sm btn-block">
          <Play />
          Выполнить
        </button>
      </form>
    </Popover>
  );
}

/** Фразы строкой: не поместившиеся прячутся, вместо них — «+N» сразу за последней видимой. */
function PhraseChips({ phrases }: { phrases: string[] }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [fit, setFit] = useState({ hidden: 0, left: 0 });
  const shown = phrases.slice(0, 4);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const chips = Array.from(el.children) as HTMLElement[];
      if (!chips.length) return;
      const top = chips[0].offsetTop;
      const visible = chips.filter((c) => c.offsetTop <= top + 2);
      const last = visible[visible.length - 1] ?? chips[0];
      const next = { hidden: chips.length - visible.length, left: last.offsetLeft + last.offsetWidth + 6 };
      setFit((f) => (f.hidden === next.hidden && f.left === next.left ? f : next));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [phrases]);
  if (!phrases.length) return <span className="faint tiny">Нет фраз</span>;
  const extra = phrases.length - shown.length + fit.hidden;
  return (
    <span className={cx("cmd-row-phrases-line", phrases.length > 1 && "reserve")}>
      <span ref={ref} className="cmd-row-phrases">
        {shown.map((p) => (
          <span key={p} className="cmd-phrase">
            {p}
          </span>
        ))}
      </span>
      {extra > 0 && (
        <span
          className="cmd-phrase more"
          style={{ left: fit.left }}
          title={phrases.slice(shown.length - fit.hidden).join(" · ")}
        >
          +{extra}
        </span>
      )}
    </span>
  );
}

function CommandDetails({
  id,
  cmd,
  byType,
  apps,
}: {
  id: string;
  cmd: CommandInfo;
  byType: Map<string, ActionType>;
  apps: AppEntry[];
}) {
  return (
    <div className="cmd-details" id={id}>
      <div className="cmd-det-col">
        <div className="cmd-det-label">Фразы для активации</div>
        <div className="cmd-det-chips">
          {cmd.phrases.length ? (
            cmd.phrases.map((p) => (
              <span key={p} className="cmd-phrase">
                {p}
              </span>
            ))
          ) : (
            <span className="faint small">Нет фраз</span>
          )}
        </div>
        {cmd.optional.length > 0 && (
          <>
            <div className="cmd-det-label">Необязательные фразы</div>
            <div className="cmd-det-chips">
              {cmd.optional.map((p) => (
                <span key={p} className="cmd-phrase soft">
                  {p}
                </span>
              ))}
            </div>
          </>
        )}
        <div className="cmd-det-flags">
          {cmd.chain && <span className="chip">Связывать</span>}
          {cmd.confirm && <span className="chip">Подтверждать</span>}
          {cmd.say && <span className="chip">Ответ: «{cmd.say}»</span>}
          <span className="chip mono-chip">{cmd.id}</span>
        </div>
      </div>
      <div className="cmd-det-col">
        <div className="cmd-det-label">Действия</div>
        {cmd.actions.length ? (
          <ol className="cmd-det-steps">
            {cmd.actions.map((a, i) => {
              const at = byType.get(a.type);
              const v = valueText(a, at, apps);
              return (
                <li key={i}>
                  <span className="n">{i + 1}</span>
                  <AppIcon name={actionIcon(a, at, apps)} size={24} radius={7} />
                  <span className="grow ellipsis">
                    {at?.title ?? a.type}
                    {v && <span className="faint"> · {v}</span>}
                  </span>
                  <code>{a.type}</code>
                </li>
              );
            })}
          </ol>
        ) : (
          <div className="faint small">Команду выполняет модуль Jarvis — действий для правки нет.</div>
        )}
      </div>
    </div>
  );
}

// --- «Проверить фразу» ---------------------------------------------------------------------------------
interface TestPart {
  command_id: string;
  title: string;
  text: string;
  score: number;
  slots: Record<string, unknown>;
}

function TestBox({ onShow }: { onShow: (id: string) => void }) {
  const [text, setText] = useState("");
  const [res, setRes] = useState<{ text: string; parts: TestPart[] } | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [running, setRunning] = useState(false);
  const req = useRef(0);
  const items = useCommands((s) => s.items);

  const test = useCallback(async (t: string) => {
    const q = t.trim();
    const my = ++req.current;
    if (!q) {
      setRes(undefined);
      setBusy(false);
      setErr("");
      return;
    }
    setBusy(true);
    try {
      const r = await api.post<{ match: { text: string; parts: TestPart[] } | null }>("/api/commands/test", { text: q });
      if (my !== req.current) return;
      setRes(r?.match && Array.isArray(r.match.parts) && r.match.parts.length ? r.match : null);
      setErr("");
    } catch (e) {
      if (my !== req.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      setRes(undefined);
    } finally {
      if (my === req.current) setBusy(false);
    }
  }, []);
  const testLater = useDebounced(test, 350);

  const runPhrase = async () => {
    const q = text.trim();
    if (!q) return;
    setRunning(true);
    try {
      const r = await api.post<{ kind: string; result?: ExecResult }>("/api/dispatch", { text: q, source: "ui" });
      if (r?.kind === "command" && r.result) notifyResult(r.result, q);
      else if (r?.kind === "ai") notify("Фразу обработал Jarvis AI", "info");
      else notify("Ни одна команда не подошла", "info");
    } catch (e) {
      notifyError(e);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className={cx("cmd-test", (res !== undefined || err) && "has-result")}>
      <div className="cmd-test-row">
        <span className="cmd-test-label">
          <AudioLines />
          Проверить фразу
        </span>
        <input
          className="cmd-test-input"
          value={text}
          placeholder="Например: «сделай громкость пятьдесят» — покажу, какая команда сработает"
          spellCheck={false}
          aria-label="Проверить фразу"
          onChange={(e) => {
            setText(e.target.value);
            testLater(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") test(text);
            if (e.key === "Escape" && text) {
              e.stopPropagation();
              setText("");
              test("");
            }
          }}
        />
        {busy && <span className="spinner" aria-label="Проверяю" />}
        {text && (
          <button
            type="button"
            className="cmd-clear static"
            aria-label="Очистить"
            onClick={() => {
              setText("");
              test("");
            }}
          >
            <X />
          </button>
        )}
      </div>
      {err && (
        <div className="cmd-test-result err">
          <TriangleAlert />
          <span>{err}</span>
        </div>
      )}
      {res === null && !err && (
        <div className="cmd-test-result none">
          <span className="cmd-test-arrow">→</span>
          <span>Ни одна команда не подошла — фразу получит Jarvis AI</span>
        </div>
      )}
      {res && (
        <div className="cmd-test-result">
          <span className="cmd-test-arrow">→</span>
          <div className="cmd-test-parts">
            {res.parts.map((p, i) => {
              const cmd = items.find((c) => c.id === p.command_id);
              const score = Math.round(p.score);
              const slots = Object.entries(p.slots ?? {}).filter(([, v]) => v !== null && v !== undefined && v !== "");
              return (
                <div key={`${p.command_id}-${i}`} className="cmd-test-part">
                  {i > 0 && <span className="cmd-test-and">и</span>}
                  <AppIcon name={cmd?.icon || "command"} size={24} radius={7} />
                  <button type="button" className="cmd-test-title" onClick={() => onShow(p.command_id)} title="Показать в списке">
                    {p.title || cmd?.name || p.command_id}
                  </button>
                  <span className={cx("cmd-score", score >= 80 ? "hi" : score >= 60 ? "mid" : "lo")}>{score}%</span>
                  {slots.map(([k, v]) => (
                    <span key={k} className="chip cmd-slot">
                      {slotLabel(k).toLowerCase()}: {String(v)}
                    </span>
                  ))}
                  {res.parts.length > 1 && p.text && <span className="faint tiny">«{p.text}»</span>}
                </div>
              );
            })}
          </div>
          <button type="button" className="btn btn-sm btn-soft" onClick={runPhrase} disabled={running}>
            {running ? <span className="spinner" /> : <CirclePlay />}
            Выполнить
          </button>
        </div>
      )}
    </div>
  );
}
