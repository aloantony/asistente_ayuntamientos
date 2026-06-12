import type { Metadata } from "next";
import type { ReactNode } from "react";
import { SessionProvider } from "./lib/session";
import "./styles.css";

export const metadata: Metadata = {
  title: "Asistente Ayuntamientos",
  description: "Plataforma privada para asistencia municipal y administrativa",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <html lang="es">
      <body>
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
