// Ядро Jarvis не работает: вместо всплывающих «Нет связи» — понятный экран с причиной из журнала и кнопками
// «Перезапустить ядро» и «Открыть журналы». Причину присылает главный процесс (core:failed), когда ядро
// перестало подниматься; при долгой потере связи экран появляется и без неё.
import { Copy, FolderOpen, RefreshCw, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { bridge, isElectron } from "@/lib/bridge";
import { useApp } from "@/store/app";
import { Button, notify } from "@/ui";

interface CoreFailure {
  code: number | null;
  reason: string;
  tail: string[];
  at: number;
}

const LOST_AFTER_MS = 15_000;

export function CoreDown() {
  const connected = useApp((s) => s.connected);
  const ready = useApp((s) => s.ready);
  const [failure, setFailure] = useState<CoreFailure | null>(null);
  const [lost, setLost] = useState(false);
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    if (!isElectron) return;
    void bridge.coreFailure?.().then((f) => setFailure(f ?? null));
    const offFailed = bridge.on("core:failed", (f) => setFailure((f as CoreFailure | null) ?? null));
    const offReady = bridge.on("core:ready", () => {
      setFailure(null);
      setRestarting(false);
    });
    return () => {
      offFailed();
      offReady();
    };
  }, []);

  useEffect(() => {
    if (connected) {
      setLost(false);
      setRestarting(false);
      return;
    }
    const t = setTimeout(() => setLost(true), LOST_AFTER_MS);
    return () => clearTimeout(t);
  }, [connected]);

  if (!isElectron || (!failure && !(lost && ready))) return null;

  const restart = () => {
    setRestarting(true);
    setFailure(null);
    bridge.send("core:restart");
  };
  const copy = async () => {
    const text = [`Jarvis ${bridge.version}`, failure ? `код выхода: ${failure.code}` : "", ...(failure?.tail ?? [])]
      .filter(Boolean)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      notify("Подробности скопированы", "ok");
    } catch {
      notify("Не удалось скопировать — откройте журналы", "error");
    }
  };

  return (
    <div className="core-down" role="alertdialog" aria-live="assertive">
      <div className="core-down-card">
        <div className="core-down-icon">
          <TriangleAlert />
        </div>
        <h2>{failure ? "Ядро Jarvis не запускается" : "Нет связи с ядром Jarvis"}</h2>
        <p className="muted">
          {failure
            ? "Я перезапускал его несколько раз, но оно снова останавливается. Вот что оно сказало последним:"
            : restarting
              ? "Перезапускаю ядро…"
              : "Ядро не отвечает — пробую переподключиться. Можно перезапустить его вручную."}
        </p>
        {failure?.reason && <pre className="core-down-reason">{failure.reason}</pre>}
        <div className="core-down-actions">
          <Button variant="primary" icon={restarting ? <span className="spinner" /> : <RefreshCw />} onClick={restart} disabled={restarting}>
            Перезапустить ядро
          </Button>
          <Button icon={<FolderOpen />} onClick={() => bridge.send("logs:open")}>
            Открыть журналы
          </Button>
          {failure && (
            <Button variant="ghost" icon={<Copy />} onClick={copy}>
              Скопировать подробности
            </Button>
          )}
        </div>
        <p className="faint small">Если ошибка повторяется, пришлите файл core-process.log из папки журналов.</p>
      </div>
    </div>
  );
}
