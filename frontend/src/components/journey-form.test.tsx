import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { JourneyForm } from "@/components/journey-form";
import { RequireSession } from "@/components/require-session";
import * as session from "@/lib/session";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: React.ComponentProps<"a">) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const RIDER = {
  id: "5c1e0b7e-0000-4000-8000-000000000001",
  email: "rider@example.com",
  claim_consent_at: null,
  created_at: "2026-09-04T12:00:00Z",
};

type Recorded = { path: string; method: string; body: unknown };

function fakeApi(createStatus: number, createBody: unknown): Recorded[] {
  const calls: Recorded[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    calls.push({
      path,
      method: init?.method ?? "GET",
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    });
    if (path === "/auth/me") {
      return new Response(JSON.stringify(RIDER), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify(createBody), {
      status: createStatus,
      headers: { "Content-Type": "application/json" },
    });
  });
  return calls;
}

function fill(values: Record<string, string>) {
  for (const [label, value] of Object.entries(values)) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
}

beforeEach(() => {
  window.localStorage.clear();
  session.store("a-live-jwt");
  replace.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("JourneyForm", () => {
  it("sends the journey as the API expects it and returns to the list", async () => {
    const calls = fakeApi(201, { id: "j1" });
    render(
      <RequireSession>
        <JourneyForm />
      </RequireSession>,
    );
    await screen.findByRole("heading", { name: "Add a journey" });
    fill({
      "From (station code)": "eus",
      "To (station code)": "man",
      "Date of travel": "2026-09-04",
      Departs: "08:14",
      Arrives: "10:22",
      "Ticket price (£)": "24.50",
    });
    fireEvent.click(screen.getByRole("button", { name: "Add journey" }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/journeys"));
    const create = calls.find((c) => c.path === "/journeys");
    expect(create?.method).toBe("POST");
    expect(create?.body).toEqual({
      origin_crs: "EUS", // upper-cased for the API's [A-Z]{3} rule
      destination_crs: "MAN",
      travel_date: "2026-09-04",
      scheduled_departure: "2026-09-04T07:14:00Z", // London 08:14 in summer
      scheduled_arrival: "2026-09-04T09:22:00Z",
      price_pence: 2450, // pounds typed, pence sent
      kind: "single",
    });
  });

  it("reads an arrival before the departure as the next day", async () => {
    const calls = fakeApi(201, { id: "j1" });
    render(
      <RequireSession>
        <JourneyForm />
      </RequireSession>,
    );
    await screen.findByRole("heading", { name: "Add a journey" });
    fill({
      "From (station code)": "EUS",
      "To (station code)": "GLC",
      "Date of travel": "2026-01-15",
      Departs: "23:30",
      Arrives: "04:10",
      "Ticket price (£)": "50",
    });
    fireEvent.click(screen.getByRole("button", { name: "Add journey" }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/journeys"));
    const create = calls.find((c) => c.path === "/journeys");
    expect(create?.body).toMatchObject({
      scheduled_departure: "2026-01-15T23:30:00Z",
      scheduled_arrival: "2026-01-16T04:10:00Z",
    });
  });

  it("shows the API's field error and stays on the form", async () => {
    fakeApi(422, {
      detail: [{ loc: ["body", "scheduled_arrival"], msg: "scheduled_arrival must be after scheduled_departure" }],
    });
    render(
      <RequireSession>
        <JourneyForm />
      </RequireSession>,
    );
    await screen.findByRole("heading", { name: "Add a journey" });
    fill({
      "From (station code)": "EUS",
      "To (station code)": "MAN",
      "Date of travel": "2026-09-04",
      Departs: "08:14",
      Arrives: "08:14",
      "Ticket price (£)": "24.50",
    });
    fireEvent.click(screen.getByRole("button", { name: "Add journey" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("scheduled_arrival: scheduled_arrival must be after scheduled_departure");
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Add journey" })).toBeDefined();
  });
});
