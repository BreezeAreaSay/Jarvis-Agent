// «НАСТРОЙКИ»: слева — разделы, справа — карточки раздела (#/settings/<раздел>).
import "@/styles/settings.css";
import { AudioLines, Mic, Monitor, Palette, ShieldCheck, Smartphone, Sparkles, SquareUser, Wrench } from "lucide-react";
import type { ReactNode } from "react";
import { PageHeader } from "@/components/PageHeader";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { cx } from "@/ui";
import AiSection from "./sections/AiSection";
import AiToolsSection from "./sections/AiToolsSection";
import AvatarSection from "./sections/AvatarSection";
import InterfaceSection from "./sections/InterfaceSection";
import PrivacySection from "./sections/PrivacySection";
import RemoteSection from "./sections/RemoteSection";
import SystemSection from "./sections/SystemSection";
import TtsSection from "./sections/TtsSection";
import VoiceSection from "./sections/VoiceSection";

interface Section {
  id: string;
  title: string;
  hint: string;
  icon: ReactNode;
  view: ReactNode;
}

const SECTIONS: Section[] = [
  { id: "interface", title: "Интерфейс", hint: "Цвета и окно", icon: <Palette />, view: <InterfaceSection /> },
  { id: "voice", title: "Голос и распознавание", hint: "Микрофон и активатор", icon: <Mic />, view: <VoiceSection /> },
  { id: "tts", title: "Синтез речи", hint: "Голос Jarvis", icon: <AudioLines />, view: <TtsSection /> },
  { id: "ai", title: "Jarvis AI", hint: "Провайдер и модель", icon: <Sparkles />, view: <AiSection /> },
  { id: "tools", title: "Инструменты и разрешения", hint: "Что ИИ может делать сам", icon: <Wrench />, view: <AiToolsSection /> },
  { id: "avatar", title: "Аватар и питомец", hint: "Сфера на рабочем столе", icon: <SquareUser />, view: <AvatarSection /> },
  { id: "remote", title: "Пульт в телефоне", hint: "Wi-Fi и Telegram", icon: <Smartphone />, view: <RemoteSection /> },
  { id: "privacy", title: "Приватность и данные", hint: "Что хранится и куда уходит", icon: <ShieldCheck />, view: <PrivacySection /> },
  { id: "system", title: "Система", hint: "Запуск, клавиши, данные", icon: <Monitor />, view: <SystemSection /> },
];

export default function SettingsPage({ route }: { route: string[] }) {
  const ready = useApp((s) => !!s.settings);
  const current = SECTIONS.find((s) => s.id === route[0]) ?? SECTIONS[0];
  return (
    <div className="page set-page set-scope">
      <PageHeader title="Настройки" />
      <div className="page-body fill set-wrap">
        <div className="set-layout">
          <nav className="set-nav" aria-label="Разделы настроек">
            {SECTIONS.map((s) => (
              <button
                key={s.id}
                type="button"
                className={cx("set-nav-item", s.id === current.id && "active")}
                aria-current={s.id === current.id ? "page" : undefined}
                onClick={() => navigate("settings", s.id)}
                title={s.title}
              >
                <span className="ico">{s.icon}</span>
                <span className="lbl">
                  <span className="t">{s.title}</span>
                  <span className="h">{s.hint}</span>
                </span>
              </button>
            ))}
          </nav>
          {/* key — новый раздел начинается сверху и плавно появляется */}
          <div className="set-content" key={current.id}>
            <div className="set-inner fade-in">
              {ready && current.view}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
