/**
 * The settings page against a fake API that answers by path, the same
 * harness as dashboard.test.tsx. Three tests, one per rule the page has:
 * what it shows, what replacing the address does, and what it says when
 * forwarding is not configured at all.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ForwardingSettings } from "@/components/forwarding-settings";
import { RequireSession } from "@/components/require-session";

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

const ADDRESS = "tickets-0123456789abcdef0123456789abcdef@in.autotrain.test";

const PARSED = {
  id: "e1",
  // Late enough that London is already on the next day: 23:30 UTC in
  // September is 00:30 on the 6th in London.
  received_at: "2026-09-05T23:30:00Z",
  sender: "noreply@trainline.com",
  subject: "Your e-ticket",
  status: "parsed",
  status_reason: "2 journey(s) added",
};

const SETUP = {
  id: "e2",
  received_at: "2026-09-05T09:00:00Z",
  sender: "forwarding-noreply@google.com",
  subject: "Gmail Forwarding Confirmation (#33821484)",
  status: "confirmation",
  status_reason: "rider@example.com asked to forward mail here. Enter code 33821484 in Gmail to switch it on.",
};

type Route = { status: number; body: unknown };

function fakeApi(routes: Record<string, Route>): { calls: string[] } {
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    calls.push(`${init?.method ?? "GET"} ${path}`);
    const route = routes[path];
    if (route === undefined) {
      return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    }
    return new Response(JSON.stringify(route.body), {
      status: route.status,
      headers: { "Content-Type": "application/json" },
    });
  });
  return { calls };
}

function renderPage() {
  return render(
    <RequireSession>
      <ForwardingSettings />
    </RequireSession>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  replace.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Forwarding settings", () => {
  it("shows the address, the setup code to act on, and what became of each email", async () => {
    fakeApi({
      "/auth/me": { status: 200, body: RIDER },
      "/intake/address": { status: 200, body: { address: ADDRESS } },
      "/intake/emails": { status: 200, body: [PARSED, SETUP] },
    });
    renderPage();

    await screen.findByText(ADDRESS);
    // The code is lifted out of the list into its own notice: it is the one
    // thing on the page a person has to do something about.
    const notice = await screen.findByText(/Enter code 33821484 in Gmail/);
    expect(notice).toBeDefined();
    expect(screen.getByText("Finish setting up forwarding")).toBeDefined();
    // Both emails are listed, each with its status in plain words.
    expect(screen.getByText("Your e-ticket")).toBeDefined();
    expect(screen.getByText("Journey added")).toBeDefined();
    expect(screen.getByText("2 journey(s) added")).toBeDefined();
    expect(screen.getByText("Action needed")).toBeDefined();
    // Date and time both read in London, so they cannot disagree. This
    // email arrived at 23:30 UTC, which is 00:30 the NEXT day in London:
    // taking the date off the ISO string would pair the 5th with 00:30.
    // (Sep or Sept: the abbreviation varies by Node's date data.)
    expect(screen.getByText(/6 Sept? · 00:30/)).toBeDefined();
  });

  it("replaces the address only after confirming, then re-reads the page", async () => {
    const fresh = "tickets-ffffffffffffffffffffffffffffffff@in.autotrain.test";
    let addressCalls = 0;
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      const path = new URL(url).pathname;
      calls.push(`${init?.method ?? "GET"} ${path}`);
      const json = (status: number, body: unknown) =>
        new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      if (path === "/auth/me") return json(200, RIDER);
      if (path === "/intake/emails") return json(200, []);
      if (path === "/intake/address/rotate") return json(200, { address: fresh });
      if (path === "/intake/address") {
        addressCalls += 1;
        return json(200, { address: addressCalls === 1 ? ADDRESS : fresh });
      }
      return json(404, { detail: "not found" });
    });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Replace this address" }));
    // Asking first: the old address still stands until the person agrees.
    expect(screen.getByText(ADDRESS)).toBeDefined();
    expect(calls).not.toContain("POST /intake/address/rotate");

    fireEvent.click(screen.getByRole("button", { name: "Yes, replace it" }));

    await screen.findByText(fresh);
    expect(calls).toContain("POST /intake/address/rotate");
    expect(screen.queryByText(ADDRESS)).toBeNull();
  });

  it("explains rather than alarms when no mail domain is configured", async () => {
    // What every development machine answers until an inbound domain is set.
    fakeApi({
      "/auth/me": { status: 200, body: RIDER },
      "/intake/address": { status: 503, body: { detail: "no inbound email domain configured" } },
      "/intake/emails": { status: 200, body: [] },
    });
    renderPage();

    await screen.findByText("Not switched on yet");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Replace this address" })).toBeNull();
    expect(screen.getByText(/Nothing yet/)).toBeDefined();
  });
});
