// «Паки команд»: витрина «Готовые паки команд» со счётчиками, карточки с установкой и панель с командами пака.
import { Check, Download, ListFilter, Package, RotateCw, Search, SearchX, Trash2, TriangleAlert, X } from "lucide-react";
import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import type { Pack } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { navigate } from "@/lib/router";
import { Button, cx, notify, notifyError, Tabs } from "@/ui";
import { appLabel, countText, norm, plural, W_COMMANDS, W_PACKS } from "./model";
import { Drawer } from "./Overlays";
import { SectionHeader } from "./SectionHeader";
import { listMemory, useCommands, usePacks } from "./store";

type Filter = "all" | "installed" | "available";

const WALL_FALLBACK = [
  "spotify",
  "chrome",
  "telegram",
  "discord",
  "obs",
  "steam",
  "photoshop",
  "minecraft",
  "youtube",
  "vscode",
  "figma",
  "blender",
  "twitch",
  "notion",
  "firefox",
  "word",
  "excel",
  "premiere",
  "vk",
  "yandex",
  "zoom",
  "cs2",
  "dota2",
  "netflix",
];

export function PacksPage({ packId }: { packId?: string }) {
  const items = usePacks((s) => s.items);
  const totals = usePacks((s) => s.totals);
  const status = usePacks((s) => s.status);
  const error = usePacks((s) => s.error);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  useEffect(() => {
    const st = usePacks.getState();
    st.load(st.status === "ready");
    if (useCommands.getState().status === "idle") useCommands.getState().load();
  }, []);

  const installedCount = items.filter((p) => p.installed).length;
  const list = useMemo(() => {
    const words = norm(q).split(/\s+/).filter(Boolean);
    return items.filter((p) => {
      if (filter === "installed" && !p.installed) return false;
      if (filter === "available" && p.installed) return false;
      if (!words.length) return true;
      const hay = norm(
        [p.name, p.app, p.description, ...(p.commands ?? []).flatMap((c) => [c.name, ...(c.phrases ?? [])])].join("\n"),
      );
      return words.every((w) => hay.includes(w));
    });
  }, [items, q, filter]);
  const open = packId ? items.find((p) => p.id === packId) : undefined;

  return (
    <div className="page">
      <SectionHeader tab="packs" />
      <div className="page-body packs-body">
        <PacksHero items={items} totals={totals} installed={installedCount} loading={status !== "ready"} />

        <div className="packs-toolbar">
          <div className="input-wrap grow">
            <Search />
            <input
              className="input"
              value={q}
              placeholder="Найти пак: программа, игра или команда"
              spellCheck={false}
              aria-label="Поиск паков"
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && q && (e.stopPropagation(), setQ(""))}
            />
            {q && (
              <button type="button" className="cmd-clear" aria-label="Очистить поиск" onClick={() => setQ("")}>
                <X />
              </button>
            )}
          </div>
          <Tabs<Filter>
            value={filter}
            onChange={setFilter}
            items={[
              { value: "all", label: "Все" },
              { value: "installed", label: `Установленные${installedCount ? ` · ${installedCount}` : ""}` },
              { value: "available", label: "Доступные" },
            ]}
          />
        </div>

        {status === "error" && !items.length ? (
          <div className="cmd-state">
            <TriangleAlert />
            <div className="cmd-state-title">Не удалось загрузить паки</div>
            <div className="faint small">{error}</div>
            <Button size="sm" icon={<RotateCw />} onClick={() => usePacks.getState().load()}>
              Повторить
            </Button>
          </div>
        ) : status !== "ready" && !items.length ? (
          <div className="packs-grid">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="pack-card skeleton" style={{ animationDelay: `${i * 60}ms` }} />
            ))}
          </div>
        ) : !items.length ? (
          <div className="cmd-state">
            <Package />
            <div className="cmd-state-title">Паков пока нет</div>
            <div className="faint small">Готовые наборы команд для программ появятся здесь с обновлением Jarvis.</div>
          </div>
        ) : list.length === 0 ? (
          <div className="cmd-state">
            {q ? <SearchX /> : filter === "installed" ? <Package /> : <ListFilter />}
            <div className="cmd-state-title">
              {q
                ? `Ничего не нашлось по запросу «${q.trim()}»`
                : filter === "installed"
                  ? "Вы ещё не установили ни одного пака"
                  : "Все паки уже установлены"}
            </div>
            {q ? (
              <Button size="sm" onClick={() => setQ("")}>
                Сбросить поиск
              </Button>
            ) : (
              filter !== "all" && (
                <Button size="sm" onClick={() => setFilter("all")}>
                  Показать все паки
                </Button>
              )
            )}
          </div>
        ) : (
          <div className="packs-grid">
            {list.map((p) => (
              <article key={p.id} className={cx("pack-card", p.installed && "installed")}>
                <button
                  type="button"
                  className="pack-main"
                  onClick={() => navigate("commands", "packs", p.id)}
                  aria-label={`${p.name} — подробнее`}
                >
                  <AppIcon name={p.icon || p.app} size={48} radius={14} />
                  <span className="pack-text">
                    <span className="pack-name ellipsis">{p.name}</span>
                    <span className="pack-desc ellipsis">{p.description}</span>
                    <span className="pack-count">{countText(p.commands_count || p.commands?.length || 0, W_COMMANDS)}</span>
                  </span>
                </button>
                <PackButton pack={p} />
              </article>
            ))}
          </div>
        )}
      </div>
      <PackDrawer pack={open} missing={!!packId && status === "ready" && !open} onClose={() => navigate("commands", "packs")} />
    </div>
  );
}

