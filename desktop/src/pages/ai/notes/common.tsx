// Общее для вкладок «Заметки и память»: кнопка удаления с подтверждением, загрузка и ошибка.
import { RotateCcw, Trash2 } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { Button, cx } from "@/ui";

/** Корзина: первый щелчок — «Удалить?», второй (в течение 3 с) — удаляет. */
export function DeleteButton({ onConfirm, label = "Удалить" }: { onConfirm: () => void; label?: string }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button
      type="button"
      className={cx("nt-del", armed && "armed")}
      aria-label={armed ? `${label}: нажмите ещё раз` : label}
      title={armed ? "Нажмите ещё раз, чтобы удалить" : label}
      onClick={(e) => {
        e.stopPropagation();
        if (armed) onConfirm();
        else setArmed(true);
      }}
      onBlur={() => setArmed(false)}
    >
      <Trash2 />
      {armed && <span>Удалить?</span>}
    </button>
  );
}

export function ListState({
  loading,
  error,
  empty,
  onRetry,
  children,
}: {
  loading: boolean;
  error: string;
  empty: ReactNode | null;
  onRetry: () => void;
  children: ReactNode;
}) {
  if (loading)
    return (
      <div className="nt-state">
        <span className="spinner" /> Загружаю…
      </div>
    );
  if (error)
    return (
      <div className="nt-state err">
        <span>Не удалось загрузить: {error}</span>
        <Button size="sm" icon={<RotateCcw />} onClick={onRetry}>
          Повторить
        </Button>
      </div>
    );
  if (empty) return <div className="nt-empty">{empty}</div>;
  return <>{children}</>;
}

export function truthy(v: unknown): boolean {
  return v === true || v === 1 || v === "1";
}
