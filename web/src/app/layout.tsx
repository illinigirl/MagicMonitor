import type { Metadata } from "next";
import { Fraunces, Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";

import { SiteHeader } from "@/components/site-header";

// Type stack mirrors Watchtower for portfolio visual consistency.
// Fraunces — display + headlines (serif, editorial castle vibe)
// Inter   — UI body, with ss01/cv11 features set on body
// JetBrains Mono — small uppercase meta labels (LL prices, status)
const fraunces = Fraunces({
  variable: "--font-display-fraunces",
  subsets: ["latin"],
  display: "swap",
  axes: ["opsz"],
});

const inter = Inter({
  variable: "--font-ui-inter",
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
      className={`${fraunces.variable} ${inter.variable} ${jetbrains.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-bg-0 text-fg-0">
        <SiteHeader />
        <main className="flex-1">{children}</main>
        <footer className="border-t border-line-soft mt-16 py-6 text-center text-fg-3 text-xs">
          Polled from themeparks.wiki · refreshes every 2 minutes
        </footer>
      </body>
    </html>
  );
}
