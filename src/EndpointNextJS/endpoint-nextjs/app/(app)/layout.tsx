import type { ReactNode } from "react";

import "@/features/cards/cards.css";
import { AppShell } from "@/shell/AppShell";

export default function AppLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
