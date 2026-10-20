// «Как он это делал»: журнал работы агента — шаги слева, справа выбранный шаг: картинка экрана после него, что
// модель написала перед ним, с чем вызвала действие и что получила в ответ (GET /api/agent/journal/{run_id}).
import { CircleCheck, CircleX, History, Monitor } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { Button, cx, Dots, Modal } from "@/ui";
import { fmtTime } from "./util";

interface JournalStep {
  index: number;
  ts: number;
  title: string;
  name: string;
  args: Record<string, string>;
  ok: boolean;
  result: string;
  thought: string;
  images: string[];
}

interface JournalData {
  run_id: string;
  task: string;
  started: number;
  finished: number | null;
  status: string;
  outcome?: string;
  summary?: string;
  error?: string;
  steps: JournalStep[];
}

const STATUS: Record<string, string> = {
  done: "Готово",
  partial: "Сделано не всё",
  failed: "Не получилось",
  stopped: "Остановлено",
  error: "Ошибка",
  running: "Идёт",
};

function Picture({ runId, name }: { runId: string; name: string }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    setUrl("");
    setFailed(false);
    api
      .get<{ url: string }>(`/api/agent/journal/${runId}/${name}`)
      .then((r) => alive && setUrl(r.url))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [runId, name]);
  if (failed) return <div className="aj-noimg faint tiny">Картинки нет</div>;
  if (!url) return <div className="aj-noimg"><Dots /></div>;
  return (
    <a href={url} target="_blank" rel="noreferrer" title="Открыть крупно">
      <img className="aj-img" src={url} alt="Экран после шага" />
    </a>
  );
}

function StepView({ runId, step }: { runId: string; step: JournalStep }) {
  const [full, setFull] = useState(false);
  const args = Object.entries(step.args ?? {});
  const result = step.result ?? "";
  return (
    <div className="aj-step">
      {step.images.length > 0 ? (
        step.images.map((name, i) => (
          <div key={name} className="aj-pic">
            {step.images.length > 1 && (
              <div className="faint tiny">
                <Monitor /> {i === 0 ? "Монитор с окном впереди" : `Другой монитор ${i}`}
              </div>
            )}
            <Picture runId={runId} name={name} />
          </div>
        ))
      ) : (
        <div className="aj-noimg faint tiny">Без картинки: модель без зрения или экран не снять</div>
      )}
      {step.thought && (
        <div className="aj-block">
          <div className="aj-label">Модель перед шагом</div>
          <div className="aj-thought">{step.thought}</div>
        </div>
      )}
      {(step.name || args.length > 0) && (
        <div className="aj-block">
          <div className="aj-label">Действие</div>
          <code className="aj-code">
            {step.name}
            {args.length > 0 && ` ${args.map(([k, v]) => `${k}: «${v}»`).join(" · ")}`}
          </code>
        </div>
      )}
      {result && (
        <div className="aj-block">
          <div className="aj-label">Ответ</div>
          <pre className={cx("aj-result", !full && "short")}>{full ? result : result.slice(0, 700)}</pre>
          {result.length > 700 && (
            <Button size="sm" variant="ghost" onClick={() => setFull(!full)}>
              {full ? "Свернуть" : "Показать целиком"}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

export function AgentJournal({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [data, setData] = useState<JournalData | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(0);
  useEffect(() => {
    api
      .get<JournalData>(`/api/agent/journal/${runId}`)
      .then((d) => {
        setData(d);
        setSelected(Math.max(0, d.steps.length - 1));
      })
      .catch((e) => setError((e as Error).message));
  }, [runId]);
  const step = data?.steps[selected];
  return (
    <Modal open title="Как он это делал" icon={<History />} onClose={onClose} width={1100}>
      {error && <div className="faint">{error}</div>}
      {!data && !error && <Dots />}
      {data && (
        <div className="aj">
          <div className="aj-head">
            <div className="aj-task">{data.task}</div>
            <div className="faint tiny">
              {fmtTime(data.started)} · {STATUS[data.outcome || data.status] ?? data.status} · шагов:{" "}
              {data.steps.length}
            </div>
          </div>
          <div className="aj-main">
            <ol className="aj-list">
              {data.steps.map((s, i) => (
                <li key={s.index}>
                  <button type="button" className={cx("aj-item", i === selected && "on")} onClick={() => setSelected(i)}>
                    {s.ok ? <CircleCheck className="aj-ok" /> : <CircleX className="aj-err" />}
                    <span className="aj-n">{s.index}</span>
                    <span className="aj-title">{s.title}</span>
                  </button>
                </li>
              ))}
            </ol>
            {step && <StepView runId={data.run_id} step={step} />}
          </div>
        </div>
      )}
    </Modal>
  );
}
