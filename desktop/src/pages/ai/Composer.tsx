// Строка ввода: [+] (картинка / нарисовать / новый чат / режим агента), растущее поле, модель, режим, микрофон
// (диктовка через /api/voice/capture с живым текстом voice.partial) и «Отправить» / «■ Стоп». Пока в этом чате
// работает агент (toAgent), текст уходит ему — поправкой или ответом на его вопрос; без текста — «■ Стоп».
import {
  Bot,
  ImagePlus,
  MessageSquare,
  Mic,
  Palette,
  Plus,
  SendHorizontal,
  Settings2,
  Sparkles,
  Square,
  SquarePen,
  X,
} from "lucide-react";
import { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { cx, notify, notifyError } from "@/ui";
import { Menu, type MenuItem } from "./Menu";
import { useAi } from "./store";
import { MODES, MODE_LABEL, type Mode } from "./types";
import { prettyModel } from "./util";

// как в ядре: до 4 картинок, data URL до 8 млн знаков (≈ 6 МБ файла), только png / jpeg / webp / gif
const MAX_IMAGES = 4;
const MAX_IMAGE_BYTES = 5.9 * 1024 * 1024;
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const drafts = new Map<string, string>();

export interface ComposerHandle {
  focus: () => void;
}

export const Composer = forwardRef<
  ComposerHandle,
  {
    chatKey: string;
    mode: Mode;
    model: string;
    busy: boolean;
    disabled?: boolean;
    toAgent?: boolean;
    onSend: (text: string, images: string[]) => void;
    onStop: () => void;
    onMode: (mode: Mode) => void;
    onModel: (model: string) => void;
  }
>(function Composer({ chatKey, mode, model, busy, disabled, toAgent, onSend, onStop, onMode, onModel }, ref) {
  // «Авто» — картинки можно прикреплять; модель без зрения получит вместо них пометку (ядро решает по модели)
  const vision = useApp((s) => (s.settings?.ai.vision ?? "auto") !== "off");
  const defaultMode = useApp((s) => s.settings?.ai.mode ?? "all");
  const partial = useApp((s) => s.partial);
  const models = useAi((s) => s.models);
  const modelsLoading = useAi((s) => s.modelsLoading);
  const modelsError = useAi((s) => s.modelsError);
  const loadModels = useAi((s) => s.loadModels);
  const newChat = useAi((s) => s.newChat);

  const [text, setText] = useState(() => drafts.get(chatKey) ?? "");
  const [images, setImages] = useState<string[]>([]);
  const [menu, setMenu] = useState<"plus" | "model" | "mode" | null>(null);
  const [query, setQuery] = useState("");
  const [capturing, setCapturing] = useState(false);
  const captureStart = useRef(0);
  const area = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const plusBtn = useRef<HTMLButtonElement>(null);
  const modelBtn = useRef<HTMLButtonElement>(null);
  const modeBtn = useRef<HTMLButtonElement>(null);

  useImperativeHandle(ref, () => ({ focus: () => area.current?.focus() }), []);

  // черновик у каждого чата свой
  const keyRef = useRef(chatKey);
  useEffect(() => {
    keyRef.current = chatKey;
    setText(drafts.get(chatKey) ?? "");
    setImages([]);
    area.current?.focus();
  }, [chatKey]);
  useEffect(() => {
    if (text) drafts.set(keyRef.current, text);
    else drafts.delete(keyRef.current);
  }, [text]);

  // поле растёт вместе с текстом (до ~7 строк); вставили начало фразы из меню — курсор в конец
  const caretToEnd = useRef(false);
  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
    if (caretToEnd.current) {
      caretToEnd.current = false;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    }
  }, [text]);

  const live = capturing && partial && partial.at >= captureStart.current ? partial.text : "";

  const addFiles = (files: Iterable<File>) => {
    for (const f of files) {
      if (!IMAGE_TYPES.includes(f.type)) {
        if (f.type.startsWith("image/")) notify(`«${f.name}»: подойдут PNG, JPEG, WebP или GIF`, "error");
        continue;
      }
      if (f.size > MAX_IMAGE_BYTES) {
        notify(`«${f.name}» больше 6 МБ`, "error");
        continue;
      }
      const reader = new FileReader();
      reader.onload = () => setImages((list) => (list.length >= MAX_IMAGES ? list : [...list, String(reader.result)]));
      reader.readAsDataURL(f);
    }
  };

  const submit = () => {
    if (disabled || (busy && !toAgent)) return;
    const value = text.trim();
    if (toAgent) {
      // агенту — только текст; картинки остаются для следующего запроса
      if (!value) return;
      onSend(value, []);
      setText("");
      return;
    }
    if (!value && !images.length) return;
    onSend(value || "Что на изображении?", images);
    setText("");
    setImages([]);
  };

  const capture = async () => {
    if (capturing) {
      api.post("/api/voice/stop").catch(() => undefined);
      return;
    }
    captureStart.current = Date.now();
    setCapturing(true);
    try {
      const res = await api.post<{ text: string }>("/api/voice/capture", { timeout: 15 });
      const heard = (res?.text ?? "").trim();
      if (heard) setText((t) => (t.trim() ? `${t.trimEnd()} ${heard}` : heard));
      else notify("Ничего не расслышал — попробуйте ещё раз", "info");
    } catch (e) {
      notifyError(e);
    } finally {
      setCapturing(false);
      area.current?.focus();
    }
  };

  const plusItems: MenuItem[] = [
    ...(vision
      ? [
          {
            key: "image",
            label: "Прикрепить изображение",
            icon: <ImagePlus />,
            disabled: images.length >= MAX_IMAGES,
            onSelect: () => fileInput.current?.click(),
          },
        ]
      : []),
    {
      key: "draw",
      label: "Нарисовать картинку",
      hint: images.length ? "По приложенному фото" : "OpenAI gpt-image-1, ~30 секунд",
      icon: <Palette />,
      onSelect: () => {
        // «сгенерируй …» — чату с инструментом generate_image (а «нарисуй …» в «Всё вместе» берёт агент)
        const lead = images.length ? "Сгенерируй картинку по моему фото: " : "Сгенерируй картинку: ";
        if (text.trim()) {
          area.current?.focus(); // уже что-то написано — не трогаем
          return;
        }
        caretToEnd.current = true;
        setText(lead);
      },
    },
    { key: "new", label: "Новый чат", icon: <SquarePen />, onSelect: newChat },
    {
      key: "agent",
      label: "Режим агента",
      hint: mode === "agent" ? "Включён — нажмите, чтобы выключить" : "Сам выполнит задачу по шагам",
      icon: <Bot />,
      onSelect: () => onMode(mode !== "agent" ? "agent" : defaultMode !== "agent" ? defaultMode : "all"),
    },
  ];

  const modelList = useMemo(() => {
    const list = models ?? [];
    const all = list.includes(model) || !model ? list : [model, ...list];
    const q = query.trim().toLowerCase();
    return q ? all.filter((m) => m.toLowerCase().includes(q) || prettyModel(m).toLowerCase().includes(q)) : all;
  }, [models, model, query]);

  const modelItems: MenuItem[] = modelList.map((m) => ({
    key: m,
    label: prettyModel(m),
    hint: prettyModel(m) !== m ? m : undefined,
    checked: m === model,
    onSelect: () => onModel(m),
  }));

  const placeholder = disabled
    ? "Сначала подключите ИИ"
    : toAgent
      ? "Поправка или ответ агенту…"
      : mode === "agent"
        ? "Опишите задачу для агента…"
        : "Введите ваш запрос…";
  const stopButton = busy && !(toAgent && text.trim());

  return (
    <div className={cx("ai-composer", disabled && "disabled")}>
      {images.length > 0 && (
        <div className="ai-attach">
          {images.map((src, i) => (
            <div key={i} className="ai-attach-item pop-in">
              <img src={src} alt={`Вложение ${i + 1}`} />
              <button
                type="button"
                aria-label="Убрать изображение"
                onClick={() => setImages((list) => list.filter((_, j) => j !== i))}
              >
                <X />
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="ai-input-row">
        <button
          ref={plusBtn}
          type="button"
          className={cx("ai-sq", menu === "plus" && "open")}
          aria-label="Ещё"
          aria-haspopup="menu"
          aria-expanded={menu === "plus"}
          disabled={disabled}
          onClick={() => setMenu(menu === "plus" ? null : "plus")}
        >
          <Plus />
        </button>
        <div className={cx("ai-field", capturing && "capturing", mode === "agent" && "agent")}>
          {mode === "agent" && !capturing && (
            <span className="ai-field-badge" title="Режим агента">
              <Bot />
            </span>
          )}
          <textarea
            ref={area}
            rows={1}
            value={text}
            disabled={disabled}
            placeholder={placeholder}
            aria-label="Сообщение для Jarvis"
            spellCheck
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            onPaste={(e) => {
              if (!vision) return;
              const files = Array.from(e.clipboardData.files ?? []).filter((f) => IMAGE_TYPES.includes(f.type));
              if (files.length) {
                e.preventDefault();
                addFiles(files);
              }
            }}
          />
          {capturing && (
            <div className="ai-live" aria-live="polite">
              <span className="ai-live-wave" aria-hidden>
                <i />
                <i />
                <i />
                <i />
              </span>
              <span className={cx("ellipsis", !live && "faint")}>{live || "Слушаю… говорите"}</span>
            </div>
          )}
        </div>
        <button
          ref={modelBtn}
          type="button"
          className={cx("ai-pill", menu === "model" && "open")}
          aria-haspopup="menu"
          aria-expanded={menu === "model"}
          title={`Модель: ${model}`}
          disabled={disabled}
          onClick={() => {
            loadModels();
            setQuery("");
            setMenu(menu === "model" ? null : "model");
          }}
        >
          <span className="ellipsis">{prettyModel(model)}</span>
        </button>
        <button
          ref={modeBtn}
          type="button"
          className={cx("ai-pill mode", menu === "mode" && "open", mode === "agent" && "agent")}
          aria-haspopup="menu"
          aria-expanded={menu === "mode"}
          title="Режим"
          disabled={disabled}
          onClick={() => setMenu(menu === "mode" ? null : "mode")}
        >
          {mode === "agent" ? <Bot /> : <Sparkles />}
          <span className="ellipsis">{MODE_LABEL[mode]}</span>
        </button>
        <button
          type="button"
          className={cx("ai-sq mic", capturing && "live mic-live")}
          aria-label={capturing ? "Остановить диктовку" : "Сказать голосом"}
          title={capturing ? "Остановить диктовку" : "Сказать голосом"}
          aria-pressed={capturing}
          disabled={disabled}
          onClick={capture}
        >
          {capturing ? <Square /> : <Mic />}
        </button>
        {stopButton ? (
          <button type="button" className="ai-send stop" aria-label="Остановить ответ" title="Остановить" onClick={onStop}>
            <Square />
          </button>
        ) : (
          <button
            type="button"
            className="ai-send"
            aria-label={toAgent ? "Отправить агенту" : "Отправить"}
            title={toAgent ? "Отправить агенту (Enter)" : "Отправить (Enter)"}
            disabled={disabled || (!text.trim() && !images.length)}
            onClick={submit}
          >
            <SendHorizontal />
          </button>
        )}
      </div>
      <input
        ref={fileInput}
        type="file"
        accept={IMAGE_TYPES.join(",")}
        multiple
        hidden
        onChange={(e) => {
          addFiles(Array.from(e.target.files ?? []));
          e.target.value = "";
        }}
      />

      {menu === "plus" && (
        <Menu anchor={plusBtn.current} placement="top-start" items={plusItems} onClose={() => setMenu(null)} width={250} label="Ещё" />
      )}
      {menu === "model" && (
        <Menu
          anchor={modelBtn.current}
          placement="top-end"
          width={270}
          label="Модель"
          items={modelItems}
          onClose={() => setMenu(null)}
          header={
            (models?.length ?? 0) > 7 ? (
              <div className="ai-menu-search">
                <input
                  autoFocus
                  value={query}
                  placeholder="Найти модель"
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "ArrowDown") {
                      e.preventDefault();
                      (e.currentTarget.closest(".ai-menu")?.querySelector('[role^="menuitem"]') as HTMLElement | null)?.focus();
                    }
                  }}
                />
              </div>
            ) : (
              <div className="ai-menu-title">Модель</div>
            )
          }
          empty={
            modelsLoading ? (
              <span className="row" style={{ gap: 8 }}>
                <span className="spinner" /> Загружаю список…
              </span>
            ) : modelsError ? (
              <span>
                {modelsError}
                <button type="button" className="ai-link" onClick={() => loadModels(true)}>
                  Повторить
                </button>
              </span>
            ) : (
              "Ничего не нашлось"
            )
          }
          footer={
            <button
              type="button"
              className="ai-menu-item foot"
              role="menuitem"
              onClick={() => {
                setMenu(null);
                navigate("settings", "ai");
              }}
            >
              <span className="mi-icon">
                <Settings2 />
              </span>
              <span className="mi-text">
                <span className="mi-label">Провайдер и ключ…</span>
              </span>
            </button>
          }
        />
      )}
      {menu === "mode" && (
        <Menu
          anchor={modeBtn.current}
          placement="top-end"
          width={300}
          label="Режим"
          header={<div className="ai-menu-title">Режим</div>}
          items={MODES.map((m) => ({
            key: m.value,
            label: m.label,
            hint: m.hint,
            checked: m.value === mode,
            icon: m.value === "agent" ? <Bot /> : m.value === "chat" ? <MessageSquare /> : <Sparkles />,
            onSelect: () => onMode(m.value),
          }))}
          onClose={() => setMenu(null)}
        />
      )}
    </div>
  );
});
