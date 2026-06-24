import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "HOMERUN Dashboard",
  description: "Kalshi-native MLB paper-trading dashboard.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
