/**
 * The logo tile and wordmark, as drawn in the design's header (screen 4a):
 * a 32px brand-blue tile with a train glyph, then "AutoTrain" in heavy
 * type. Used by the login screen and the signed-in header.
 */

export function Brand() {
  return (
    <span className="flex items-center gap-2.5">
      <span className="grid h-8 w-8 place-items-center rounded-control bg-brand text-white">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="17"
          height="17"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M8 3.1V7a4 4 0 0 0 8 0V3.1" />
          <path d="M9 19c-2.8 0-5-2.2-5-5v-4a8 8 0 0 1 16 0v4c0 2.8-2.2 5-5 5Z" />
          <path d="m8 19-2 3" />
          <path d="m16 19 2 3" />
        </svg>
      </span>
      <span className="text-lg font-extrabold tracking-[-0.02em]">AutoTrain</span>
    </span>
  );
}
