import type { Metadata } from "next";
import { Geist_Mono, Inter, Bricolage_Grotesque } from "next/font/google";
import "./globals.css";

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

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${bricolage.variable} ${inter.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
