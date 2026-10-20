// Ответ ассистента в Markdown: marked → DOMPurify. Блоки кода — с подписью языка и кнопкой «Копировать»,
// таблицы — в рамке с прокруткой вбок (узкое окно), картинки открываются во весь экран, ссылки — во внешнем
// браузере (bridge.openExternal).
import DOMPurify from "dompurify";
import { Marked } from "marked";
import { memo, useMemo } from "react";
import { bridge } from "@/lib/bridge";
import { cx, notify } from "@/ui";
import { viewImage } from "./Images";

const escapeHtml = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const marked = new Marked({
  gfm: true,
  breaks: true,
  async: false,
  renderer: {
    code({ text, lang }) {
      const language = (lang ?? "").match(/^\S*/)?.[0] ?? "";
      return (
        `<div class="ai-md-code"><div class="ai-md-code-head"><span>${escapeHtml(language || "код")}</span>` +
        `<button type="button" class="ai-md-copy" aria-label="Скопировать код">Копировать</button></div>` +
        `<pre><code${language ? ` class="language-${escapeHtml(language)}"` : ""}>${escapeHtml(text)}</code></pre></div>`
      );
    },
  },
});

const PURIFY = {
  FORBID_TAGS: ["style", "form", "input", "textarea", "select", "iframe", "object", "embed"],
  FORBID_ATTR: ["style", "onerror", "onload"],
};

export function renderMarkdown(text: string): string {
  // таблица — в обёртке: широкая прокручивается вбок внутри ответа, а не раздвигает ленту
  const html = (marked.parse(text) as string)
    .replace(/<table>/g, '<div class="ai-md-table"><table>')
    .replace(/<\/table>/g, "</table></div>");
  return DOMPurify.sanitize(html, PURIFY) as unknown as string;
}

function onClick(e: React.MouseEvent<HTMLDivElement>) {
  const target = e.target as HTMLElement;
  const copy = target.closest<HTMLButtonElement>(".ai-md-copy");
  if (copy) {
    const code = copy.closest(".ai-md-code")?.querySelector("pre")?.textContent ?? "";
    navigator.clipboard
      .writeText(code)
      .then(() => {
        copy.textContent = "Скопировано";
        copy.classList.add("done");
        setTimeout(() => {
          copy.textContent = "Копировать";
          copy.classList.remove("done");
        }, 1600);
      })
      .catch(() => notify("Не удалось скопировать", "error"));
    return;
  }
  if (target instanceof HTMLImageElement && target.src) {
    viewImage(target.src, target.alt);
    return;
  }
  const link = target.closest<HTMLAnchorElement>("a[href]");
  if (link) {
    e.preventDefault();
    const href = link.getAttribute("href") ?? "";
    if (/^(https?:|mailto:)/i.test(href)) bridge.openExternal(href);
  }
}

export const Markdown = memo(function Markdown({
  text,
  streaming,
  className,
}: {
  text: string;
  streaming?: boolean;
  className?: string;
}) {
  const html = useMemo(() => renderMarkdown(text), [text]);
  return (
    <div
      className={cx("ai-md", streaming && "streaming", className)}
      onClick={onClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
});
