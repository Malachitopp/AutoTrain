import type { Metadata } from "next";
import { Geist } from "next/font/google";
import "./globals.css";

// The root layout wraps every page: the <html> and <body> live here once,
// and each route's page.tsx renders where {children} is.
const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "AutoTrain",
  description: "Delay Repay, claimed for you.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col font-sans">{children}</body>
    </html>
  );
}
