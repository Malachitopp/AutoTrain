import type { Metadata } from "next";
import "./globals.css";

import { Backdrop } from "@/components/backdrop";

// The root layout wraps every page: the <html> and <body> live here once,
// and each route's page.tsx renders where {children} is. The backdrop sits
// behind everything; the page content is lifted above it with z-index.
export const metadata: Metadata = {
  title: "AutoTrain",
  description: "Delay Repay, claimed for you.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col font-sans text-ink">
        <Backdrop />
        <div className="relative z-10 flex flex-1 flex-col">{children}</div>
      </body>
    </html>
  );
}
