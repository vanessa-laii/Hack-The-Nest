import type { Metadata } from "next";
import {
  IBM_Plex_Mono,
  Manrope,
  Playfair_Display,
  Space_Grotesk,
} from "next/font/google";
import { Footer } from "@/components/Footer";
import "./globals.css";

const display = Playfair_Display({
  variable: "--font-display",
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  style: ["normal", "italic"],
  display: "swap",
});

/** Geometric sans for UI chrome (place names in picker / cards). */
const ui = Space_Grotesk({
  variable: "--font-ui",
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  display: "swap",
});

const label = IBM_Plex_Mono({
  variable: "--font-label",
  subsets: ["latin"],
  weight: ["400", "500"],
  display: "swap",
});

const body = Manrope({
  variable: "--font-body",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Revisit",
  description:
    "Type a place → a 60–90s narrated Landsat film of how it changed since 1984.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${display.variable} ${ui.variable} ${label.variable} ${body.variable} h-full antialiased`}
    >
      <body className="flex h-dvh flex-col overflow-hidden bg-[#121214] text-[#f5f5f7]">
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          {children}
        </div>
        <Footer />
      </body>
    </html>
  );
}
