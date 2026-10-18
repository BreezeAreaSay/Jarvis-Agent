// Ошибка в разделе или виджете не роняет всё окно: вместо пустого экрана — карточка с причиной и кнопками.
// Текст ошибки уходит в console.error — главный процесс пишет его в журнал ui.log (папка журналов).
import { Copy, House, RefreshCw, TriangleAlert } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { bridge } from "@/lib/bridge";
import { navigate } from "@/lib/router";
import { Button, notify } from "@/ui";

interface Props {
  /** Что упало — для заголовка и журнала: «Jarvis AI», «виджет «Погода»». */
  area: string;
  /** Маленькая версия для виджета: одна строка и «Повторить». */
  compact?: boolean;
  /** Ничего не показывать (части оверлея): упавшая часть исчезает, остальные работают; причина — в журнале. */
  silent?: boolean;
  children: ReactNode;
}

interface State {
  error: Error | null;
  stack: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, stack: "" };

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    const stack = (info.componentStack ?? "").trim().split("\n").slice(0, 8).join("\n");
    this.setState({ stack });
    console.error(`[интерфейс] ${this.props.area} упал: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}\n${stack}`);
  }

  private reset = () => this.setState({ error: null, stack: "" });

  private copy = async () => {
    const { error, stack } = this.state;
    const text = [`Jarvis ${bridge.version} — ${this.props.area}`, error?.stack ?? String(error), stack].join("\n");
    try {
      await navigator.clipboard.writeText(text);
      notify("Подробности скопированы", "ok");
    } catch {
      notify("Не удалось скопировать — откройте журналы", "error");
    }
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.silent) return null;
    // не догрузилась часть интерфейса (файл раздела) — помогает только перезагрузка окна
    const chunk = /dynamically imported module|Importing a module script failed|Loading chunk/i.test(error.message);
    if (this.props.compact) {
      return (
        <div className="eb-compact" role="alert">
          <TriangleAlert />
          <span className="grow ellipsis" title={error.message}>
            Не отрисовался: {error.message}
          </span>
          <button className="link-btn" onClick={this.reset}>
            Повторить
          </button>
        </div>
      );
    }
    return (
      <div className="eb-page" role="alert">
        <div className="core-down-card">
          <div className="core-down-icon">
            <TriangleAlert />
          </div>
          <h2>Раздел «{this.props.area}» не открылся</h2>
          <p className="muted">Остальной Jarvis работает. Вот что пошло не так:</p>
          <pre className="core-down-reason">{error.message || String(error)}</pre>
          <div className="core-down-actions">
            <Button variant="primary" icon={<RefreshCw />} onClick={chunk ? () => location.reload() : this.reset}>
              Попробовать снова
            </Button>
            <Button icon={<House />} onClick={() => navigate("home")}>
              На главную
            </Button>
            <Button variant="ghost" icon={<Copy />} onClick={this.copy}>
              Скопировать подробности
            </Button>
          </div>
          <p className="faint small">Если повторяется — пришлите подробности или файл ui.log из папки журналов.</p>
        </div>
      </div>
    );
  }
}
