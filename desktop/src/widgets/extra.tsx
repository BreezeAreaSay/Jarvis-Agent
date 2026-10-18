// Дополнительные виджеты (крипта, акции, новости, программы, быстрые команды, таймер и др.).
import type { ComponentType } from "react";
import type { WidgetProps } from "./common";

export const EXTRA_WIDGETS: Record<string, ComponentType<WidgetProps>> = {};
