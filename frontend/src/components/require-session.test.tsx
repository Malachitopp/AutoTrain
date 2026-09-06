/**
 * The gate. Same doubles as the login tests: a fake fetch answers /auth/me,
 * a fake router records where the visitor was sent. There is no token to
 * store — the session is a cookie the browser would send — so every case
 * is "what the API answered".
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

function fakeFetch(status: number, body: unknown): { calls: RequestInit[] } {
  const calls: RequestInit[] = [];
  vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
    calls.push(init ?? {});
    return jsonResponse(status, body);
  });
  return { calls };
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
  it("asks the API with the cookie, renders the page, and notes the session", async () => {
    const api = fakeFetch(200, RIDER);
    gate();
    await screen.findByText("Signed in as rider@example.com");
    expect(api.calls[0].credentials).toBe("include");
    expect(session.hasSession()).toBe(true);
    expect(replace).not.toHaveBeenCalled();
  });

  it("sends a visitor the API does not know to /login and drops the hint", async () => {
    session.remember(); // a stale hint from a session that has since died
    fakeFetch(401, { detail: "invalid token" });
    gate();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(session.hasSession()).toBe(false);
    expect(screen.queryByText(/Signed in as/)).toBeNull();
  });

  it("keeps the session and offers a retry when the API cannot answer", async () => {
    // Unreachable, or a 5xx (no JWT secret, a proxy mid-deploy): neither
    // says the session is bad, so nothing is forgotten and nothing redirects.
    session.remember();
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    gate();
    await screen.findByRole("button", { name: "Try again" });
    expect(session.hasSession()).toBe(true);
    expect(replace).not.toHaveBeenCalled();

    cleanup();
    fakeFetch(503, { detail: "no JWT secret configured" });
    gate();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("no JWT secret configured");
    expect(replace).not.toHaveBeenCalled();
  });

  it("retries on request and renders once the API answers", async () => {
    const answers = [jsonResponse(503, { detail: "warming up" }), jsonResponse(200, RIDER)];
    vi.stubGlobal("fetch", async () => answers.shift());
    gate();
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    await screen.findByText("Signed in as rider@example.com");
    expect(replace).not.toHaveBeenCalled();
  });
});
