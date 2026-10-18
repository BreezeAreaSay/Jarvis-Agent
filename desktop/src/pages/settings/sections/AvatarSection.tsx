// «Аватар и питомец»: живая сфера на рабочем столе (предпросмотр) и плагин «Питомец».
import { ArrowRight, Crosshair, PawPrint, Sparkles } from "lucide-react";
import { useState } from "react";
import { api } from "@/api/client";
import type { PluginInfo } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { LogoMark, Orb } from "@/components/Orb";
import { navigate } from "@/lib/router";
import { Button, Card, cx, notify, notifyError, Setting, Slider, Toggle } from "@/ui";
import { useApi, useSettingsPatch } from "../kit";

/** Рабочий стол в миниатюре с аватаром в углу — размер и включение видны сразу. */
function DesktopPreview({ size, on, onTop }: { size: number; on: boolean; onTop: boolean }) {
  const px = Math.round(44 + ((size - 60) / (400 - 60)) * 116);
  return (
    <div className="desk-preview" aria-hidden>
      <div className="dp-icons">
        <AppIcon name="obs" size={26} radius={7} />
        <AppIcon name="photoshop" size={26} radius={7} />
        <AppIcon name="spotify" size={26} radius={7} />
      </div>
      {onTop && on && <div className="dp-window" />}
      <div className={cx("dp-orb", !on && "off")} style={{ width: px, height: px }}>
        <Orb scale={0.42} reactivity={1.2} />
      </div>
      {!on && <div className="dp-off">Аватар выключен</div>}
      <div className="dp-taskbar">
        <span className="dp-win" />
        <LogoMark size={12} />
        <AppIcon name="chrome" size={13} radius={3} />
        <AppIcon name="spotify" size={13} radius={3} />
        <AppIcon name="telegram" size={13} radius={3} />
      </div>
    </div>
  );
}

function PetCard() {
  const plugins = useApi<{ items: PluginInfo[] }>("/api/plugins");
  const pet = plugins.data?.items.find((p) => p.id === "pet");
  const [busy, setBusy] = useState(false);
  const toggle = async (enabled: boolean) => {
    setBusy(true);
    try {
      await api.put(`/api/plugins/pet`, { enabled });
      await plugins.reload();
      notify(enabled ? "Питомец появился на рабочем столе" : "Питомец спрятан", "ok");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card icon={<PawPrint />} title="Питомец" sub="Пиксельный друг на рабочем столе: танцует под музыку и прячет файлы в корзину">
      <div className="pet-row">
        <div className="pet-art" aria-hidden>
          <span className="pet-blob" />
        </div>
        <div className="grow">
          <div className="small">
            Питомец живёт рядом с окнами, реагирует на музыку и ваши команды. Настроить его можно в разделе
            «Расширения».
          </div>
          <div className="row" style={{ marginTop: 12, gap: 10 }}>
            <Button size="sm" icon={<ArrowRight />} onClick={() => navigate("extensions", "plugins", "pet")}>
              Открыть в расширениях
            </Button>
            {pet && (
              <label className="row small" style={{ gap: 8 }}>
                <Toggle checked={pet.enabled} disabled={busy} onChange={toggle} label="Питомец" />
                {pet.enabled ? "Включён" : "Выключен"}
              </label>
            )}
          </div>
        </div>
      </div>
    </Card>
  );
}

export default function AvatarSection() {
  const [settings, patch] = useSettingsPatch();
  const [size, setSize] = useState<number | null>(null);
  if (!settings) return null;
  const a = settings.avatar;
  return (
    <div className="stack">
      <Card icon={<Sparkles />} title="Аватар на рабочем столе" sub="Живая сфера Jarvis: дышит в покое и пульсирует, когда вы говорите">
        <div className="avatar-layout">
          <DesktopPreview size={size ?? a.size} on={a.desktop} onTop={a.on_top} />
          <div className="grow col" style={{ gap: 0 }}>
            <Setting name="Аватар на рабочем столе">
              <Toggle checked={a.desktop} onChange={(v) => patch({ avatar: { desktop: v } })} label="Аватар на рабочем столе" />
            </Setting>
            <Setting name="Размер" wide>
              <Slider
                value={a.size}
                min={60}
                max={400}
                step={10}
                format={(v) => `${Math.round(v)}`}
                onChange={setSize}
                onCommit={(v) => {
                  setSize(null);
                  patch({ avatar: { size: Math.round(v) } });
                }}
              />
            </Setting>
            <Setting name="Поверх всех окон">
              <Toggle checked={a.on_top} onChange={(v) => patch({ avatar: { on_top: v } })} label="Поверх всех окон" />
            </Setting>
            <Setting name="Анимация" desc="Без анимации сфера неподвижна">
              <Toggle checked={a.animated} onChange={(v) => patch({ avatar: { animated: v } })} label="Анимация аватара" />
            </Setting>
            <Setting name="Прятать в полноэкранных" desc="Игры и видео во весь экран">
              <Toggle
                checked={a.hide_fullscreen}
                onChange={(v) => patch({ avatar: { hide_fullscreen: v } })}
                label="Прятать в полноэкранных"
              />
            </Setting>
          </div>
        </div>
        <div className="row faint tiny" style={{ marginTop: 10, gap: 8 }}>
          <span>Перетащите аватар мышью, чтобы поставить его в удобное место.</span>
          {a.position && (
            <button type="button" className="link-btn" onClick={() => patch({ avatar: { position: null } })}>
              <Crosshair /> Вернуть в правый нижний угол
            </button>
          )}
        </div>
      </Card>
      <PetCard />
    </div>
  );
}
