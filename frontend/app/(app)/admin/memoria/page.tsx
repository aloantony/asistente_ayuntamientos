"use client";

import { RefreshCw, ShieldAlert } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import { MemoryReviewAdmin } from "../../../components/MemoryReviewAdmin";
import {
  ASSISTANT_MEMORY_STATUSES,
  type AssistantMemoryStatus,
} from "../../../components/types";
import { useMemoryReviewAdmin } from "../../../lib/admin/useMemoryReviewAdmin";
import { canUseMemoryReview } from "../../../lib/permissions";
import { useSession } from "../../../lib/session";

function parsePageParam(value: string | null) {
  const parsed = value ? Number.parseInt(value, 10) : Number.NaN;
  return Number.isInteger(parsed) && parsed > 1 ? parsed : 1;
}

function parseSelectedId(value: string | null) {
  const parsed = value ? Number.parseInt(value, 10) : Number.NaN;
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function parseMemoryStatus(value: string | null): AssistantMemoryStatus {
  return ASSISTANT_MEMORY_STATUSES.includes(value as AssistantMemoryStatus)
    ? (value as AssistantMemoryStatus)
    : "proposed";
}

function buildMemorySearch(
  status: AssistantMemoryStatus,
  page: number,
  selectedId: number | null = null,
) {
  const params = new URLSearchParams();
  if (status !== "proposed") {
    params.set("estado", status);
  }
  if (page > 1) {
    params.set("page", String(page));
  }
  if (selectedId !== null) {
    params.set("id", String(selectedId));
  }
  return params.toString();
}

function AdminMemoriaPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, getStoredToken, handleRequestError } = useSession();
  const canReview = Boolean(user && canUseMemoryReview(user));
  const memoryAdmin = useMemoryReviewAdmin({
    getStoredToken,
    handleRequestError,
  });
  const status = parseMemoryStatus(searchParams.get("estado"));
  const page = parsePageParam(searchParams.get("page"));
  const selectedId = parseSelectedId(searchParams.get("id"));
  const filters = { status, page };
  const pageCount = Math.max(
    1,
    Math.ceil(memoryAdmin.memoryTotal / memoryAdmin.adminPageSize),
  );
  const hasLoadedCurrentFilters =
    memoryAdmin.loadedFilters?.status === status &&
    memoryAdmin.loadedFilters.page === page;

  useEffect(() => {
    if (canReview) {
      void memoryAdmin.loadMemoryEntries(filters);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.permissions, status, page]);

  useEffect(() => {
    if (
      !memoryAdmin.isLoadingMemory &&
      hasLoadedCurrentFilters &&
      memoryAdmin.memoryEntries.length === 0 &&
      page > pageCount
    ) {
      replaceSearch(buildMemorySearch(status, pageCount));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    memoryAdmin.isLoadingMemory,
    memoryAdmin.memoryEntries.length,
    memoryAdmin.memoryTotal,
    page,
  ]);

  const entryIdsKey = memoryAdmin.memoryEntries
    .map((entry) => entry.id)
    .join(",");
  useEffect(() => {
    if (
      memoryAdmin.isLoadingMemory ||
      !hasLoadedCurrentFilters ||
      page > pageCount
    ) {
      return;
    }
    const firstId = memoryAdmin.memoryEntries[0]?.id ?? null;
    const selectedIsOnPage = memoryAdmin.memoryEntries.some(
      (entry) => entry.id === selectedId,
    );
    const normalizedId = selectedIsOnPage ? selectedId : firstId;
    if (normalizedId !== selectedId) {
      replaceSearch(buildMemorySearch(status, page, normalizedId));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoryAdmin.isLoadingMemory, entryIdsKey, selectedId, status, page]);

  function buildHref(search: string) {
    return search ? `/admin/memoria?${search}` : "/admin/memoria";
  }

  function replaceSearch(search: string) {
    router.replace(buildHref(search), { scroll: false });
  }

  function pushSearch(search: string) {
    router.push(buildHref(search), { scroll: false });
  }

  function changeStatus(nextStatus: AssistantMemoryStatus) {
    if (nextStatus === status && page === 1) {
      void memoryAdmin.loadMemoryEntries({ status: nextStatus, page: 1 });
      return;
    }
    pushSearch(buildMemorySearch(nextStatus, 1));
  }

  if (!user || !canReview) {
    return (
      <section className="panel">
        <p className="eyebrow">Administración</p>
        <h2>Acceso restringido</h2>
        <p className="muted">
          No tienes permisos para revisar la memoria institucional.
        </p>
      </section>
    );
  }

  return (
    <section className="panel admin-panel review-page-panel">
      <div className="panel-header">
        <div>
          <h2>Memoria institucional</h2>
        </div>
        <button
          aria-label="Actualizar memoria"
          className="secondary-button icon-button"
          disabled={
            memoryAdmin.isLoadingMemory || memoryAdmin.updatingMemoryId !== null
          }
          onClick={() => void memoryAdmin.loadMemoryEntries(filters)}
          title="Actualizar memoria"
          type="button"
        >
          <RefreshCw
            aria-hidden
            className={memoryAdmin.isLoadingMemory ? "spinning-icon" : undefined}
            size={17}
          />
        </button>
      </div>

      <div className="review-notice" role="note">
        <ShieldAlert aria-hidden size={19} />
        <p>
          Aprobar una entrada la hace visible para usuarios autorizados de la
          organización y permite incorporarla a solicitudes enviadas al
          proveedor de IA aprobado. La aprobación de contenido personal,
          sensible o legal exige confirmación expresa. Una edición material de
          una entrada ya aprobada la devuelve al estado Propuesta hasta que se
          apruebe de nuevo.
        </p>
      </div>

      {memoryAdmin.memoryError ? (
        <p className="error-message">{memoryAdmin.memoryError}</p>
      ) : null}
      {memoryAdmin.memoryMessage ? (
        <p className="success-message">{memoryAdmin.memoryMessage}</p>
      ) : null}

      <MemoryReviewAdmin
        entries={hasLoadedCurrentFilters ? memoryAdmin.memoryEntries : []}
        isLoading={memoryAdmin.isLoadingMemory}
        onNextPage={() =>
          pushSearch(buildMemorySearch(status, Math.min(pageCount, page + 1)))
        }
        onPrevPage={() =>
          pushSearch(buildMemorySearch(status, Math.max(1, page - 1)))
        }
        onSelectEntry={(entryId) =>
          pushSearch(buildMemorySearch(status, page, entryId))
        }
        onStatusFilterChange={changeStatus}
        onUpdateEntry={(entryId, updates) =>
          memoryAdmin.updateMemoryEntry(entryId, updates, filters)
        }
        page={page}
        pageSize={memoryAdmin.adminPageSize}
        selectedEntryId={selectedId}
        statusFilter={status}
        total={hasLoadedCurrentFilters ? memoryAdmin.memoryTotal : 0}
        updatingEntryId={memoryAdmin.updatingMemoryId}
      />
    </section>
  );
}

export default function AdminMemoriaPage() {
  return (
    <Suspense
      fallback={
        <section className="panel">
          <p className="muted">Cargando…</p>
        </section>
      }
    >
      <AdminMemoriaPageInner />
    </Suspense>
  );
}
