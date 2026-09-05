import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Dashboard } from "@/components/dashboard";
import { RequireSession } from "@/components/require-session";
import * as session from "@/lib/session";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

const RIDER = {
  id: "5c1e0b7e-0000-4000-8000-000000000001",
  email: "rider@example.com",
  claim_consent_at: null,
  created_at: "2026-09-04T12:00:00Z",
};

beforeEach(() => {
  window.localStorage.clear();
  replace.mockReset();
  vi.stubGlobal(
    "fetch",
    async () =>
      new Response(JSON.stringify(RIDER), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Dashboard", () => {
  it("shows who is signed in", async () => {
    session.store("a-live-jwt");
    render(
      <RequireSession>
        <Dashboard />
      </RequireSession>,
    );
    await screen.findByText("rider@example.com");
  });

  it("sign out forgets the token and goes to the front door", async () => {
    // The only place the session is ever forgotten on purpose: if this
    // stopped clearing storage, the button would still navigate and look
    // right, and the next visit would be signed in.
    session.store("a-live-jwt");
    render(
      <RequireSession>
        <Dashboard />
      </RequireSession>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(session.token()).toBeNull();
    expect(replace).toHaveBeenCalledWith("/");
  });
});
