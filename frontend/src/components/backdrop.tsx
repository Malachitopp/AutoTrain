"use client";

/**
 * The Soft Modern backdrop from the design's screen 4a: three blurred colour
 * orbs, a slow drift of pale hills, and the signature "rail running into the
 * screen" — a ground plane tilted away from the viewer with ties and rails
 * sliding toward them. The shapes and timings live in globals.css.
 *
 * Purely decorative: hidden from assistive technology, lets clicks pass
 * through, and stands still for people who ask their system for reduced
 * motion. Mouse parallax writes two numbers, --mx and --my (each between
 * -0.5 and 0.5), onto the root as CSS variables, and the layers read them
 * in CSS. No React state is involved: a pointer move never re-renders
 * anything, and the browser only repaints the layers that moved.
 */

import { useEffect, useRef } from "react";

export function Backdrop() {
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = root.current;
    if (element === null) return;
    let frame = 0;
    // Pointer events arrive faster than screens refresh; the frame request
    // coalesces them so at most one write happens per repaint.
    function follow(event: MouseEvent) {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        element!.style.setProperty("--mx", String(event.clientX / window.innerWidth - 0.5));
        element!.style.setProperty("--my", String(event.clientY / window.innerHeight - 0.5));
      });
    }
    window.addEventListener("mousemove", follow, { passive: true });
    return () => {
      window.removeEventListener("mousemove", follow);
      cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <div ref={root} className="backdrop" aria-hidden="true">
      <div className="backdrop-orb backdrop-orb-1" />
      <div className="backdrop-orb backdrop-orb-2" />
      <div className="backdrop-orb backdrop-orb-3" />
      <div className="backdrop-hills">
        <div className="backdrop-hill-strip" data-anim>
          <div className="backdrop-hill" />
          <div className="backdrop-hill" />
          <div className="backdrop-hill" />
          <div className="backdrop-hill" />
        </div>
      </div>
      <div className="backdrop-rail">
        <div className="backdrop-rail-plane">
          <div className="backdrop-rail-strip" data-anim />
        </div>
        <div className="backdrop-rail-scrim" />
      </div>
    </div>
  );
}
