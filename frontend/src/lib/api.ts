/**
 * The one module that speaks HTTP to the AutoTrain API.
 *
 * Pages and components call the functions at the bottom of this file and get
 * typed values back; they never build URLs, headers or fetch calls
 * themselves. That mirrors the backend's repository layer, where only one
 * module writes SQL. If the API moves, gains a version prefix, or changes how
 * sessions are sent, this file changes and nothing else does.
 *
 * The types mirror backend/src/autotrain/api/schemas.py field for field, in
 * the same snake_case: each type IS the wire shape, not a translation of it.
 * Timestamps arrive as ISO 8601 strings in UTC (the API speaks UTC only);
 * money is integer pence; ids are UUID strings.
 */

import * as session from "@/lib/session";

// --- Wire shapes ---------------------------------------------------------

/** GET /auth/me. claim_consent_at is null until the user grants auto-filing
 * consent in the app; the frontend decides from it whether to show the
 * consent screen. */
export type User = {
  id: string;
  email: string;
  claim_consent_at: string | null;
  created_at: string;
};

/** POST /auth/login/verify. */
export type Session = { access_token: string };

export type JourneyKind = "single" | "return" | "season";

/** POST /journeys body. CRS codes are three capital letters (EUS, MAN);
 * travel_date is the UK timetable day (YYYY-MM-DD); the two scheduled
 * instants must carry a timezone offset or the API rejects them as a 422. */
export type JourneyCreate = {
  origin_crs: string;
  destination_crs: string;
  travel_date: string;
  scheduled_departure: string;
  scheduled_arrival: string;
  price_pence: number;
  kind?: JourneyKind;
};

export type Journey = {
  id: string;
  ticket_id: string;
  origin_crs: string;
  destination_crs: string;
  travel_date: string;
  scheduled_departure: string;
  scheduled_arrival: string;
  status: string;
  created_at: string;
};

export type Page<T> = { items: T[]; count: number; limit: number };

/** GET /journeys/{id}/decision: the frozen delay decision for one journey.
 * band_percent is null when the delay earned nothing. */
export type Decision = {
  actual_arrival: string;
  delay_minutes: number;
  source: string;
  band_percent: number | null;
  entitlement_pence: number;
  observed_at: string;
};

export type Claim = {
  id: string;
  journey_id: string;
  operator_id: string;
  amount_pence: number;
  status: string;
  file_by: string;
  submitted_at: string | null;
  resolved_at: string | null;
  operator_reference: string | null;
  created_at: string;
};

/** One audited state change. from_status null is the creation event. */
export type ClaimEvent = {
  from_status: string | null;
  to_status: string;
  detail: string | null;
  created_at: string;
};

/** GET /claims/summary: the money box on the home screen. */
export type ClaimSummary = { recovered_pence: number; pending_pence: number };

/** POST /claims/{id}/file: where the user files this claim, and the status
 * the claim was left in. */
export type ClaimFiling = { url: string; status: string };

// --- Errors --------------------------------------------------------------

/** Any non-2xx answer. `status` is the HTTP code, `detail` the API's own
 * message (FastAPI puts one in every error body). Callers branch on status:
 * 401 means sign in again, 404 means it is gone, 422 names a bad field. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
  }
}

// --- The single request path --------------------------------------------

/** Read at build time for browser bundles: only NEXT_PUBLIC_ variables are
 * inlined into client code. Set in frontend/.env.local (see .env.example). */
const BASE_URL = process.env.NEXT_PUBLIC_API_URL;

async function request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  if (!BASE_URL) {
    throw new Error("NEXT_PUBLIC_API_URL is not set — copy frontend/.env.example to .env.local");
  }
  const headers: Record<string, string> = {};
  const jwt = session.token();
  if (jwt !== null) headers["Authorization"] = `Bearer ${jwt}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(response.status, await detailOf(response));
  // 204 carries no body by definition (the login request endpoint).
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** FastAPI error bodies are {"detail": ...}: a string for HTTPException, a
 * list of {loc, msg, ...} for validation (422). Anything else falls back to
 * the status text, so a proxy's HTML error page still reads as something. */
async function detailOf(response: Response): Promise<string> {
  try {
    const parsed: unknown = await response.json();
    if (typeof parsed === "object" && parsed !== null && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
      if (Array.isArray(detail)) {
        return detail
          .map((item: unknown) => {
            if (typeof item === "object" && item !== null && "msg" in item) {
              const { loc, msg } = item as { loc?: unknown[]; msg: unknown };
              const field = Array.isArray(loc) ? loc.filter((p) => p !== "body").join(".") : "";
              return field ? `${field}: ${String(msg)}` : String(msg);
            }
            return String(item);
          })
          .join("; ");
      }
    }
  } catch {
    // not JSON — fall through
  }
  return response.statusText || `HTTP ${response.status}`;
}

// --- Endpoints -----------------------------------------------------------
// One function per route, named for what the user is doing. Grouped by the
// backend router they belong to, so the two sides read the same way.

export const auth = {
  /** Always resolves, account or not (user enumeration: the API says nothing
   * either way). The link arrives by email; nothing comes back here. */
  requestLogin: (email: string) => request<void>("POST", "/auth/login/request", { email }),
  /** Exchange the token from the emailed link for a session. 401 for a
   * token that is unknown, expired, or already used. */
  verifyLogin: (token: string) => request<Session>("POST", "/auth/login/verify", { token }),
  /** Who the stored session belongs to. 401 once it expires or the account
   * is erased. */
  me: () => request<User>("GET", "/auth/me"),
};

export const journeys = {
  list: (limit = 50) => request<Page<Journey>>("GET", `/journeys?limit=${limit}`),
  create: (input: JourneyCreate) => request<Journey>("POST", "/journeys", input),
  get: (id: string) => request<Journey>("GET", `/journeys/${id}`),
  /** 404 until the ingestor has assessed the journey. */
  decision: (id: string) => request<Decision>("GET", `/journeys/${id}/decision`),
};

export const claims = {
  list: (limit = 50) => request<Page<Claim>>("GET", `/claims?limit=${limit}`),
  summary: () => request<ClaimSummary>("GET", "/claims/summary"),
  get: (id: string) => request<Claim>("GET", `/claims/${id}`),
  /** Hands the claim to the operator's form: returns the URL to open. */
  file: (id: string) => request<ClaimFiling>("POST", `/claims/${id}/file`),
  events: (id: string) => request<ClaimEvent[]>("GET", `/claims/${id}/events`),
};
