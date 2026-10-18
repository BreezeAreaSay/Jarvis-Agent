// «Интерфейс» — внешний вид, цвета и поведение окна (как в Luxify: цвет темы, прозрачность, размытие…).
import { AppWindow, Palette, Plus } from "lucide-react";
import { useEffect, useRef } from "react";
import { ACCENTS, applyTheme } from "@/lib/theme";
import { Card, cx, Select, Setting, Slider, Tabs, Toggle, useDebounced } from "@/ui";
import { useSettingsPatch } from "../kit";

function norm(hex: string) {
  return hex.trim().toLowerCase();
}

function AccentPicker() {
  const [settings, patch] = useSettingsPatch();
  const input = useRef<HTMLInputElement>(null);
  const savePicked = useDebounced((v: string) => patch({ interface: { accent: v.toUpperCase() } }), 350);
  if (!settings) return null;
  const ui = settings.interface;
  const current = norm(ui.accent);
  const custom = !ACCENTS.some((a) => norm(a) === current);
  return (
    <div className="swatches" role="radiogroup" aria-label="Цвет темы">
      {ACCENTS.map((c) => (
        <button
          key={c}
          type="button"
          role="radio"
          aria-checked={norm(c) === current}
          aria-label={c}
          className={cx("swatch", norm(c) === current && "selected")}
          style={{ background: c }}
          onClick={() => patch({ interface: { accent: c } })}
        />
      ))}
      {custom && (
        <button
          type="button"
          role="radio"
          aria-checked
          aria-label={`Свой цвет ${ui.accent}`}
          className="swatch selected"
          style={{ background: ui.accent }}
          onClick={() => input.current?.click()}
        />
      )}
      <button
        type="button"
        className="swatch add"
        aria-label="Свой цвет"
        title="Свой цвет"
        onClick={() => input.current?.click()}
      >
        <Plus size={13} />
      </button>
      <input
        ref={input}
        type="color"
        className="visually-hidden"
        tabIndex={-1}
        value={/^#[0-9a-f]{6}$/i.test(ui.accent) ? ui.accent.toLowerCase() : "#3b82f6"}
        onChange={(e) => {
          // живой предпросмотр, в настройки — когда цвет перестанет меняться
          applyTheme({ ...ui, accent: e.target.value });
          savePicked(e.target.value);
        }}
      />
    </div>
  );
}

/** Ползунок с живым предпросмотром темы (без запросов к ядру, пока тянете). */
function ThemeSlider({ field }: { field: "transparency" | "blur" }) {
  const [settings, patch] = useSettingsPatch();
  const ui = settings?.interface;
  const latest = useRef(ui);
  latest.current = ui;
  // уходя со страницы посреди перетаскивания — вернуть сохранённое
  useEffect(() => () => applyTheme(latest.current), []);
  if (!ui) return null;
  return (
    <Slider
      value={ui[field]}
      onChange={(v) => applyTheme({ ...ui, [field]: Math.round(v) })}
      onCommit={(v) => patch({ interface: { [field]: Math.round(v) } })}
    />
  );
}

export default function InterfaceSection() {
  const [settings, patch] = useSettingsPatch();
  if (!settings) return null;
  const ui = settings.interface;
  return (
    <div className="stack">
      <Card icon={<Palette />} title="Интерфейс" sub="Внешний вид, цвета и поведение окна">
        <Setting name="Цвет темы" wide>
          <AccentPicker />
        </Setting>
        <Setting name="Прозрачность" wide>
          <ThemeSlider field="transparency" />
        </Setting>
        <Setting name="Размытие" wide>
          <ThemeSlider field="blur" />
        </Setting>
        <Setting name="Анимации" desc="Живая сфера, плавные переходы и волны голоса">
          <Toggle checked={ui.animations} onChange={(v) => patch({ interface: { animations: v } })} label="Анимации" />
        </Setting>
        <Setting name="Масштаб интерфейса" desc="Размер текста и элементов окна" wide>
          <Slider
            value={ui.ui_scale}
            min={80}
            max={140}
            step={5}
            onCommit={(v) => patch({ interface: { ui_scale: Math.round(v) } })}
          />
        </Setting>
        <Setting name="Язык интерфейса">
          <Select
            value={ui.language}
            width={160}
            options={[
              { value: "ru", label: "Русский" },
              { value: "en", label: "English", hint: "скоро", disabled: true },
            ]}
            onChange={(v) => patch({ interface: { language: v } })}
          />
        </Setting>
        <Setting name="Сворачивать в трей при закрытии" desc="Крестик прячет окно, Jarvis продолжает слушать">
          <Toggle
            checked={ui.minimize_to_tray}
            onChange={(v) => patch({ interface: { minimize_to_tray: v } })}
            label="Сворачивать в трей при закрытии"
          />
        </Setting>
      </Card>

      <Card icon={<AppWindow />} title="Окно" sub="Как ведёт себя главное окно Jarvis">
        <Setting name="Поверх всех окон" desc="Окно Jarvis не закрывается другими программами">
          <Toggle checked={ui.always_on_top} onChange={(v) => patch({ interface: { always_on_top: v } })} label="Поверх всех окон" />
        </Setting>
        <Setting name="Материал окна" desc="Настоящая прозрачность окна — Windows 11">
          <Tabs
            value={ui.window_material}
            items={[
              { value: "none", label: "Обычный" },
              { value: "acrylic", label: "Акрил" },
              { value: "mica", label: "Слюда" },
            ]}
            onChange={(v) => patch({ interface: { window_material: v } })}
          />
        </Setting>
        <Setting name="Запускать свёрнутым" desc="При старте Windows — сразу в трей, без окна">
          <Toggle
            checked={ui.start_minimized}
            onChange={(v) => patch({ interface: { start_minimized: v } })}
            label="Запускать свёрнутым"
          />
        </Setting>
      </Card>
    </div>
  );
}
