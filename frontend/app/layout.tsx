import type { Metadata } from "next";
import type { ReactNode } from "react";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import { SessionProvider } from "./lib/session";
import "./styles.css";

// Fuentes IBM Plex servidas por next/font: se descargan en build y se sirven
// desde nuestro propio host, sin peticiones del navegador a la CDN de Google
// (coherente con la postura de privacidad del proyecto). Exponen las variables
// CSS que styles.css consume en :root.
const ibmPlexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-ibm-plex-sans",
  display: "swap",
});

const ibmPlexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-ibm-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Anacleto",
  description: "Plataforma privada para asistencia municipal y administrativa",
  icons: {
    icon: "/anacleto-logo.svg",
    shortcut: "/anacleto-logo.svg",
    apple: "/anacleto-logo.svg",
  },
};

// Aplica el tema guardado (o la preferencia del sistema) antes de pintar, para
// evitar el parpadeo claro→oscuro. Debe ejecutarse de forma síncrona al inicio.
const themeScript = `try{var t=localStorage.getItem('theme');if(t==='dark'||(!t&&matchMedia('(prefers-color-scheme:dark)').matches)){document.documentElement.classList.add('dark');}}catch(e){}`;

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <html
      lang="es"
      className={`${ibmPlexSans.variable} ${ibmPlexMono.variable}`}
      suppressHydrationWarning
    >
      <body>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
