import type { Metadata } from "next";

import { JourneyForm } from "@/components/journey-form";
import { RequireSession } from "@/components/require-session";

export const metadata: Metadata = { title: "Add a journey — AutoTrain" };

export default function NewJourneyPage() {
  return (
    <RequireSession>
      <JourneyForm />
    </RequireSession>
  );
}
