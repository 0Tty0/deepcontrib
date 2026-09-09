import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "DeepContrib",
  description: "A durable, review-first GitHub contribution workbench.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
