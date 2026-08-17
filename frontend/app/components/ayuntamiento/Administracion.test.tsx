// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type {
  MunicipalContract,
  MunicipalGrant,
  MunicipalLicence,
  MunicipalNotice,
  OfficeHour,
} from "../types";
import { Administracion, formatAmount, formatMinutes } from "./Administracion";
import { Comunicacion, describeExposure } from "./Comunicacion";

afterEach(cleanup);
beforeEach(() => window.localStorage.clear());

function officeHour(overrides: Partial<OfficeHour> = {}): OfficeHour {
  return {
    id: 1,
    organization_id: 1,
    office_name: "Secretaría",
    weekday: "monday",
    opens_at: 540,
    closes_at: 780,
    notes: null,
    created_at: "2026-08-05T10:00:00Z",
    updated_at: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

function licence(overrides: Partial<MunicipalLicence> = {}): MunicipalLicence {
  return {
    id: 1,
    organization_id: 1,
    reference: "LIC-2026-01",
    kind: "works",
    applicant: "Vecina de la calle Mayor",
    address: null,
    summary: null,
    status: "requested",
    requested_on: "2026-03-01",
    resolved_on: null,
    fee_amount: null,
    created_at: "2026-08-05T10:00:00Z",
    updated_at: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

function notice(overrides: Partial<MunicipalNotice> = {}): MunicipalNotice {
  return {
    id: 1,
    organization_id: 1,
    kind: "bando",
    title: "Corte de agua",
    body: null,
    status: "draft",
    published_on: null,
    expires_on: null,
    publish_to_sede: false,
    created_by_id: null,
    created_at: "2026-08-05T10:00:00Z",
    updated_at: "2026-08-05T10:00:00Z",
    events: [],
    ...overrides,
  };
}

const NO_CONTRACTS: MunicipalContract[] = [];
const NO_GRANTS: MunicipalGrant[] = [];

describe("formatMinutes", () => {
  it("lee los minutos desde medianoche como una hora", () => {
    expect(formatMinutes(540)).toBe("09:00");
    expect(formatMinutes(0)).toBe("00:00");
    expect(formatMinutes(1439)).toBe("23:59");
    expect(formatMinutes(1440)).toBe("24:00");
  });
});

describe("formatAmount", () => {
  it("devuelve null en lugar de un importe inventado", () => {
    expect(formatAmount(null)).toBeNull();
    expect(formatAmount("no es un numero")).toBeNull();
  });
});

describe("describeExposure", () => {
  it("conserva la fecha de exposición de un bando retirado", () => {
    // Que estuvo expuesto ese día puede tener que demostrarse después.
    expect(
      describeExposure(
        notice({ status: "withdrawn", published_on: "2026-08-05" }),
      ),
    ).toMatch(/retirado/);
  });

  it("distingue lo no expuesto de lo expuesto sin fin", () => {
    expect(describeExposure(notice())).toBe("Sin exponer");
    expect(
      describeExposure(notice({ status: "published", published_on: "2026-08-05" })),
    ).toMatch(/^Desde /);
  });
});

describe("Administracion", () => {
  it("declara la falta de permiso en lugar de un bloque vacío", () => {
    render(
      <Administracion
        canView={false}
        contracts={NO_CONTRACTS}
        grants={NO_GRANTS}
        licences={[licence()]}
        officeHours={[officeHour()]}
      />,
    );

    expect(screen.getByText("Administración no autorizado")).toBeTruthy();
    expect(screen.queryByText("Licencias")).toBeNull();
  });

  it("distingue lo vacío de lo restringido", () => {
    render(
      <Administracion
        canView
        contracts={NO_CONTRACTS}
        grants={NO_GRANTS}
        licences={[]}
        officeHours={[]}
      />,
    );

    expect(screen.getByText("Administración sin datos")).toBeTruthy();
  });

  it("pinta el horario en horas legibles", () => {
    render(
      <Administracion
        canView
        contracts={NO_CONTRACTS}
        grants={NO_GRANTS}
        licences={[]}
        officeHours={[officeHour()]}
      />,
    );

    expect(screen.getByText("09:00–13:00")).toBeTruthy();
    expect(screen.getByText("Lunes")).toBeTruthy();
  });

  it("omite los bloques que no tienen nada que enseñar", () => {
    render(
      <Administracion
        canView
        contracts={NO_CONTRACTS}
        grants={NO_GRANTS}
        licences={[licence()]}
        officeHours={[]}
      />,
    );

    expect(screen.getByText("Licencias")).toBeTruthy();
    expect(screen.queryByText("Atención al público")).toBeNull();
    expect(screen.queryByText("Subvenciones")).toBeNull();
  });
});

describe("Comunicacion", () => {
  it("distingue la falta de permiso de la falta de bandos", () => {
    const { unmount } = render(<Comunicacion canView={false} notices={[]} />);
    expect(screen.getByText("Comunicación no autorizada")).toBeTruthy();
    unmount();

    render(<Comunicacion canView notices={[]} />);
    expect(screen.getByText("Sin bandos registrados")).toBeTruthy();
  });

  it("muestra el estado de exposición de cada bando", () => {
    render(
      <Comunicacion
        canView
        notices={[notice({ status: "published", published_on: "2026-08-05" })]}
      />,
    );

    expect(screen.getByText("Expuesto")).toBeTruthy();
  });
});
