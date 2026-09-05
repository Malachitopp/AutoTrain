/**
 * The gate. Same doubles as the login tests: a fake fetch answers /auth/me,
 * a fake router records where the visitor was sent.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fakeFetch(status: number, body: unknown): void {
  vi.stubGlobal("fetch", async () => jsonResponse(status, body));
}

function WhoAmI() {
  const user = useCurrentUser();
  return <p>Signed in as {user.email}</p>;
}

function gate() {
  return render(
    <RequireSession>
      <WhoAmI />
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

describe("RequireSession", () => {
  it("renders the page for a confirmed session and hands it the user", async () => {
    session.store("a-live-jwt");
    fakeFetch(200, RIDER);
    gate();
    await screen.findByText("Signed in as rider@example.com");
    expect(replace).not.toHaveBeenCalled();
  });

  it("sends a visitor with no token to /login without asking the API", async () => {
    const calls = vi.fn();
    vi.stubGlobal("fetch", calls);
    gate();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(calls).not.toHaveBeenCalled();
    expect(screen.queryByText(/Signed in as/)).toBeNull();
  });

  it("forgets a dead token and sends the visitor to /login", async () => {
    session.store("an-expired-jwt");
    fakeFetch(401, { detail: "invalid token" });
    gate();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(session.token()).toBeNull();
  });

  it("does not forget a token another tab stored while the dead one was being checked", async () => {
    session.store("an-expired-jwt");
    let calls = 0;
    vi.stubGlobal("fetch", async () => {
      calls += 1;
      if (calls === 1) {
        // Another tab signs in again before this answer lands.
        session.store("a-fresh-jwt");
        return jsonResponse(401, { detail: "invalid token" });
      }
      return jsonResponse(200, RIDER);
    });
    gate();
    // The fresh token is checked in turn and wins; the stale 401 never
    // becomes a redirect, and never wipes the new session.
    await screen.findByText("Signed in as rider@example.com");
    expect(session.token()).toBe("a-fresh-jwt");
    expect(replace).not.toHaveBeenCalled();
  });

  it("keeps the token and offers a retry when the API is unreachable", async () => {
    session.store("a-probably-fine-jwt");
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    gate();
    await screen.findByRole("button", { name: "Try again" });
    expect(session.token()).toBe("a-probably-fine-jwt");
    expect(replace).not.toHaveBeenCalled();
  });

  it("keeps the token when the API fails for a reason other than a dead session", async () => {
    // The bearer gate answers 503 when no JWT secret is configured
    // (backend deps.py); a proxy answers 502 mid-deploy. Neither says the
    // token is bad, so the next visit must still try it.
    session.store("a-probably-fine-jwt");
    fakeFetch(503, { detail: "no JWT secret configured" });
    gate();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("no JWT secret configured");
    expect(session.token()).toBe("a-probably-fine-jwt");
    expect(replace).not.toHaveBeenCalled();
  });

  it("retries on request and renders once the API answers", async () => {
    session.store("a-live-jwt");
    const answers = [jsonResponse(503, { detail: "warming up" }), jsonResponse(200, RIDER)];
    vi.stubGlobal("fetch", async () => answers.shift());
    gate();
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    await screen.findByText("Signed in as rider@example.com");
    expect(replace).not.toHaveBeenCalled();
  });
});
