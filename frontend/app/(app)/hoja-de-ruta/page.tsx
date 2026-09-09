"use client";
import { useSearchParams } from "next/navigation";
import { RoadmapPanel } from "../../components/RoadmapPanel";
export default function HojaDeRutaPage() {
  const params = useSearchParams();
  const positive = (name: string) => { const value = Number(params.get(name)); return Number.isInteger(value) && value > 0 ? value : null; };
  return <RoadmapPanel key={`${params.get("organization_id")}:${params.get("task_id")}`}
    initialOrganizationId={positive("organization_id")} initialTaskId={positive("task_id")} />;
}
