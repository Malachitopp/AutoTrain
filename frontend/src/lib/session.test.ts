import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as session from "@/lib/session";

describe("the signed-in hint", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("starts absent, is set by remember and cleared by forget", () => {
    expect(session.hasSession()).toBe(false);
    session.remember();
    expect(session.hasSession()).toBe(true);
    session.forget();
    expect(session.hasSession()).toBe(false);
  });

  it("never throws when the browser blocks storage: the API is asked instead", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(() => session.remember()).not.toThrow();
    expect(session.hasSession()).toBe(false);
  });
});

describe("tokenFromHash", () => {
  it("reads the token out of a login link's fragment", () => {
    // secrets.token_urlsafe output: letters, digits, '-' and '_' only.
    expect(session.tokenFromHash("#token=Ab3-_xyz")).toBe("Ab3-_xyz");
  });

  it("is null for a plain visit, another fragment, or an empty token", () => {
    expect(session.tokenFromHash("")).toBeNull();
    expect(session.tokenFromHash("#section=faq")).toBeNull();
    expect(session.tokenFromHash("#token=")).toBeNull();
  });
});