// --- витрина ------------------------------------------------------------------------------------------
function PacksHero({
  items,
  totals,
  installed,
  loading,
}: {
  items: Pack[];
  totals: { packs: number; commands: number };
  installed: number;
  loading: boolean;
}) {
  const icons = useMemo(() => {
    const keys = [...new Set(items.map((p) => p.icon || p.app).filter(Boolean))];
    for (const k of WALL_FALLBACK) if (keys.length < 24 && !keys.includes(k)) keys.push(k);
    return keys.slice(0, 24);
  }, [items]);
  return (
    <section className="packs-hero">
      <div className="packs-wall" aria-hidden>
        <div className="packs-wall-grid">
          {icons.map((k, i) => (
            <span key={`${k}-${i}`} className="wall-tile" style={{ "--i": i } as CSSProperties}>
              <AppIcon name={k} size={50} radius={14} />
            </span>
          ))}
        </div>
      </div>
      <div className="packs-hero-body">
        <div className="eyebrow">Десятки программ</div>
        <h2 className="packs-title">Готовые паки команд</h2>
        <p className="packs-lead">Установите пак — и Jarvis начнёт понимать голосовые команды для этой программы.</p>
        <div className="packs-stats" aria-busy={loading}>
          <div className="pack-stat">
            <b>{loading ? "—" : totals.packs.toLocaleString("ru-RU")}</b>
            <span>{plural(totals.packs, W_PACKS)}</span>
          </div>
          <div className="pack-stat">
            <b>{loading ? "—" : totals.commands.toLocaleString("ru-RU")}</b>
            <span>{plural(totals.commands, W_COMMANDS)}</span>
          </div>
          <div className="pack-stat soft">
            <b>{loading ? "—" : installed}</b>
            <span>установлено</span>
          </div>
        </div>
      </div>
    </section>
  );
}

