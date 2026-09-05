import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import LandingPage from "@/app/page";

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

describe("the front door", () => {
  it("says what AutoTrain does and offers one way in", () => {
    render(<LandingPage />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("worked out for you");
    // Header button and hero button, both to /login for a visitor.
    const ways_in = screen.getAllByRole("link", { name: "Sign in" });
    expect(ways_in).toHaveLength(2);
    for (const link of ways_in) expect(link.getAttribute("href")).toBe("/login");
    // The three steps are the whole explanation.
    expect(screen.getAllByText(/^STEP \d$/)).toHaveLength(3);
  });
});
