import type { Metadata, Viewport } from "next";
import { Geist_Mono, Inter, Bricolage_Grotesque } from "next/font/google";
import "./globals.css";
import { ThreadProvider } from "@/components/ThreadProvider";
import { AppShell } from "@/components/AppShell";

// ui_plan.md §2 — "Display in a grotesk with actual character... pick one and commit."
// Bricolage Grotesque, committed. Body in Inter. Utility mono stays Geist Mono (record
// IDs, form numbers, dates, the evidence chain).
const bricolage = Bricolage_Grotesque({
  variable: "--font-bricolage",
  subsets: ["latin"],
});

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "FO Intelligence — Family Office Records",
  description: "Grounded search over a verified family-office dataset assembled from public filings.",
};

// T47.4 — `interactiveWidget: "resizes-content"` is what makes the mobile keyboard PUSH
// the docked composer instead of covering it. Without it the composer is the single most
// common phone bug in this class of interface.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  interactiveWidget: "resizes-content",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#eceff4" },
    { media: "(prefers-color-scheme: dark)", color: "#0d1014" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${bricolage.variable} ${inter.variable} ${geistMono.variable} antialiased`}
    >
      <body>
        {/* T47.1 — one shell owns all three routes. The provider sits above it so the
            thread survives navigation to /watch and /log, and so the evidence surface can
            render as a sibling of the main column rather than a child of it. */}
        <ThreadProvider>
          <AppShell>{children}</AppShell>
        </ThreadProvider>
      </body>
    </html>
  );
}
