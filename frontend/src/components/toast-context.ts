import { createContext, useContext } from "react";

export type ToastTone = "info" | "warn" | "error" | "ok";
export type ToastAction = {
  label: string;
  onClick: () => void | Promise<void>;
};
export type ToastContextValue = {
  toast: (message: string, tone?: ToastTone, action?: ToastAction) => void;
};

export const ToastContext = createContext<ToastContextValue | null>(null);

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext);
  if (!value) throw new Error("useToast 必须在 ToastProvider 内使用");
  return value;
}
