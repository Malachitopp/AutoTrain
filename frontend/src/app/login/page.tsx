import type { Metadata } from "next";

import { Login } from "@/components/login";

// A server component: it owns the route and the page title, and renders the
// client component that does the work. Metadata cannot be exported from a
// "use client" file, which is why the two are separate.
export const metadata: Metadata = { title: "Sign in — AutoTrain" };

export default function LoginPage() {
  return <Login />;
}
