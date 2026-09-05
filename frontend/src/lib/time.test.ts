import { describe, expect, it } from "vitest";

import { londonToIso, nextDay } from "@/lib/time";

describe("londonToIso", () => {
  it("takes an hour off in summer", () => {
    expect(londonToIso("2026-07-01", "08:14")).toBe("2026-07-01T07:14:00Z");
  });

  it("changes nothing in winter", () => {
    expect(londonToIso("2026-01-15", "08:14")).toBe("2026-01-15T08:14:00Z");
  });

  it("handles the day the clocks go forward", () => {
    // 2026-03-29: BST begins at 01:00 GMT. 08:14 that morning is BST.
    expect(londonToIso("2026-03-29", "08:14")).toBe("2026-03-29T07:14:00Z");
    // And 00:30, before the change, is still GMT.
    expect(londonToIso("2026-03-29", "00:30")).toBe("2026-03-29T00:30:00Z");
  });

  it("round-trips the API's own examples", () => {
    // A 23:50 departure in summer is 22:50 UTC.
    expect(londonToIso("2026-08-14", "23:50")).toBe("2026-08-14T22:50:00Z");
  });
});

describe("nextDay", () => {
  it("steps over a month end", () => {
    expect(nextDay("2026-09-30")).toBe("2026-10-01");
  });
});
