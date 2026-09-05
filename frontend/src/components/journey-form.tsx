"use client";

/**
 * The add-journey form: the manual way in, and the fallback for every other
 * way. Six fields, all of which the API's JourneyCreate needs, then one
 * request (POST /journeys) and back to the list.
 *
 * Times are typed in London time and converted to UTC instants here (see
 * lib/time), because the API refuses a time without a zone. An arrival
 * earlier than the departure is read as after midnight, the next day.
 * Validation the browser can do (required, three capital letters, a price
 * above zero) is done by the browser; anything the API still rejects comes
 * back as a 422 naming the field, and is shown as-is.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { AppHeader } from "@/components/app-header";
import { describeError, journeys, type JourneyKind } from "@/lib/api";
import { londonToIso, nextDay } from "@/lib/time";

const FIELD = "rounded-control border border-line bg-white px-4 py-3 text-[15px] font-semibold";
const LABEL = "text-[11px] font-semibold text-muted";

type Phase = { kind: "editing"; error?: string } | { kind: "saving" };

export function JourneyForm() {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>({ kind: "editing" });
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [date, setDate] = useState("");
  const [departs, setDeparts] = useState("");
  const [arrives, setArrives] = useState("");
  const [price, setPrice] = useState("");
  const [kind, setKind] = useState<JourneyKind>("single");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPhase({ kind: "saving" });
    const departure = londonToIso(date, departs);
    let arrival = londonToIso(date, arrives);
    if (arrival <= departure) arrival = londonToIso(nextDay(date), arrives);
    try {
      await journeys.create({
        origin_crs: from.toUpperCase(),
        destination_crs: to.toUpperCase(),
        travel_date: date,
        scheduled_departure: departure,
        scheduled_arrival: arrival,
        price_pence: Math.round(Number(price) * 100),
        kind,
      });
      router.replace("/journeys");
    } catch (error: unknown) {
      setPhase({ kind: "editing", error: describeError(error) });
    }
  }

  return (
    <>
      <AppHeader />
      <main className="mx-auto w-full max-w-md px-6 py-10">
        <h1 className="text-2xl font-extrabold tracking-[-0.02em]">Add a journey</h1>
        <p className="mt-2 text-muted">
          The details from your ticket. Station codes are the three letters printed on it, like
          EUS or MAN.
        </p>

        <form onSubmit={submit} className="mt-6 flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="from" className={LABEL}>
                From (station code)
              </label>
              <input
                id="from"
                required
                pattern="[A-Za-z]{3}"
                maxLength={3}
                autoCapitalize="characters"
                placeholder="EUS"
                value={from}
                onChange={(event) => setFrom(event.target.value)}
                className={`${FIELD} uppercase`}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="to" className={LABEL}>
                To (station code)
              </label>
              <input
                id="to"
                required
                pattern="[A-Za-z]{3}"
                maxLength={3}
                autoCapitalize="characters"
                placeholder="MAN"
                value={to}
                onChange={(event) => setTo(event.target.value)}
                className={`${FIELD} uppercase`}
              />
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="date" className={LABEL}>
              Date of travel
            </label>
            <input
              id="date"
              type="date"
              required
              value={date}
              onChange={(event) => setDate(event.target.value)}
              className={FIELD}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="departs" className={LABEL}>
                Departs
              </label>
              <input
                id="departs"
                type="time"
                required
                value={departs}
                onChange={(event) => setDeparts(event.target.value)}
                className={FIELD}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="arrives" className={LABEL}>
                Arrives
              </label>
              <input
                id="arrives"
                type="time"
                required
                value={arrives}
                onChange={(event) => setArrives(event.target.value)}
                className={FIELD}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="price" className={LABEL}>
                Ticket price (£)
              </label>
              <input
                id="price"
                type="number"
                required
                min="0.01"
                step="0.01"
                inputMode="decimal"
                placeholder="24.50"
                value={price}
                onChange={(event) => setPrice(event.target.value)}
                className={FIELD}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="kind" className={LABEL}>
                Ticket type
              </label>
              <select
                id="kind"
                value={kind}
                onChange={(event) => setKind(event.target.value as JourneyKind)}
                className={FIELD}
              >
                <option value="single">Single</option>
                <option value="return">Return</option>
                <option value="season">Season</option>
              </select>
            </div>
          </div>

          <div className="mt-2 flex items-center gap-4">
            <button
              type="submit"
              disabled={phase.kind === "saving"}
              className="rounded-control bg-cta px-6 py-3 font-semibold text-white shadow-soft transition-colors hover:bg-pink-700 disabled:opacity-50"
            >
              {phase.kind === "saving" ? "Saving…" : "Add journey"}
            </button>
            <Link href="/journeys" className="text-sm font-semibold text-muted hover:text-ink">
              Cancel
            </Link>
          </div>
        </form>

        {phase.kind === "editing" && phase.error !== undefined && (
          <p role="alert" className="mt-4 text-sm text-red-700">
            {phase.error}
          </p>
        )}
      </main>
    </>
  );
}
