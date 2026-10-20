// «Jarvis AI»: провайдер и подключение, характер, возможности, агент, рисование картинок, расходы.
import { Bot, Brain, CircleDollarSign, Plug, Sparkles, Wand } from "lucide-react";
import { Card, cx, Setting, Slider, Tabs, Toggle } from "@/ui";
import { AccountPanel, AiConnection, isAccountProvider, ProviderCards, useAiConfigured } from "../ai";
import { ImagesCard } from "../ImagesCard";
import { LazyText, NumberField, ProgressBar, useApi, useSettingsPatch } from "../kit";

interface Usage {
  month: { prompt_tokens: number; completion_tokens: number; cost: number };
  budget: number;
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(".", ",")} млн`;
  if (n >= 1000) return `${Math.round(n / 1000)} тыс.`;
  return String(n);
}

function UsageCard() {
  const [settings, patch] = useSettingsPatch();
  const usage = useApi<Usage>("/api/ai/usage");
  if (!settings) return null;
  const ai = settings.ai;
  const m = usage.data?.month;
  const cost = m?.cost ?? 0;
  const budget = ai.monthly_budget;
  const part = m && budget > 0 ? cost / budget : 0;
  const month = new Date().toLocaleDateString("ru-RU", { month: "long" });
  return (
    <Card icon={<CircleDollarSign />} title="Расходы" sub="Оценка по ценам провайдера — точные суммы в его кабинете">
      <div className="usage">
        <div className="usage-main">
          <div className="faint tiny">Потрачено за {month}</div>
          <div className="usage-cost">{m ? `$${cost.toFixed(2)}` : usage.error ? "—" : "…"}</div>
          {budget > 0 && m && (
            <>
              <ProgressBar value={Math.min(1, part)} className={cx(part >= 1 ? "over" : part > 0.8 && "warn")} />
              <div className="faint tiny">
                {Math.round(part * 100)}% из ${budget.toFixed(2)}
              </div>
            </>
          )}
        </div>
        <div className="usage-stat">
          <div className="faint tiny">Запросы</div>
          <div className="usage-num">{m ? fmtTokens(m.prompt_tokens ?? 0) : "—"}</div>
          <div className="faint tiny">токенов</div>
        </div>
        <div className="usage-stat">
          <div className="faint tiny">Ответы</div>
          <div className="usage-num">{m ? fmtTokens(m.completion_tokens ?? 0) : "—"}</div>
          <div className="faint tiny">токенов</div>
        </div>
      </div>
      <Setting name="Лимит в месяц" desc="Дойдя до лимита, Jarvis перестаёт обращаться к ИИ до следующего месяца. 0 — без лимита">
        <NumberField value={budget} min={0} step={1} prefix="$" width={120} onCommit={(v) => patch({ ai: { monthly_budget: v } })} />
      </Setting>
    </Card>
  );
}

export default function AiSection() {
  const [settings, patch] = useSettingsPatch();
  const configured = useAiConfigured();
  if (!settings) return null;
  const ai = settings.ai;
  return (
    <div className="stack">
      <Card
        icon={<Sparkles />}
        title="Jarvis AI"
        sub="Отвечает на вопросы, ищет в интернете и выполняет сложные задачи"
        actions={
          <div className="row" style={{ gap: 10 }}>
            <span className={cx("chip", configured ? "ok" : "")}>
              <span className={cx("status-dot", configured && "ok")} />
              {configured ? "Настроено" : "Не настроено"}
            </span>
            <Toggle checked={ai.enabled} onChange={(v) => patch({ ai: { enabled: v } })} label="Jarvis AI" />
          </div>
        }
      >
        <ProviderCards />
      </Card>

      {isAccountProvider(ai.provider) && (
        <Card className={cx("gpt-card", ai.provider !== "chatgpt" && `cli-card cli-card-${ai.provider}`)}>
          <AccountPanel />
        </Card>
      )}

      <Card
        icon={<Plug />}
        title="Подключение"
        sub={isAccountProvider(ai.provider) ? "Модель и проверка связи" : "Ключ хранится зашифрованным только на этом компьютере"}
      >
        <AiConnection />
        <Setting
          name="Прокси"
          desc="Для ИИ и Telegram-бота: http://… или socks5://… — адрес и порт есть в настройках VPN-программы. Claude Code понимает только http://. Пусто — через системный прокси (его включает VPN) или напрямую"
          wide
        >
          <LazyText
            value={ai.proxy}
            placeholder="http://127.0.0.1:10809"
            width="100%"
            onCommit={(v) => patch({ ai: { proxy: v } })}
          />
        </Setting>
      </Card>

      <Card icon={<Wand />} title="Характер" sub="Как Jarvis разговаривает с вами">
        <div className="persona-grid">
          {(
            [
              { id: "jarvis", title: "Джарвис", sub: "Учтивый дворецкий с лёгкой иронией, как в «Железном человеке»" },
              { id: "plain", title: "Нейтральная", sub: "Просто полезный помощник, коротко и по делу" },
            ] as const
          ).map((p) => (
            <button
              key={p.id}
              type="button"
              role="radio"
              aria-checked={ai.persona === p.id}
              className={cx("radio-card persona-card", ai.persona === p.id && "selected")}
              onClick={() => patch({ ai: { persona: p.id } })}
            >
              <span className="grow">
                <div className="title">{p.title}</div>
                <div className="sub">{p.sub}</div>
              </span>
              <span className="radio-dot" />
            </button>
          ))}
        </div>
        <Setting name="Креативность" desc="Ниже — точнее и предсказуемее, выше — живее и разнообразнее" wide>
          <Slider
            value={ai.temperature}
            min={0}
            max={2}
            step={0.1}
            format={(v) => v.toFixed(1)}
            onCommit={(v) => patch({ ai: { temperature: Math.round(v * 10) / 10 } })}
          />
        </Setting>
        <div className="setting setting-block">
          <div className="label">
            <div className="name">Свои инструкции</div>
            <div className="desc">Что Jarvis должен учитывать всегда: «отвечай коротко», «я программист на Python»…</div>
          </div>
          <LazyText
            multiline
            rows={3}
            value={ai.custom_instructions}
            placeholder="Например: обращайся ко мне на «ты», отвечай коротко"
            onCommit={(v) => patch({ ai: { custom_instructions: v } })}
          />
        </div>
      </Card>

      <Card icon={<Brain />} title="Возможности" sub="Что Jarvis AI умеет делать сам">
        <Setting name="Режим по умолчанию" desc="Чат — только разговор, агент — сам выполняет задачи на ПК">
          <Tabs
            value={ai.mode}
            items={[
              { value: "chat", label: "Чат" },
              { value: "all", label: "Всё вместе" },
              { value: "agent", label: "Агент" },
            ]}
            onChange={(v) => patch({ ai: { mode: v } })}
          />
        </Setting>
        <Setting
          name="Скорость ответа"
          desc="«Быстро» — модель думает меньше и отвечает сразу; «Тщательно» — дольше, но обдуманнее"
        >
          <Tabs
            value={ai.speed ?? "fast"}
            items={[
              { value: "fast", label: "Быстро" },
              { value: "balanced", label: "Обычно" },
              { value: "deep", label: "Тщательно" },
            ]}
            onChange={(v) => patch({ ai: { speed: v } })}
          />
        </Setting>
        <Setting
          name="Сразу запускать агента"
          desc="В режиме «Всё вместе» дела на компьютере («напиши Ане…», «поставь будильник…», «что на экране?») сразу берёт агент. Чат — только на лёгком: окна, громкость, открыть программу, вопросы, заметки"
        >
          <Toggle
            checked={ai.auto_agent ?? true}
            onChange={(v) => patch({ ai: { auto_agent: v } })}
            label="Сразу запускать агента"
          />
        </Setting>
        <Setting name="Поиск в интернете" desc="Свежие новости, погода, курсы — с источниками">
          <Toggle checked={ai.web_search} onChange={(v) => patch({ ai: { web_search: v } })} label="Поиск в интернете" />
        </Setting>
        <Setting name="Память" desc="Запоминать факты о вас: «Аня не ест мясо», «я живу в Казани»">
          <Toggle checked={ai.memory} onChange={(v) => patch({ ai: { memory: v } })} label="Память" />
        </Setting>
        <Setting
          name="Зрение"
          desc="Картинки в чате и снимки окон, когда агент работает в программах. «Авто» — по выбранной модели"
        >
          <Tabs
            value={ai.vision}
            items={[
              { value: "auto", label: "Авто" },
              { value: "on", label: "Вкл" },
              { value: "off", label: "Выкл" },
            ]}
            onChange={(v) => patch({ ai: { vision: v } })}
          />
        </Setting>
      </Card>

      <Card icon={<Bot />} title="Агент" sub="Jarvis сам выполняет задачи на компьютере по шагам">
        <Setting name="Спрашивать разрешение" desc="Перед действиями, которые что-то меняют или удаляют">
          <Tabs
            value={ai.agent_confirm}
            items={[
              { value: "risky", label: "Только рискованные" },
              { value: "always", label: "Всегда" },
            ]}
            onChange={(v) => patch({ ai: { agent_confirm: v } })}
          />
        </Setting>
        <Setting name="Шагов на задачу" desc="Больше — сложнее задачи, но дольше и дороже" wide>
          <Slider
            value={ai.max_agent_steps}
            min={3}
            max={100}
            format={(v) => String(Math.round(v))}
            onCommit={(v) => patch({ ai: { max_agent_steps: Math.round(v) } })}
          />
        </Setting>
      </Card>

      <ImagesCard />

      <UsageCard />
    </div>
  );
}
