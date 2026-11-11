export function formatNames(values: unknown): string {
  if (!Array.isArray(values)) return "";
  return values
    .map((value) => {
      if (typeof value === "string") return value;
      if (value && typeof value === "object" && "name" in value) {
        const name = (value as { name?: unknown }).name;
        return typeof name === "string" ? name : "";
      }
      return "";
    })
    .filter(Boolean)
    .join(" / ");
}
