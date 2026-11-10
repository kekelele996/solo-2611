import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";

import { useUIStore } from "../stores/ui";

export type HotkeyHandlers = {
  onSave?: () => void;
  onPrimary?: () => void;
  onNext?: () => void;
  onPrevious?: () => void;
  onCopyEvidence?: () => void;
  onTaskRoot?: () => void;
};

export function useHotkeys(handlers: HotkeyHandlers = {}): void {
  const navigate = useNavigate();
  const setCommandOpen = useUIStore((state) => state.setCommandOpen);
  const setHelpOpen = useUIStore((state) => state.setHelpOpen);
  const prefix = useRef(false);
  const prefixTimer = useRef<number | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const editing =
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.tagName === "SELECT" ||
        Boolean(target?.closest(".cm-editor"));
      const command = event.metaKey || event.ctrlKey;
      if (command && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen(true);
        return;
      }
      if (event.key === "?") {
        if (!editing) {
          event.preventDefault();
          setHelpOpen(true);
        }
        return;
      }
      if (editing) {
        if (command && event.key.toLowerCase() === "s") {
          event.preventDefault();
          handlers.onSave?.();
        }
        if (command && event.key === "Enter") {
          event.preventDefault();
          handlers.onPrimary?.();
        }
        return;
      }
      if (event.key === "g") {
        prefix.current = true;
        if (prefixTimer.current) window.clearTimeout(prefixTimer.current);
        prefixTimer.current = window.setTimeout(() => {
          prefix.current = false;
        }, 300);
        return;
      }
      if (prefix.current) {
        const destinations: Record<string, string> = {
          d: "/",
          t: "/tasks",
          s: "/source",
          e: "/export",
          ",": "/system",
        };
        const destination = destinations[event.key];
        prefix.current = false;
        if (destination) {
          event.preventDefault();
          void navigate(destination);
        }
        return;
      }
      if (event.key === "[") {
        event.preventDefault();
        handlers.onPrevious?.();
      }
      if (event.key === "]") {
        event.preventDefault();
        handlers.onNext?.();
      }
      if (command && event.shiftKey && event.key.toLowerCase() === "c") {
        event.preventDefault();
        handlers.onCopyEvidence?.();
      }
      if (command && event.key.toLowerCase() === "o") {
        event.preventDefault();
        handlers.onTaskRoot?.();
      }
      if (command && event.key === "Enter") {
        event.preventDefault();
        handlers.onPrimary?.();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [handlers, navigate, setCommandOpen, setHelpOpen]);
}
