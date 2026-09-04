/**
 * Tests for the contract layer. The real API is not called: `fetch` is
 * replaced with a recording double, the same move as the backend suites
 * make with the email sender. What these pin is the part that can be wrong
 * on this side of the wire — the URL, the headers, the body, and how the
 * API's error shapes are turned into ApiError.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, auth, claims, journeys } from "@/lib/api";
import * as session from "@/lib/session";

type Recorded = { url: string; init: RequestInit };

/** Install a fake fetch that answers every call with `body` at `status`,
 * and returns the list of calls it saw. */
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
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request shape", () => {
  it("posts JSON to the API base URL", async () => {
    const calls = fakeFetch(204);
    await auth.requestLogin("rider@example.com");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://api.test/auth/login/request");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(calls[0].init.body).toBe(JSON.stringify({ email: "rider@example.com" }));
  });

  it("sends the stored session as a bearer token", async () => {
    session.store("the-jwt");
    const calls = fakeFetch(200, { items: [], count: 0, limit: 50 });
    await journeys.list();
    expect(calls[0].init.headers).toMatchObject({ Authorization: "Bearer the-jwt" });
  });

  it("sends no Authorization header when signed out", async () => {
    const calls = fakeFetch(204);
    await auth.requestLogin("rider@example.com");
    expect(calls[0].init.headers).not.toHaveProperty("Authorization");
  });

  it("GET carries no body and no content type", async () => {
    const calls = fakeFetch(200, { recovered_pence: 0, pending_pence: 0 });
    await claims.summary();
    expect(calls[0].url).toBe("http://api.test/claims/summary");
    expect(calls[0].init.body).toBeUndefined();
    expect(calls[0].init.headers).not.toHaveProperty("Content-Type");
  });

  it("returns the parsed JSON body", async () => {
    fakeFetch(200, { access_token: "jwt" });
    await expect(auth.verifyLogin("magic")).resolves.toEqual({ access_token: "jwt" });
  });
});

describe("errors", () => {
  it("turns a string detail into ApiError", async () => {
    fakeFetch(401, { detail: "invalid token" });
    const failure = auth.me();
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ status: 401, detail: "invalid token" });
  });

  it("flattens a 422 validation list, naming the field", async () => {
    fakeFetch(422, {
      detail: [
        { loc: ["body", "origin_crs"], msg: "String should match pattern", type: "x" },
        { loc: ["body", "price_pence"], msg: "Input should be greater than 0", type: "x" },
      ],
    });
    await expect(
      journeys.create({
        origin_crs: "eus",
        destination_crs: "MAN",
        travel_date: "2026-09-04",
        scheduled_departure: "2026-09-04T08:00:00Z",
        scheduled_arrival: "2026-09-04T10:00:00Z",
        price_pence: 0,
      }),
    ).rejects.toMatchObject({
      status: 422,
      detail: "origin_crs: String should match pattern; price_pence: Input should be greater than 0",
    });
  });

  it("falls back to the status when the body is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      async () => new Response("<html>Bad Gateway</html>", { status: 502, statusText: "Bad Gateway" }),
    );
    await expect(claims.list()).rejects.toMatchObject({ status: 502, detail: "Bad Gateway" });
  });
});
