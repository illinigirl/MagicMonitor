import type { Metadata } from "next";
import {
  Alfa_Slab_One,
  JetBrains_Mono,
  Libre_Franklin,
  Oswald,
} from "next/font/google";
import "./globals.css";

import { SiteHeader } from "@/components/site-header";
import { SessionProvider } from "@/components/auth/SessionProvider";

// Attraction-poster type stack (design handoff 2026-07-05):
// Alfa Slab One — display headings, logo, big numbers
// Oswald        — section headings, nav, labels, links (uppercase, tracked)
// Libre Franklin — body copy
// JetBrains Mono — code-ish strings (widget URL, Pushover key)
const alfa = Alfa_Slab_One({
  variable: "--font-display-alfa",
  weight: "400",
  subsets: ["latin"],
  display: "swap",
});

const oswald = Oswald({
  variable: "--font-head-oswald",
  weight: ["500", "600"],
  subsets: ["latin"],
  display: "swap",
});

const franklin = Libre_Franklin({
  variable: "--font-ui-franklin",
  weight: ["400", "600"],
  subsets: ["latin"],
  display: "swap",
});

const jetbrains = JetBrains_Mono({
  variable: "--font-mono-jetbrains",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Magic Monitor — live WDW ride status",
  description:
    "Live wait times and ride status across the four Walt Disney World parks. Polls themeparks.wiki every two minutes; alerts when your favorite rides go down.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${alfa.variable} ${oswald.variable} ${franklin.variable} ${jetbrains.variable} h-full antialiased`}
    >
      <body className="min-h-full bg-bg-0 text-fg-0 p-[14px]">
        <SessionProvider>
          {/* Poster frame — every page sits inside a 3px teal border,
              14px from the viewport edge (body padding above). */}
          <div className="flex min-h-[calc(100vh-28px)] flex-col border-[3px] border-line">
            <SiteHeader />
            <main className="flex-1">{children}</main>
            <footer className="mt-16 border-t-2 border-line py-5 text-center">
              <span className="label-meta">
                Polled from themeparks.wiki · refreshes every 2 minutes
              </span>
            </footer>
          </div>
        </SessionProvider>
      </body>
    </html>
  );
}
