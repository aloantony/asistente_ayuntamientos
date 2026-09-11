export type SidebarNavIconName =
  | "home"
  | "townhall"
  | "ordinances"
  | "needs"
  | "inventory"
  | "maintenance"
  | "map"
  | "projects"
  | "iconcejo"
  | "admin"
  | "account";

export function SidebarNavIcon({ name }: { name: SidebarNavIconName }) {
  const common = {
    "aria-hidden": true,
    fill: "none",
    height: 17,
    stroke: "currentColor",
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    strokeWidth: 1.6,
    viewBox: "0 0 24 24",
    width: 17,
  };

  switch (name) {
    case "home":
      return (
        <svg {...common}>
          <rect x="3" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="3" width="7" height="7" rx="1.5" />
          <rect x="3" y="14" width="7" height="7" rx="1.5" />
          <rect x="14" y="14" width="7" height="7" rx="1.5" />
        </svg>
      );
    case "townhall":
      return (
        <svg {...common}>
          <path d="m3 10 9-6 9 6" />
          <path d="M5 10h14M6 20h12M8 10v10M12 10v10M16 10v10" />
        </svg>
      );
    case "ordinances":
      return (
        <svg {...common}>
          <path d="M5 4.5A2.5 2.5 0 0 1 7.5 2H20v17H7.5A2.5 2.5 0 0 0 5 21.5v-17Z" />
          <path d="M5 4.5v17M9 7h7M9 11h7M9 15h4" />
        </svg>
      );
    case "needs":
      return (
        <svg {...common}>
          <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
          <rect x="9" y="3" width="6" height="4" rx="1" />
          <path d="m9 13 2 2 4-4" />
        </svg>
      );
    case "projects":
      return (
        <svg {...common}>
          <path d="M4 5h5l2 2.5h9A1.5 1.5 0 0 1 21 9v9.5A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5v-12A1.5 1.5 0 0 1 4 5Z" />
        </svg>
      );
    case "map":
      return (
        <svg {...common}>
          <path d="M9 18 3.5 21V6L9 3l6 3 5.5-3v15L15 21l-6-3Z" />
          <path d="M9 3v15" />
          <path d="M15 6v15" />
        </svg>
      );
    case "inventory":
      return (
        <svg {...common}>
          <path d="M4 8.5 12 4l8 4.5v9L12 22l-8-4.5v-9Z" />
          <path d="m4 8.5 8 4.5 8-4.5M12 13v9" />
          <path d="m8 6.25 8 4.5" />
        </svg>
      );
    case "maintenance":
      return (
        <svg {...common}>
          <path d="M14.5 6.5a4 4 0 0 0-5-5l2.1 2.1-3 3-2.1-2.1a4 4 0 0 0 5 5L19 17a2.1 2.1 0 0 1-3 3l-7.5-7.5" />
          <path d="m5.5 14.5-3 3a2.1 2.1 0 0 0 3 3l3-3" />
        </svg>
      );
    case "iconcejo":
      return (
        <svg aria-hidden="true" height="17" viewBox="0 0 24 24" width="17">
          <use href="/icons/assistant-symbols.svg#icon-assistant-mark" />
        </svg>
      );
    case "admin":
      return (
        <svg {...common}>
          <path d="M4 8h10M18 8h2M4 16h2M10 16h10" />
          <circle cx="16" cy="8" r="2" />
          <circle cx="8" cy="16" r="2" />
        </svg>
      );
    case "account":
      return (
        <svg {...common}>
          <circle cx="12" cy="8" r="3.5" />
          <path d="M5 20c0-3.3 3.1-6 7-6s7 2.7 7 6" />
        </svg>
      );
  }
}
