import { Dashboard } from "@/components/dashboard";
import { RequireSession } from "@/components/require-session";

// The home route. Everything inside RequireSession renders only for a
// confirmed session; a signed-out visitor is sent to /login.
export default function HomePage() {
  return (
    <RequireSession>
      <Dashboard />
    </RequireSession>
  );
}
