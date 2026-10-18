import type { ReactNode } from "react";

/** Заголовок страницы: «НАСТРОЙКИ», слева — дополнительный блок (вкладки), справа — действия. */
export function PageHeader({ title, children, actions }: { title?: ReactNode; children?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-head">
      {title && <h1 className="h-page">{title}</h1>}
      {children}
      <span className="spacer" />
      {actions}
    </header>
  );
}
