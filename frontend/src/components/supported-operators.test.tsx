import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SupportedOperators } from "@/components/supported-operators";

const LIST = [
  { atoc_code: "VT", name: "Avanti West Coast", min_delay_minutes: 15 },
  { atoc_code: "GR", name: "LNER", min_delay_minutes: 30 },
  { atoc_code: "NT", name: "Northern", min_delay_minutes: 15 },
];

function fakeOperators(status: number, body: unknown): string[] {
  const paths: string[] = [];
  vi.stubGlobal("fetch", async (url: string) => {
    paths.push(new URL(url).pathname);
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  return paths;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SupportedOperators", () => {
  it("lists the operators and works the threshold sentence out from them", async () => {
    const paths = fakeOperators(200, LIST);
    render(<SupportedOperators />);
    await screen.findByText("Avanti West Coast");
    expect(paths).toEqual(["/operators"]);
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Avanti West Coast",
      "LNER",
      "Northern",
    ]);
    expect(screen.getByText("Claims start at 15 minutes late, or 30 with LNER.")).toBeDefined();

    // The compact form is one sentence, for the add-journey page.
    cleanup();
    render(<SupportedOperators compact />);
    await screen.findByText(
      "AutoTrain can claim only for journeys with these operators: Avanti West Coast, LNER, Northern.",
    );
  });

  it("says so when the list cannot be loaded, and the section still stands", async () => {
    fakeOperators(503, { detail: "database unavailable" });
    render(<SupportedOperators />);
    await screen.findByText("The list could not be loaded just now.");
    expect(screen.getByRole("heading", { name: "Works with these operators" })).toBeDefined();
  });
});
