import { describe, expect, it } from "vitest";

import { clockTime, pounds, shortDate } from "@/lib/format";

describe("pounds", () => {
  it("formats pence as pounds with two decimals", () => {
    expect(pounds(640)).toBe("£6.40");
    expect(pounds(5)).toBe("£0.05");
    expect(pounds(0)).toBe("£0.00");
    expect(pounds(123456)).toBe("£1234.56");
  });

  it("keeps a minus sign in front", () => {
    expect(pounds(-250)).toBe("-£2.50");
  });
});

describe("shortDate", () => {
  it("names the weekday of a timetable day", () => {
    expect(shortDate("2026-09-04")).toBe("Fri 4 Sept");
  });
});

describe("clockTime", () => {
  it("shows a UTC instant as London time, summer and winter", () => {
    expect(clockTime("2026-09-04T07:14:00Z")).toBe("08:14"); // BST
    expect(clockTime("2026-01-15T08:14:00Z")).toBe("08:14"); // GMT
  });
});
