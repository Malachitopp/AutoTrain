import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // react compiles JSX for component tests; tsconfigPaths makes the "@/..."
  // import alias from tsconfig.json work inside tests.
  plugins: [react()],
  resolve: { tsconfigPaths: true },
  test: {
    // jsdom gives tests a fake browser: window, localStorage, document.
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    // Pinned like the backend conftest pins its settings: a developer's
    // .env.local must not steer what the suite asserts on.
    env: { NEXT_PUBLIC_API_URL: "http://api.test" },
  },
});
