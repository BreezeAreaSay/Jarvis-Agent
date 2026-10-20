// «Браузер Jarvis»: агент работает с сайтами в своём окне Chrome или Edge (ядро — /api/browser, событие
// browser.state). Свой профиль: человек один раз входит в нужные сайты, его обычный браузер не трогается.
import { Globe, LogIn } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import { Button, Card, cx, notifyError, Setting, Tabs, Toggle } from "@/ui";
import { useApi, useSettingsPatch } from "../kit";

type BrowserStatus = "off" | "closed" | "starting" | "connected" | "error";

interface BrowserState {
  enabled: boolean;
  status: BrowserStatus;
  app: string;
  found: boolean;
  version: string;
  error: string;
  tabs: number;
  profile: string;
}

const STATUS: Record<BrowserStatus, string> = {
  off: "Выключен",
  closed: "Окно не открыто",
  starting: "Открывается…",
  connected: "Подключён",
  error: "Не подключился",
};

export default function BrowserCard() {
  const [settings, patch] = useSettingsPatch();
  const q = useApi<BrowserState>("/api/browser");
  const { data, setData, reload } = q;
  const [busy, setBusy] = useState(false);
  useEffect(() => onEvent<BrowserState>("browser.state", (e) => setData(e.data)), [setData]);
  if (!settings || q.missing) return null;
  const ai = settings.ai;
  const status: BrowserStatus = !ai.browser ? "off" : (data?.status ?? "closed");
  const app = data?.app || "Chrome или Edge";

  const open = async () => {
    setBusy(true);
    try {
      setData(await api.post<BrowserState>("/api/browser/open", {}));
    } catch (e) {
      notifyError(e);
      void reload();
    } finally {
      setBusy(false);
    }
  };

  let hint: string;
  if (data && !data.found && status !== "connected") hint = "Не нашёл Google Chrome или Microsoft Edge — установите один из них";
  else if (status === "off")
    hint = "Включите — и агент будет открывать сайты в отдельном окне со своим профилем. Ваш обычный браузер он не трогает";
  else if (status === "starting") hint = `Открываю ${app}…`;
  else if (status === "error") hint = data?.error || "Не удалось подключиться к браузеру";
  else if (status === "connected")
    hint = `${app}${data?.version ? ` ${data.version}` : ""}: агент видит страницы целиком и действует в нём. Пароли и cookies Jarvis не читает`;
  else
    hint = "Окно откроется само, когда агенту понадобится сайт. Откройте его сейчас и войдите в сайты, где агент будет работать за вас (почта, магазины), — один раз";

  return (
    <Card
      icon={<Globe />}
      title="Браузер Jarvis"
      sub="Агент работает с сайтами в своём окне Chrome или Edge: видит всю страницу и нажимает точнее"
      actions={<Toggle checked={ai.browser} onChange={(v) => patch({ ai: { browser: v } })} label="Браузер Jarvis" />}
    >
      <Setting
        name={
          <span className={cx("chip", status === "connected" && "ok")}>
            <span className={cx("status-dot", status === "connected" && "ok")} />
            {STATUS[status]}
          </span>
        }
        desc={hint}
      >
        <Button variant={status === "connected" ? "default" : "primary"} size="sm" icon={<LogIn />} loading={busy} onClick={open}>
          {status === "connected" ? "Показать окно" : "Открыть браузер Jarvis"}
        </Button>
      </Setting>
      {ai.browser && (
        <Setting name="Какой браузер" desc="«Авто» — Google Chrome, если установлен, иначе Microsoft Edge (есть в любой Windows)">
          <Tabs
            value={ai.browser_app}
            items={[
              { value: "auto", label: "Авто" },
              { value: "chrome", label: "Chrome" },
              { value: "edge", label: "Edge" },
            ]}
            onChange={(v) => patch({ ai: { browser_app: v } })}
          />
        </Setting>
      )}
    </Card>
  );
}
