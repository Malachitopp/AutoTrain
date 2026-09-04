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
