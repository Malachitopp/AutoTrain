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

const OPERATORS = [
  { atoc_code: "VT", name: "Avanti West Coast", min_delay_minutes: 15 },
  { atoc_code: "NT", name: "Northern", min_delay_minutes: 15 },
];

beforeEach(() => {
  window.localStorage.clear();
  // The one request the front door makes: the operator list.
  vi.stubGlobal("fetch", async () =>
    new Response(JSON.stringify(OPERATORS), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the front door", () => {
  it("says what AutoTrain does, names the operators it works with, and offers one way in", async () => {
    render(<LandingPage />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("worked out for you");
    // Header button and hero button, both to /login for a visitor.
    const ways_in = screen.getAllByRole("link", { name: "Sign in" });
    expect(ways_in).toHaveLength(2);
    for (const link of ways_in) expect(link.getAttribute("href")).toBe("/login");
    // The three steps are the whole explanation.
    expect(screen.getAllByText(/^STEP \d$/)).toHaveLength(3);
    // The operators come from the API, so the page never over-promises.
    await screen.findByText("Avanti West Coast");
    expect(screen.getByText("Northern")).toBeDefined();
  });
});
