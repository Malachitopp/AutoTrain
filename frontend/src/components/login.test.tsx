/**
 * Component tests for the login screen. Two doubles stand in for the world:
 * a recording fetch (the API) and a recording router (Next's navigation).
 * jsdom supplies the address bar, so the fragment flow is exercised for
 * real: the token goes in via window.location.hash and must come out of it.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Login } from "@/components/login";
import * as session from "@/lib/session";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

type Recorded = { url: string; init: RequestInit };

function fakeFetch(status: number, body: unknown = null): Recorded[] {
  const calls: Recorded[] = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  return calls;
}

beforeEach(() => {
  window.localStorage.clear();
  window.history.replaceState(null, "", "/login");
  replace.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a plain visit", () => {
  it("shows the email form and asks for a link", async () => {
    const calls = fakeFetch(204);
    render(<Login />);
    const input = await screen.findByLabelText("Email");
    fireEvent.change(input, { target: { value: "rider@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Email me a link" }));

    await screen.findByRole("heading", { name: "Check your inbox" });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://api.test/auth/login/request");
    expect(calls[0].init.body).toBe(JSON.stringify({ email: "rider@example.com" }));
    expect(replace).not.toHaveBeenCalled();
  });

  it("shows the API's reason when the link cannot be sent", async () => {
    fakeFetch(503, { detail: "no email sender configured" });
    render(<Login />);
    fireEvent.change(await screen.findByLabelText("Email"), {
      target: { value: "rider@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Email me a link" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("no email sender configured");
  });
});

describe("a visit from the emailed link", () => {
  it("exchanges the token, stores the session, scrubs the link, and goes home", async () => {
    window.location.hash = "#token=magic-123";
    const calls = fakeFetch(200, { access_token: "the-jwt" });
    render(<Login />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(calls[0].url).toBe("http://api.test/auth/login/verify");
    expect(calls[0].init.body).toBe(JSON.stringify({ token: "magic-123" }));
    expect(session.token()).toBe("the-jwt");
    // The token must not survive in the address bar: a reload or a
    // bookmark would otherwise replay it.
    expect(window.location.hash).toBe("");
  });

  it("falls back to the form when the link is dead", async () => {
    window.location.hash = "#token=used-already";
    fakeFetch(401, { detail: "invalid token" });
    render(<Login />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("expired or was already used");
    expect(screen.getByLabelText("Email")).toBeDefined();
    expect(session.token()).toBeNull();
    expect(replace).not.toHaveBeenCalled();
  });
});
