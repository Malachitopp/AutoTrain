/**
 * Turning what the API sends into what a person reads.
 *
 * The API speaks integer pence, ISO dates ("2026-09-04") and ISO instants in
 * UTC ("2026-09-04T07:14:00Z"). People read pounds, "Thu 4 Sep" and "08:14".
 * Times are shown in Europe/London: the backend's rule is UTC in the
 * database and London only at render time, and this is render time.
 */

const LONDON = "Europe/London";

/** 640 → "£6.40". Integer arithmetic only: no floating point near money. */
export function pounds(pence: number): string {
  const sign = pence < 0 ? "-" : "";
  const abs = Math.abs(pence);
  return `${sign}£${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

const dateFormat = new Intl.DateTimeFormat("en-GB", {
  weekday: "short",
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});

/** "2026-09-04" → "Thu 4 Sep". A timetable day has no zone, so it is read
 * as UTC midnight purely to name the weekday. */
export function shortDate(isoDate: string): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  return dateFormat.format(new Date(Date.UTC(year, month - 1, day)));
}

const timeFormat = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: LONDON,
});

/** "2026-09-04T07:14:00Z" → "08:14" (London was on BST that day). */
export function clockTime(isoInstant: string): string {
  return timeFormat.format(new Date(isoInstant));
}
