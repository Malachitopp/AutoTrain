import { describe, expect, it } from "vitest";

import type { Claim, Journey } from "@/lib/api";
import { describeJourney } from "@/lib/journey-status";

const NOW = new Date("2026-09-05T12:00:00Z");

function journey(status: string, arrival = "2026-09-04T10:22:00Z"): Journey {
  return {
    id: "j1",
    ticket_id: "t1",
    origin_crs: "EUS",
    destination_crs: "MAN",
    travel_date: "2026-09-04",
    scheduled_departure: "2026-09-04T08:14:00Z",
    scheduled_arrival: arrival,
    status,
    created_at: "2026-09-01T00:00:00Z",
  };
}

function claim(status: string, fileBy = "2026-10-02"): Claim {
  return {
    id: "c1",
    journey_id: "j1",
    operator_id: "o1",
    amount_pence: 640,
    status,
    file_by: fileBy,
    submitted_at: null,
    resolved_at: null,
    operator_reference: null,
    created_at: "2026-09-05T00:00:00Z",
  };
}

describe("a journey with no claim", () => {
  it("is upcoming before its arrival time", () => {
    const view = describeJourney(journey("pending", "2026-09-06T10:22:00Z"), undefined, NOW);
    expect(view).toMatchObject({ label: "Upcoming", tone: "muted", amountPence: null, canFile: false });
  });

  it("is being checked after its arrival time", () => {
    expect(describeJourney(journey("matched"), undefined, NOW).label).toBe("Checking");
  });

  it("says only what is known once assessed without a claim", () => {
    // Could be on time, under threshold, a claim not yet opened by the
    // scheduler, or an operator with no scheme: "No claim" fits all four.
    expect(describeJourney(journey("assessed"), undefined, NOW).label).toBe("No claim");
  });

  it("says so when the train could not be found", () => {
    expect(describeJourney(journey("unmatched"), undefined, NOW)).toMatchObject({
      label: "Couldn't find this train",
      tone: "bad",
    });
  });
});

describe("a journey with a claim, before the deadline", () => {
  it.each([
    ["draft", "Ready to file", true, "2026-10-02"],
    ["needs_user", "File it with the operator", true, "2026-10-02"],
    ["ready", "Being filed for you", false, "2026-10-02"],
    ["submitted", "Claim sent", false, null],
    ["approved", "Claim sent", false, null],
    ["paid", "Paid", false, null],
    ["rejected", "Rejected by the operator", false, null],
    ["expired", "Deadline passed", false, null],
  ])("%s → %s", (status, label, canFile, fileBy) => {
    const view = describeJourney(journey("assessed"), claim(status), NOW);
    expect(view.label).toBe(label);
    expect(view.canFile).toBe(canFile);
    expect(view.fileBy).toBe(fileBy);
    expect(view.amountPence).toBe(640);
  });

  it("shows an unknown status rather than hiding it", () => {
    expect(describeJourney(journey("assessed"), claim("something_new"), NOW).label).toBe(
      "something_new",
    );
  });
});

describe("a claim past its deadline", () => {
  // The API refuses a user filing once file_by is before today's UK date,
  // so the button must go, whatever the status says.
  it("a draft can no longer be filed", () => {
    const view = describeJourney(journey("assessed"), claim("draft", "2026-09-04"), NOW);
    expect(view).toMatchObject({ label: "Deadline passed", tone: "bad", canFile: false, fileBy: null });
  });

  it("a needs_user claim asks, because the backend cannot know", () => {
    const view = describeJourney(journey("assessed"), claim("needs_user", "2026-09-04"), NOW);
    expect(view.label).toBe("Deadline passed — did you file it?");
    expect(view.canFile).toBe(false);
  });

  it("uses the London date, not the UTC one, for 'today'", () => {
    // 23:30 UTC on 4 Sep is already 5 Sep in London: a 4 Sep deadline has passed.
    const lateNight = new Date("2026-09-04T23:30:00Z");
    expect(describeJourney(journey("assessed"), claim("draft", "2026-09-04"), lateNight).canFile).toBe(false);
    // A deadline of today itself is still open.
    expect(describeJourney(journey("assessed"), claim("draft", "2026-09-05"), lateNight).canFile).toBe(true);
  });
});
