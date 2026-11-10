const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

function parts(value: string): Record<string, string> {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return {};
  return Object.fromEntries(
    dateTimeFormatter
      .formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
}

export function formatDateTime(value: string, seconds = false): string {
  if (!value) return "—";
  const values = parts(value);
  if (!values.year) return value;
  const clock = seconds
    ? `${values.hour}:${values.minute}:${values.second}`
    : `${values.hour}:${values.minute}`;
  return `${values.year}-${values.month}-${values.day} ${clock}`;
}

export function formatClock(value: string): string {
  if (!value) return "—";
  const values = parts(value);
  if (!values.hour) return value.slice(11, 19);
  return `${values.hour}:${values.minute}:${values.second}`;
}

export function todayShanghai(): string {
  const values = parts(new Date().toISOString());
  return `${values.year}-${values.month}-${values.day}`;
}
