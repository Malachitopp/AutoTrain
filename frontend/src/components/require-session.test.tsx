/**
 * The gate. Same doubles as the login tests: a fake fetch answers /auth/me,
 * a fake router records where the visitor was sent.
 */

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RequireSession, useCurrentUser } from "@/components/require-session";
import * as session from "@/lib/session";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

const RIDER = {
  id: "5c1e0b7e-0000-4000-8000-000000000001",
  email: "rider@example.com",
  claim_consent_at: null,
  created_at: "2026-09-04T12:00:00Z",
};

function fakeFetch(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

function WhoAmI() {
  const user = useCurrentUser();
  return <p>Signed in as {user.email}</p>;
}

beforeEach(() => {
  window.localStorage.clear();
  replace.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("RequireSession", () => {
  it("renders the page for a confirmed session and hands it the user", async () => {
    session.store("a-live-jwt");
    fakeFetch(200, RIDER);
    render(
      <RequireSession>
        <WhoAmI />
      </RequireSession>,
    );
    await screen.findByText("Signed in as rider@example.com");
    expect(replace).not.toHaveBeenCalled();
  });

  it("sends a visitor with no token to /login without asking the API", async () => {
    const calls = vi.fn();
    vi.stubGlobal("fetch", calls);
    render(
      <RequireSession>
        <WhoAmI />
      </RequireSession>,
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(calls).not.toHaveBeenCalled();
    expect(screen.queryByText(/Signed in as/)).toBeNull();
  });

  it("forgets a dead token and sends the visitor to /login", async () => {
    session.store("an-expired-jwt");
    fakeFetch(401, { detail: "invalid token" });
    render(
      <RequireSession>
        <WhoAmI />
      </RequireSession>,
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(session.token()).toBeNull();
  });

  it("keeps the token when the API is unreachable", async () => {
    session.store("a-probably-fine-jwt");
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    render(
      <RequireSession>
        <WhoAmI />
      </RequireSession>,
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(session.token()).toBe("a-probably-fine-jwt");
  });
});
