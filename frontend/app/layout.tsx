import type { Metadata } from "next";
import type { ReactNode } from "react";
import {
  IBM_Plex_Mono,
  IBM_Plex_Sans,
  Libre_Baskerville,
  Newsreader,
} from "next/font/google";
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

const libreBaskerville = Libre_Baskerville({
  subsets: ["latin"],
  weight: ["400", "700"],
  variable: "--font-libre-baskerville",
  display: "swap",
});

// Serif institucional del nombre del municipio en la barra del Ayuntamiento.
// Se cargan los tres pesos en uso: 400 en los bloques configurables y
// 500/600 en los titulares de la barra superior.
const newsreader = Newsreader({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-newsreader",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Anacleto",
  description: "Plataforma privada para asistencia municipal y administrativa",
  icons: {
    icon: "/brand/logo-principal.svg",
    shortcut: "/brand/logo-principal.svg",
    apple: "/brand/logo-principal.svg",
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
      className={`${ibmPlexSans.variable} ${ibmPlexMono.variable} ${libreBaskerville.variable} ${newsreader.variable}`}
      suppressHydrationWarning
    >
      <body>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
