import type { Metadata } from "next";

import { ForwardingSettings } from "@/components/forwarding-settings";
import { RequireSession } from "@/components/require-session";

// Signed-in only, like /journeys: the forwarding address belongs to one
// account, so there is nothing here for a visitor without a session.
export const metadata: Metadata = { title: "Settings — AutoTrain" };

export default function SettingsPage() {
  return (
    <RequireSession>
      <ForwardingSettings />
    </RequireSession>
  );
}
