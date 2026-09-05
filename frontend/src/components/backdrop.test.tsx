import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Backdrop } from "@/components/backdrop";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Backdrop", () => {
  it("is decorative and follows the pointer until unmounted", () => {
    // Run frame callbacks at once: the test has no screen to wait for.
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {});
    Object.defineProperty(window, "innerWidth", { value: 1000, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 500, configurable: true });

    const { container, unmount } = render(<Backdrop />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.getAttribute("aria-hidden")).toBe("true");
    // The hill strip and the rail strip are the two animated layers, and
    // both carry the marker that reduced-motion CSS switches off.
    expect(container.querySelectorAll("[data-anim]")).toHaveLength(2);
    // Four hills: two shapes and their twins, so the drift loops seamlessly.
    expect(container.querySelectorAll(".backdrop-hill")).toHaveLength(4);

    // Three quarters across, one quarter down: +0.25 and -0.25 from centre.
    window.dispatchEvent(new MouseEvent("mousemove", { clientX: 750, clientY: 125 }));
    expect(root.style.getPropertyValue("--mx")).toBe("0.25");
    expect(root.style.getPropertyValue("--my")).toBe("-0.25");

    // After unmount the listener is gone: the last values stay put.
    unmount();
    window.dispatchEvent(new MouseEvent("mousemove", { clientX: 0, clientY: 0 }));
    expect(root.style.getPropertyValue("--mx")).toBe("0.25");
  });
});
