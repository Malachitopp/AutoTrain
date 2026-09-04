import { beforeEach, describe, expect, it } from "vitest";

import * as session from "@/lib/session";

describe("session storage", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("starts signed out", () => {
    expect(session.token()).toBeNull();
  });

  it("round-trips a token", () => {
    session.store("jwt-goes-here");
    expect(session.token()).toBe("jwt-goes-here");
  });

  it("clear signs out", () => {
    session.store("jwt-goes-here");
    session.clear();
    expect(session.token()).toBeNull();
  });
});

describe("tokenFromHash", () => {
  it("reads the token out of a login link's fragment", () => {
    // secrets.token_urlsafe output: letters, digits, '-' and '_' only.
    expect(session.tokenFromHash("#token=Ab3-_xyz")).toBe("Ab3-_xyz");
  });

  it("is null for a plain visit to /login", () => {
    expect(session.tokenFromHash("")).toBeNull();
  });

  it("is null for a fragment that names something else", () => {
    expect(session.tokenFromHash("#section=faq")).toBeNull();
  });

  it("is null for an empty token", () => {
    expect(session.tokenFromHash("#token=")).toBeNull();
  });
});
