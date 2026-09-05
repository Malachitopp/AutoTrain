/**
 * London wall-clock time → UTC instant, for the add-journey form.
 *
 * The API refuses a datetime without a zone (a bare "08:14" is ambiguous on
 * the two days a year the clocks change), so the form has to say exactly
 * which instant it means. People type London time; this turns
 * "2026-09-04" + "08:14" into "2026-09-04T07:14:00Z".
 *
 * No library: the browser's Intl already knows Europe/London's offset at
 * any instant, so the conversion is "take the offset away from the reading".
 *
 * Known limit: on the night the clocks go back (late October) the hour
 * 01:00–01:59 happens twice in London. A reading in that hour is taken as
 * the second, GMT occurrence. One hour a year, and only for trains timed in
 * it; noted here rather than solved.
 */

const LONDON = "Europe/London";

export function londonToIso(date: string, time: string): string {
  const [year, month, day] = date.split("-").map(Number);
  const [hour, minute] = time.split(":").map(Number);
  // The wall-clock reading, taken as if it were already UTC.
  const reading = Date.UTC(year, month - 1, day, hour, minute);
  // First guess: subtract the offset London had at that moment. Refine once
  // using the offset at the guess itself, which settles the clock-change
  // days where the two differ.
  let instant = reading - offsetAt(reading);
  instant = reading - offsetAt(instant);
  return new Date(instant).toISOString().replace(".000Z", "Z");
}

/** "2026-09-04" → "2026-09-05". For an arrival after midnight. */
export function nextDay(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10);
}

const offsetFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: LONDON,
  timeZoneName: "longOffset",
});

/** London's offset from UTC, in milliseconds, at the given instant: 0 in
 * winter, one hour in summer. */
function offsetAt(instantMs: number): number {
  const name = offsetFormat
    .formatToParts(new Date(instantMs))
    .find((part) => part.type === "timeZoneName")?.value;
  // "GMT" in winter, "GMT+01:00" in summer.
  const match = /GMT([+-])(\d{2}):(\d{2})/.exec(name ?? "");
  if (match === null) return 0;
  const sign = match[1] === "-" ? -1 : 1;
  return sign * (Number(match[2]) * 60 + Number(match[3])) * 60_000;
}
