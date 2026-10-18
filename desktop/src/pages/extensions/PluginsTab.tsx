// Вкладка «Плагины»: слева — доска из девяти глянцевых плиток (как «9 новых плагинов» у Luxify),
// справа — карточка выбранного плагина: большая плитка, название, описание, включение, состояние,
// живая панель, кнопки действий, «Скажите: …» и настройки по схеме.
import { Mic, TriangleAlert, Info } from "lucide-react";
import { useEffect, useState } from "react";
import { navigate } from "@/lib/router";
import { Button, cx, notify, notifyError, Toggle } from "@/ui";
import {
  colorVars,
  fallbackPlugins,
  HEALTH_TEXT,
  type Plugin,
  pluginHealth,
  PluginTile,
  plural,
} from "./pluginKit";
import { PluginLive, runPluginAction, StatusFacts } from "./PluginLive";
import { PluginSettings } from "./PluginSettings";
import { bindPlugins, usePlugins } from "./pluginStore";

function PluginBoard({
  list,
  selected,
  onSelect,
  offline,
}: {
  list: Plugin[];
  selected?: string;
  onSelect: (id: string) => void;
  offline: boolean;
}) {
  const enabled = list.filter((p) => p.enabled).length;
  const broken = list.filter((p) => pluginHealth(p) === "error").length;
  return (
    <section className="pl-board">
      <div className="pl-board-glow" aria-hidden>
        <i />
        <i />
        <i />
      </div>
      <div className="pl-board-head">
        <span className="pl-count">{list.length}</span>
        <div>
          <div className="pl-count-label">{plural(list.length, "плагин", "плагина", "плагинов")}</div>
          <div className="pl-count-sub">
            {offline
              ? "Нет связи с ядром"
              : enabled
                ? `Включено ${enabled}${broken ? ` · с ошибкой ${broken}` : ""}`
                : "Пока ни один не включён"}
          </div>
        </div>
      </div>
      <div className="pl-grid" role="listbox" aria-label="Плагины">
        {list.map((p) => {
          const health = pluginHealth(p);
          return (
            <button
              key={p.id}
              role="option"
              aria-selected={p.id === selected}
              className={cx("pl-cell", p.id === selected && "selected")}
              onClick={() => onSelect(p.id)}
              title={`${p.name} — ${HEALTH_TEXT[health].toLowerCase()}`}
            >
              <span className="pl-tile-wrap">
                <PluginTile plugin={p} size={64} />
                {health !== "off" && <span className={cx("pl-badge", health)} />}
              </span>
              <span className="pl-name">{p.name}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function PluginHero({ plugin, offline }: { plugin: Plugin; offline: boolean }) {
  const update = usePlugins((s) => s.update);
  const [busy, setBusy] = useState("");
  const health = pluginHealth(plugin);
  const error = typeof plugin.status?.error === "string" ? plugin.status.error : "";
  const hint = typeof plugin.status?.hint === "string" ? plugin.status.hint : "";
  const examples = (plugin.examples ?? []).filter(Boolean);
  // у плагинов с сеансом (status.active) — только уместная кнопка: «Запустить» или «Остановить»
  const running = typeof plugin.status?.active === "boolean" ? plugin.status.active : null;
  const actions = (plugin.actions ?? []).filter((a) => running === null || (a.id === "start" ? !running : a.id === "stop" ? running : true));

  const setEnabled = async (v: boolean) => {
    try {
      await update(plugin.id, { enabled: v });
      notify(v ? `«${plugin.name}» включён` : `«${plugin.name}» выключен`, v ? "ok" : "info");
    } catch (e) {
      notifyError(e);
    }
  };

  const act = async (id: string, title?: string) => {
    setBusy(id);
    try {
      const res = await runPluginAction(plugin, id);
      const text = typeof res?.message === "string" ? res.message : typeof res?.text === "string" ? res.text : "";
      notify(text || `${title ?? "Действие"} — готово`, "ok");
    } catch {
      /* ошибку уже показали */
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="pl-hero" style={colorVars(plugin)} key={plugin.id}>
      <div className="pl-hero-head">
        <PluginTile plugin={plugin} size={88} className="big" />
        <div className="grow" style={{ minWidth: 0 }}>
          <h2 className="pl-title">{plugin.name}</h2>
          <p className="pl-desc">{plugin.description}</p>
          <div className={cx("pl-state", health)}>
            <span className={cx("status-dot", health === "on" && "ok", health === "error" && "err", health === "starting" && "busy", health === "ready" && "ready")} />
            {offline ? "Нет связи с ядром" : HEALTH_TEXT[health]}
          </div>
        </div>
        <label className="pl-switch">
          <span>{plugin.enabled ? "Включён" : "Выключен"}</span>
          <Toggle checked={plugin.enabled} onChange={setEnabled} disabled={offline} label={`Включить «${plugin.name}»`} />
        </label>
      </div>

      {error && plugin.enabled && (
        <div className="pl-alert err">
          <TriangleAlert />
          <span>{error}</span>
        </div>
      )}
      {!error && hint && plugin.enabled && (
        <div className="pl-alert info">
          <Info />
          <span>{hint}</span>
        </div>
      )}

      <div className={cx("pl-live", !plugin.enabled && "off")}>
        <PluginLive plugin={plugin} onAction={(id) => act(id)} />
      </div>

      <StatusFacts plugin={plugin} />

      {actions.length > 0 && (
        <div className="pl-actions">
          {actions.map((a, i) => (
            <Button
              key={a.id}
              size="sm"
              variant={i === 0 ? "soft" : "default"}
              loading={busy === a.id}
              disabled={!plugin.enabled || offline}
              title={plugin.enabled ? undefined : "Сначала включите плагин"}
              onClick={() => act(a.id, a.title)}
            >
              {a.title}
            </Button>
          ))}
        </div>
      )}

      {examples.length > 0 && (
        <div className="pl-examples">
          <span className="pl-say">
            <Mic /> Скажите:
          </span>
          {examples.slice(0, 4).map((x) => (
            <span key={x} className="pl-phrase">
              «{x}»
            </span>
          ))}
        </div>
      )}

      <div className="pl-section">
        <h3 className="pl-section-title">Настройки</h3>
        <PluginSettings plugin={plugin} readOnly={offline} />
      </div>
    </section>
  );
}

export function PluginsTab({ selectedId }: { selectedId?: string }) {
  const items = usePlugins((s) => s.items);
  const error = usePlugins((s) => s.error);
  const loading = usePlugins((s) => s.loading);
  useEffect(() => bindPlugins(), []);
  const offline = !items;
  const list = items && items.length ? items : fallbackPlugins();
  const selected = list.find((p) => p.id === selectedId) ?? list[0];
  return (
    <div className="ext-layout">
      <div className="ext-side">
        <PluginBoard
          list={list}
          selected={selected?.id}
          onSelect={(id) => navigate("extensions", "plugins", id)}
          offline={offline && !loading}
        />
        {offline && error && !loading && (
          <div className="pl-alert err" style={{ marginTop: 12 }}>
            <TriangleAlert />
            <span>
              Плагины не загрузились: {error}.{" "}
              <button className="wx-link" onClick={() => usePlugins.getState().load()}>
                Повторить
              </button>
            </span>
          </div>
        )}
      </div>
      <div className="ext-main">{selected && <PluginHero plugin={selected} offline={offline} />}</div>
    </div>
  );
}
