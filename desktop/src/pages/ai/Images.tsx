// Картинки в чате: нарисованные Jarvis (карточка с «Скачать» и «Открыть»), анимация, пока картинка рисуется
// (~30 с: живой «холст» из частиц и отсчёт), и просмотр во весь экран (картинки ответа и из Markdown).
import { Download, Expand, ImageOff, Palette, X } from "lucide-react";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import type { GeneratedInfo } from "@/api/types";
import { cx, notify } from "@/ui";

/** Примерно столько рисует gpt-image-1 картинку среднего качества — по этому идёт полоса ожидания. */
const EXPECTED_S = 30;
const VIEW_EVENT = "jarvis:view-image";

/** «нарисуй кота в шляпе» → «jarvis-kot-v-shlyape-2026-10-20.jpg» (имя файла для «Скачать»). */
export function imageFileName(src: string, prompt = ""): string {
  const ext = /^data:image\/(png|webp|gif)/i.exec(src)?.[1]?.toLowerCase() ?? "jpg";
  const map: Record<string, string> = {
    а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z", и: "i", й: "y", к: "k", л: "l",
    м: "m", н: "n", о: "o", п: "p", р: "r", с: "s", т: "t", у: "u", ф: "f", х: "h", ц: "c", ч: "ch", ш: "sh",
    щ: "sch", ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya",
  };
  const slug = [...prompt.toLowerCase()]
    .map((c) => map[c] ?? c)
    .join("")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/, "");
  const day = new Date().toISOString().slice(0, 10);
  return `jarvis-${slug ? `${slug}-` : ""}${day}.${ext}`;
}

/** Сохранить картинку файлом: в приложении Electron спросит, куда сохранить. */
export function downloadImage(src: string, prompt = "") {
  try {
    const a = document.createElement("a");
    a.href = src;
    a.download = imageFileName(src, prompt);
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch {
    notify("Не удалось сохранить картинку", "error");
  }
}

export function viewImage(src: string, prompt = "") {
  window.dispatchEvent(new CustomEvent(VIEW_EVENT, { detail: { src, prompt } }));
}

/** Нарисованные картинки ответа: превью, подпись (что просили), «Скачать» и «Открыть». */
export function GeneratedImages({ images, info }: { images: string[]; info?: GeneratedInfo[] }) {
  return (
    <div className={cx("ai-gen-grid", images.length > 1 && "multi")}>
      {images.map((src, i) => {
        const prompt = info?.[i]?.prompt ?? "";
        return (
          <figure key={i} className="ai-gen pop-in">
            <button
              type="button"
              className="ai-gen-img"
              onClick={() => viewImage(src, prompt)}
              aria-label="Открыть картинку во весь экран"
            >
              <img src={src} alt={prompt || "Нарисованная картинка"} draggable={false} />
              <span className="ai-gen-zoom" aria-hidden>
                <Expand />
              </span>
            </button>
            <figcaption>
              <span className="ai-gen-prompt" title={prompt}>
                <Palette />
                <span className="ellipsis">{prompt || "Картинка Jarvis"}</span>
              </span>
              <button type="button" className="ai-gen-dl" onClick={() => downloadImage(src, prompt)}>
                <Download />
                Скачать
              </button>
            </figcaption>
          </figure>
        );
      })}
    </div>
  );
}

/** «Картинки удалены» — их стёрли по сроку хранения или в «Приватности», текст ответа остался. */
export function ImagesRemoved({ count }: { count: number }) {
  return (
    <div className="ai-gen-removed">
      <ImageOff />
      {count > 1 ? `Картинки удалены (${count})` : "Картинка удалена"}
    </div>
  );
}

/** Пока картинка рисуется: «холст» с бегущими частицами и полосой, «Рисую… 12 с». */
export function ImageGenerating({ title, failed, detail }: { title: string; failed?: boolean; detail?: string }) {
  const [started] = useState(() => Date.now());
  const [now, setNow] = useState(started);
  useEffect(() => {
    if (failed) return;
    const t = window.setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, [failed]);
  const seconds = Math.floor((now - started) / 1000);
  // полоса растёт быстро, потом всё медленнее и не доходит до конца, пока картинки нет
  const progress = 1 - Math.exp(-(now - started) / 1000 / (EXPECTED_S * 0.62));
  const prompt = title.replace(/^Нарисовать:\s*/i, "");
  return (
    <div className={cx("ai-gen-wait", failed && "failed")} role="status" aria-live="polite">
      <div className="ai-gen-canvas" aria-hidden>
        <span className="ai-gen-blob a" />
        <span className="ai-gen-blob b" />
        <span className="ai-gen-blob c" />
        <span className="ai-gen-grain" />
        <span className="ai-gen-scan" />
        <div className="ai-gen-center">
          <Palette />
        </div>
      </div>
      <div className="ai-gen-foot">
        <div className="ai-gen-line">
          <span className="ai-gen-title">{failed ? "Не получилось нарисовать" : "Рисую…"}</span>
          {!failed && <span className="ai-gen-time">{seconds} с</span>}
        </div>
        <div className="ai-gen-prompt-line ellipsis" title={prompt}>
          {failed ? detail || prompt : prompt}
        </div>
        {!failed && (
          <div className="ai-gen-bar">
            <i style={{ width: `${Math.min(96, progress * 100).toFixed(1)}%` }} />
          </div>
        )}
      </div>
    </div>
  );
}

/** Просмотр картинки во весь экран: Esc или щелчок мимо — закрыть. Один на окно (в ленте чата). */
export function ImageViewer() {
  const [shown, setShown] = useState<{ src: string; prompt: string } | null>(null);
  useEffect(() => {
    const open = (e: Event) => setShown((e as CustomEvent<{ src: string; prompt: string }>).detail);
    window.addEventListener(VIEW_EVENT, open);
    return () => window.removeEventListener(VIEW_EVENT, open);
  }, []);
  useEffect(() => {
    if (!shown) return;
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setShown(null);
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [shown]);
  if (!shown) return null;
  return createPortal(
    <div className="ai-viewer" role="dialog" aria-label="Картинка" onMouseDown={(e) => e.target === e.currentTarget && setShown(null)}>
      <img src={shown.src} alt={shown.prompt || "Картинка"} />
      <div className="ai-viewer-bar">
        {shown.prompt && <span className="ellipsis">{shown.prompt}</span>}
        <button type="button" className="ai-gen-dl" onClick={() => downloadImage(shown.src, shown.prompt)}>
          <Download />
          Скачать
        </button>
        <button type="button" className="ai-viewer-close" aria-label="Закрыть" onClick={() => setShown(null)}>
          <X />
        </button>
      </div>
    </div>,
    document.body,
  );
}