// --- кнопка установки ----------------------------------------------------------------------------------
function PackButton({ pack, large }: { pack: Pack; large?: boolean }) {
  const busy = usePacks((s) => s.busy[pack.id]);
  const [progress, setProgress] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setInterval>>(undefined);
  useEffect(() => () => clearInterval(timer.current), []);

  const install = async () => {
    setProgress(0);
    clearInterval(timer.current);
    timer.current = setInterval(
      () => setProgress((p) => (p === null ? null : Math.min(92, p + Math.max(1.5, (92 - p) * 0.16)))),
      70,
    );
    try {
      await usePacks.getState().install(pack.id);
      clearInterval(timer.current);
      setProgress(100);
      notify(`Пак «${pack.name}» установлен — ${countText(pack.commands_count, W_COMMANDS)}`, "ok");
      await new Promise((r) => setTimeout(r, 380));
    } catch (e) {
      notifyError(e);
    } finally {
      clearInterval(timer.current);
      setProgress(null);
    }
  };
  const remove = async () => {
    try {
      await usePacks.getState().remove(pack.id);
      notify(`Пак «${pack.name}» удалён`, "info");
    } catch (e) {
      notifyError(e);
    }
  };

  if (progress !== null) {
    return (
      <button
        type="button"
        className={cx("btn pack-btn progress", large && "btn-lg")}
        disabled
        aria-label={`Устанавливаю: ${Math.round(progress)}%`}
        style={{ "--p": `${progress}%` } as CSSProperties}
      >
        <span className="pack-btn-fill" aria-hidden />
        <span className="pack-btn-label">{Math.round(progress)}%</span>
      </button>
    );
  }
  if (pack.installed) {
    return (
      <button
        type="button"
        className={cx("btn pack-btn installed", large && "btn-lg")}
        disabled={busy === "remove"}
        onClick={remove}
        aria-label={`Пак «${pack.name}» установлен. Нажмите, чтобы удалить`}
        title="Нажмите, чтобы удалить пак"
      >
        {busy === "remove" ? (
          <span className="spinner" />
        ) : (
          <>
            <span className="pb-ok">
              <Check />
              Установлено
            </span>
            <span className="pb-del">
              <Trash2 />
              Удалить
            </span>
          </>
        )}
      </button>
    );
  }
  return (
    <button
      type="button"
      className={cx("btn pack-btn", large && "btn-lg")}
      disabled={busy === "install"}
      onClick={install}
      aria-label={`Установить пак «${pack.name}»`}
    >
      <Download />
      Установить
    </button>
  );
}

// --- панель пака --------------------------------------------------------------------------------------
function PackDrawer({ pack, missing, onClose }: { pack?: Pack; missing: boolean; onClose: () => void }) {
  const commands = pack?.commands ?? [];
  return (
    <Drawer open={!!pack || missing} onClose={onClose} label={pack?.name ?? "Пак команд"}>
      {!pack ? (
        <div className="cmd-state">
          <Package />
          <div className="cmd-state-title">Пак не найден</div>
          <Button size="sm" onClick={onClose}>
            Ко всем пакам
          </Button>
        </div>
      ) : (
        <div className="pd">
          <div className="pd-head">
            <AppIcon name={pack.icon || pack.app} size={64} radius={18} />
            <div className="grow">
              <div className="eyebrow">Пак команд</div>
              <h3 className="pd-title">{pack.name}</h3>
              <div className="faint small">
                {appLabel(pack.app) && appLabel(pack.app) !== pack.name ? `${appLabel(pack.app)} · ` : ""}
                {countText(pack.commands_count || commands.length, W_COMMANDS)}
              </div>
            </div>
          </div>
          {pack.description && <p className="pd-desc">{pack.description}</p>}
          <div className="pd-actions">
            <PackButton pack={pack} large />
            {pack.installed && (
              <Button
                size="lg"
                variant="ghost"
                onClick={() => {
                  Object.assign(listMemory, { category: "all", query: pack.name, state: "all", scrollTop: 0 });
                  navigate("commands");
                }}
              >
                Показать в списке
              </Button>
            )}
          </div>
          <div className="pd-label">
            Команды пака <span className="faint">· {commands.length}</span>
          </div>
          {commands.length ? (
            <div className="pd-list">
              {commands.map((c) => (
                <div key={c.id} className="pd-cmd">
                  <div className="pd-cmd-name">{c.name}</div>
                  <div className="pd-cmd-phrases">
                    {(c.phrases ?? []).slice(0, 4).map((p) => (
                      <span key={p} className="cmd-phrase">
                        {p}
                      </span>
                    ))}
                    {(c.phrases?.length ?? 0) > 4 && <span className="cmd-phrase more">+{c.phrases.length - 4}</span>}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="faint small">Список команд появится после установки пака.</div>
          )}
        </div>
      )}
    </Drawer>
  );
}
