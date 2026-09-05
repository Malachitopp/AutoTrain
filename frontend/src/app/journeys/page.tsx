import type { Metadata } from "next";

import { Dashboard } from "@/components/dashboard";
import { RequireSession } from "@/components/require-session";

// The app proper. Everything inside RequireSession renders only for a
// confirmed session; a signed-out visitor is sent to /login.
export const metadata: Metadata = { title: "Your journeys — AutoTrain" };

export default function JourneysPage() {
  return (
    <RequireSession>
      <Dashboard />
    </RequireSession>
  );
}
