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

const storedTheme = window.localStorage.getItem("solo-auto-theme");
const storedDensity = window.localStorage.getItem("solo-auto-density");

export const useUIStore = create<UIState>((set) => ({
  theme: storedTheme === "dark" ? "dark" : "light",
  density: storedDensity === "dense" ? "dense" : "default",
  commandOpen: false,
  helpOpen: false,
  setTheme: (theme) => {
    window.localStorage.setItem("solo-auto-theme", theme);
    set({ theme });
  },
  setDensity: (density) => {
    window.localStorage.setItem("solo-auto-density", density);
    set({ density });
  },
  setCommandOpen: (commandOpen) => set({ commandOpen }),
  setHelpOpen: (helpOpen) => set({ helpOpen }),
}));
