import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SI · Intelligence workspace",
  description: "Build, run, and evaluate your intelligence workflows.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
