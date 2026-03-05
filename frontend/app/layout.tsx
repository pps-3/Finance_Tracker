import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Finance Dashboard - Manage Your Finances",
  description: "AI-powered personal finance management dashboard",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;  // this layout receives page content (children),
  //  and it must be something React can render.
}) {
  return (
    <html lang="en">
      <body className={inter.className}>{children}</body>
    </html>
  );
}
