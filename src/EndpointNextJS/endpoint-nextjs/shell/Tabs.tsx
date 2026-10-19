"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/assignments", label: "Assignments" },
  { href: "/announcements", label: "Announcements" },
] as const;

export function Tabs() {
  const pathname = usePathname();
  return (
    <nav className="shell-tabs" aria-label="Dashboards">
      {TABS.map((tab) => {
        const active = pathname?.startsWith(tab.href);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            className="shell-tab"
            aria-current={active ? "page" : undefined}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
