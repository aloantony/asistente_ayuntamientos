"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useSession } from "../../lib/session";
import { getAdminNavItems } from "./nav";

export default function AdminLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  const pathname = usePathname();
  const { user } = useSession();
  const navItems = user ? getAdminNavItems(user) : [];

  return (
    <div className="workspace">
      <header>
        <p className="eyebrow">Administración</p>
        {navItems.length > 0 ? (
          <nav className="admin-subnav">
            {navItems.map((item) => (
              <Link
                className={
                  pathname.startsWith(item.href) ? "active" : undefined
                }
                href={item.href}
                key={item.href}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        ) : null}
      </header>
      {children}
    </div>
  );
}
