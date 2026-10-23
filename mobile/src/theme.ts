/** One palette, shared with the web app so the two do not drift apart. */
import { useColorScheme } from "react-native";

const light = {
  brand: "#4c3ae0", brandSoft: "rgba(76,58,224,.10)", brandLine: "rgba(76,58,224,.26)",
  ink: "#151a23", ink2: "#565273", ink3: "#8a87a3",
  bgNav: "#e9e7f4", bgDoc: "#fbfbff", bgChat: "#f1eff9",
  surface: "#ffffff", surface2: "#f7f7fb", line: "#e5e4ef", seam: "#dcdbe8",
  ok: "#2e7d5b", err: "#b33a30", highlight: "rgba(229,185,60,.30)",
};

const dark: typeof light = {
  brand: "#8f7cff", brandSoft: "rgba(143,124,255,.16)", brandLine: "rgba(143,124,255,.34)",
  ink: "#e6eaf2", ink2: "#a6a3c0", ink3: "#7d7a9a",
  bgNav: "#0d0d16", bgDoc: "#171726", bgChat: "#12121f",
  surface: "#171725", surface2: "#1d1d2d", line: "#292839", seam: "#2c2b40",
  ok: "#5cb98c", err: "#e8796d", highlight: "rgba(229,185,60,.24)",
};

/** Platform identity carries real colour, as it does on the web. */
export const platformColour: Record<string, string> = {
  YouTube: "#e0413a", Instagram: "#c93aa0", Facebook: "#1877f2",
  TikTok: "#00b5ad", X: "#4a4a57", Vimeo: "#17b3e8",
};
export const colourFor = (p: string) => platformColour[p] ?? "#6d6a86";

export type Theme = typeof light;
export const useTheme = (): Theme => (useColorScheme() === "dark" ? dark : light);
