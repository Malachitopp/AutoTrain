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

function claim(status: string): Claim {
  return {
    id: "c1",
    journey_id: "j1",
    operator_id: "o1",
    amount_pence: 640,
    status,
    file_by: "2026-10-02",
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

  it("owes nothing once assessed", () => {
    expect(describeJourney(journey("assessed"), undefined, NOW).label).toBe("No refund due");
  });

  it("says so when the train could not be found", () => {
    expect(describeJourney(journey("unmatched"), undefined, NOW)).toMatchObject({
      label: "Couldn't find this train",
      tone: "bad",
    });
  });
});

describe("a journey with a claim", () => {
  it.each([
    ["draft", "Ready to file", true, "2026-10-02"],
    ["ready", "Ready to file", true, "2026-10-02"],
    ["needs_user", "File it with the operator", true, "2026-10-02"],
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
