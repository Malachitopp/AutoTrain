/**
 * The journeys page against a fake API that answers by path. Sessions come
 * from the gate, so every test stores a token and lets /auth/me succeed.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Dashboard } from "@/components/dashboard";
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

const JOURNEY = {
  id: "j1",
  ticket_id: "t1",
  origin_crs: "EUS",
  destination_crs: "MAN",
  travel_date: "2026-09-04",
  scheduled_departure: "2026-09-04T07:14:00Z",
  scheduled_arrival: "2026-09-04T09:22:00Z",
  status: "assessed",
  created_at: "2026-09-01T00:00:00Z",
};

const CLAIM = {
  id: "c1",
  journey_id: "j1",
  operator_id: "o1",
  amount_pence: 640,
  status: "draft",
  file_by: "2026-10-02",
  submitted_at: null,
  resolved_at: null,
  operator_reference: null,
  created_at: "2026-09-05T00:00:00Z",
};

type Route = { status: number; body: unknown };

/** A fake API: answers by path, records every call. */
function fakeApi(routes: Record<string, Route>): { calls: string[] } {
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    calls.push(`${init?.method ?? "GET"} ${path}`);
    const route = routes[path];
    if (route === undefined) return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    return new Response(JSON.stringify(route.body), {
      status: route.status,
      headers: { "Content-Type": "application/json" },
    });
  });
  return { calls };
}

function page(summary: unknown, journeys: unknown[], claims: unknown[], extra: Record<string, Route> = {}) {
  return fakeApi({
    "/auth/me": { status: 200, body: RIDER },
    "/claims/summary": { status: 200, body: summary },
    "/journeys": { status: 200, body: { items: journeys, count: journeys.length, limit: 50 } },
    "/claims": { status: 200, body: { items: claims, count: claims.length, limit: 50 } },
    ...extra,
  });
}

function renderPage() {
  return render(
    <RequireSession>
      <Dashboard />
    </RequireSession>,
  );
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

describe("Dashboard", () => {
  it("shows the money and each journey with its status", async () => {
    page({ recovered_pence: 3675, pending_pence: 640 }, [JOURNEY], [CLAIM]);
    renderPage();
    await screen.findByText("£36.75");
    expect(screen.getAllByText("£6.40")).toHaveLength(2); // pending total, and the claim's amount
    expect(screen.getByText("EUS → MAN")).toBeDefined();
    expect(screen.getByText("08:14 → 10:22")).toBeDefined(); // London time
    expect(screen.getByText("Ready to file")).toBeDefined();
    expect(screen.getByText("by Fri 2 Oct")).toBeDefined();
  });

  it("offers the first journey when there are none", async () => {
    page({ recovered_pence: 0, pending_pence: 0 }, [], []);
    renderPage();
    await screen.findByText("No journeys yet");
    const add = screen.getByRole("link", { name: "Add your first journey" });
    expect(add.getAttribute("href")).toBe("/journeys/new");
  });

  it("files a claim: asks the API for the operator's page and shows the link", async () => {
    const api = page({ recovered_pence: 0, pending_pence: 640 }, [JOURNEY], [CLAIM], {
      "/claims/c1/file": { status: 200, body: { url: "https://operator.test/claim", status: "needs_user" } },
    });
    vi.stubGlobal("open", vi.fn());
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "File" }));

    const link = await screen.findByRole("link", { name: "open it here" });
    expect(link.getAttribute("href")).toBe("https://operator.test/claim");
    expect(window.open).toHaveBeenCalledWith("https://operator.test/claim", "_blank", "noopener");
    expect(api.calls).toContain("POST /claims/c1/file");
    // The list reloads so the row shows the claim's new state — and the
    // link stays on screen throughout: the old rows are kept until the new
    // data lands.
    await waitFor(() => expect(api.calls.filter((c) => c === "GET /journeys")).toHaveLength(2));
    expect(screen.getByRole("link", { name: "open it here" })).toBeDefined();
  });

  it("shows the API's reason when filing is not possible, drops the button, and re-reads the claim", async () => {
    const api = page({ recovered_pence: 0, pending_pence: 640 }, [JOURNEY], [CLAIM], {
      "/claims/c1/file": { status: 409, body: { detail: "no filing link is available for this operator" } },
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "File" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("no filing link is available for this operator");
    // No button left to press again for the same answer.
    expect(screen.queryByRole("button", { name: "File" })).toBeNull();
    // And the claim is re-read, in case the 409 meant it had moved on.
    await waitFor(() => expect(api.calls.filter((c) => c === "GET /claims")).toHaveLength(2));
  });

  it("keeps the page and the claim link when a reload fails", async () => {
    // First load succeeds; the reload after filing fails. The person must
    // still have the operator's link and the rest of the page.
    let journeyCalls = 0;
    vi.stubGlobal("fetch", async (url: string) => {
      const path = new URL(url).pathname;
      const json = (status: number, body: unknown) =>
        new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      if (path === "/auth/me") return json(200, RIDER);
      if (path === "/claims/summary") return json(200, { recovered_pence: 0, pending_pence: 640 });
      if (path === "/claims") return json(200, { items: [CLAIM], count: 1, limit: 50 });
      if (path === "/claims/c1/file") return json(200, { url: "https://operator.test/claim", status: "needs_user" });
      if (path === "/journeys") {
        journeyCalls += 1;
        return journeyCalls === 1
          ? json(200, { items: [JOURNEY], count: 1, limit: 50 })
          : json(503, { detail: "database unavailable" });
      }
      return json(404, { detail: "not found" });
    });
    vi.stubGlobal("open", vi.fn());
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "File" }));

    await screen.findByText(/Could not refresh/);
    expect(screen.getByRole("link", { name: "open it here" })).toBeDefined();
    expect(screen.getByText("EUS → MAN")).toBeDefined();
    expect(screen.queryByText("Could not load your journeys")).toBeNull();
  });

  it("explains and offers a retry when the page cannot load", async () => {
    fakeApi({
      "/auth/me": { status: 200, body: RIDER },
      "/claims/summary": { status: 503, body: { detail: "database unavailable" } },
      "/journeys": { status: 200, body: { items: [], count: 0, limit: 50 } },
      "/claims": { status: 200, body: { items: [], count: 0, limit: 50 } },
    });
    renderPage();
    await screen.findByText("Could not load your journeys");
    expect(screen.getByRole("alert").textContent).toBe("database unavailable");
    expect(screen.getByRole("button", { name: "Try again" })).toBeDefined();
  });

  it("sign out forgets the token and goes to the front door", async () => {
    page({ recovered_pence: 0, pending_pence: 0 }, [], []);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(session.token()).toBeNull();
    expect(replace).toHaveBeenCalledWith("/");
  });
});
