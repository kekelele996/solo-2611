import { create } from "zustand";

type Theme = "light" | "dark";
type Density = "default" | "dense";

type UIState = {
  theme: Theme;
  density: Density;
  commandOpen: boolean;
  helpOpen: boolean;
  setTheme: (theme: Theme) => void;
  setDensity: (density: Density) => void;
  setCommandOpen: (open: boolean) => void;
  setHelpOpen: (open: boolean) => void;
};

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // 存不下就只在本次会话内生效
  }
}

// 没有手动选过主题时跟随系统；index.html 里的内联脚本用同一规则抢在首帧前上色
function initialTheme(): Theme {
  const stored = read("solo-auto-theme");
  if (stored === "dark" || stored === "light") return stored;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

const theme = initialTheme();
document.documentElement.dataset.theme = theme;

export const useUIStore = create<UIState>((set) => ({
  theme,
  density: read("solo-auto-density") === "dense" ? "dense" : "default",
  commandOpen: false,
  helpOpen: false,
  setTheme: (next) => {
    write("solo-auto-theme", next);
    document.documentElement.dataset.theme = next;
    set({ theme: next });
  },
  setDensity: (density) => {
    write("solo-auto-density", density);
    set({ density });
  },
  setCommandOpen: (commandOpen) => set({ commandOpen }),
  setHelpOpen: (helpOpen) => set({ helpOpen }),
}));
