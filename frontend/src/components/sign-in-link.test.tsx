import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SignInLink } from "@/components/sign-in-link";
import * as session from "@/lib/session";

// next/link needs the app router at runtime; in a unit test a plain anchor
// with the same href is all that matters.
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: React.ComponentProps<"a">) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
});

describe("SignInLink", () => {
  it("offers sign-in to a visitor", () => {
    render(<SignInLink />);
    const link = screen.getByRole("link", { name: "Sign in" });
    expect(link.getAttribute("href")).toBe("/login");
  });

  it("takes a returning user straight to the app", () => {
    session.store("a-live-jwt");
    render(<SignInLink />);
    const link = screen.getByRole("link", { name: "Your journeys" });
    expect(link.getAttribute("href")).toBe("/journeys");
  });
});
