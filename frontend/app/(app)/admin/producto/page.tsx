"use client";

import { RefreshCw, ShieldAlert } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import { ProductFeedbackAdmin } from "../../../components/ProductFeedbackAdmin";
import {
  ASSISTANT_ADMIN_FEEDBACK_STATUSES,
  type AssistantAdminFeedbackStatus,
} from "../../../components/types";
import {
  type ProductFeedbackStatusFilter,
  useProductFeedbackAdmin,
} from "../../../lib/admin/useProductFeedbackAdmin";
import { canUseProductReview } from "../../../lib/permissions";
import { useSession } from "../../../lib/session";

function parsePageParam(value: string | null) {
  const parsed = value ? Number.parseInt(value, 10) : Number.NaN;
  return Number.isInteger(parsed) && parsed > 1 ? parsed : 1;
}

function parseSelectedId(value: string | null) {
  const parsed = value ? Number.parseInt(value, 10) : Number.NaN;
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function parseFeedbackStatus(value: string | null): ProductFeedbackStatusFilter {
  if (value === "todos") {
    return "all";
  }
  return ASSISTANT_ADMIN_FEEDBACK_STATUSES.includes(
    value as AssistantAdminFeedbackStatus,
  )
    ? (value as AssistantAdminFeedbackStatus)
    : "submitted";
}

function buildProductSearch(
  status: ProductFeedbackStatusFilter,
  page: number,
  selectedId: number | null = null,
) {
  const params = new URLSearchParams();
  if (status === "all") {
    params.set("estado", "todos");
  } else if (status !== "submitted") {
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

function AdminProductoPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, getStoredToken, handleRequestError } = useSession();
  const canReview = Boolean(user && canUseProductReview(user));
  const productAdmin = useProductFeedbackAdmin({
    getStoredToken,
    handleRequestError,
  });
  const status = parseFeedbackStatus(searchParams.get("estado"));
  const page = parsePageParam(searchParams.get("page"));
  const selectedId = parseSelectedId(searchParams.get("id"));
  const filters = { status, page };
  const pageCount = Math.max(
    1,
    Math.ceil(productAdmin.feedbackTotal / productAdmin.adminPageSize),
  );
  const hasLoadedCurrentFilters =
    productAdmin.loadedFilters?.status === status &&
    productAdmin.loadedFilters.page === page;

  useEffect(() => {
    if (canReview) {
      void productAdmin.loadFeedback(filters);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.is_superuser, status, page]);

  useEffect(() => {
    if (
      !productAdmin.isLoadingFeedback &&
      hasLoadedCurrentFilters &&
      productAdmin.feedbackItems.length === 0 &&
      page > pageCount
    ) {
      replaceSearch(buildProductSearch(status, pageCount));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    productAdmin.isLoadingFeedback,
    productAdmin.feedbackItems.length,
    productAdmin.feedbackTotal,
    page,
  ]);

  const feedbackIdsKey = productAdmin.feedbackItems
    .map((item) => item.id)
    .join(",");
  useEffect(() => {
    if (
      productAdmin.isLoadingFeedback ||
      !hasLoadedCurrentFilters ||
      page > pageCount
    ) {
      return;
    }
    const firstId = productAdmin.feedbackItems[0]?.id ?? null;
    const selectedIsOnPage = productAdmin.feedbackItems.some(
      (item) => item.id === selectedId,
    );
    const normalizedId = selectedIsOnPage ? selectedId : firstId;
    if (normalizedId !== selectedId) {
      replaceSearch(buildProductSearch(status, page, normalizedId));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [productAdmin.isLoadingFeedback, feedbackIdsKey, selectedId, status, page]);

  function buildHref(search: string) {
    return search ? `/admin/producto?${search}` : "/admin/producto";
  }

  function replaceSearch(search: string) {
    router.replace(buildHref(search), { scroll: false });
  }

  function pushSearch(search: string) {
    router.push(buildHref(search), { scroll: false });
  }

  function changeStatus(nextStatus: ProductFeedbackStatusFilter) {
    if (nextStatus === status && page === 1) {
      void productAdmin.loadFeedback({ status: nextStatus, page: 1 });
      return;
    }
    pushSearch(buildProductSearch(nextStatus, 1));
  }

  if (!user || !canReview) {
    return (
      <section className="panel">
        <p className="eyebrow">Administración</p>
        <h2>Acceso restringido</h2>
        <p className="muted">
          La revisión global de producto está reservada al desarrollador.
        </p>
      </section>
    );
  }

  return (
    <section className="panel admin-panel review-page-panel">
      <div className="panel-header">
        <div>
          <h2>Feedback de producto</h2>
        </div>
        <button
          aria-label="Actualizar feedback"
          className="secondary-button icon-button"
          disabled={
            productAdmin.isLoadingFeedback ||
            productAdmin.updatingFeedbackId !== null
          }
          onClick={() => void productAdmin.loadFeedback(filters)}
          title="Actualizar feedback"
          type="button"
        >
          <RefreshCw
            aria-hidden
            className={
              productAdmin.isLoadingFeedback ? "spinning-icon" : undefined
            }
            size={17}
          />
        </button>
      </div>

      <div className="review-notice" role="note">
        <ShieldAlert aria-hidden size={19} />
        <p>
          Piloto local no anonimizado: estos registros pueden contener datos
          personales o municipales y todavía no se han enviado a ningún
          sistema central. Revísalos solo en este entorno y no los copies ni
          envíes a servicios externos.
        </p>
      </div>

      {productAdmin.feedbackError ? (
        <p className="error-message">{productAdmin.feedbackError}</p>
      ) : null}
      {productAdmin.feedbackMessage ? (
        <p className="success-message">{productAdmin.feedbackMessage}</p>
      ) : null}

      <ProductFeedbackAdmin
        isLoading={productAdmin.isLoadingFeedback}
        items={hasLoadedCurrentFilters ? productAdmin.feedbackItems : []}
        onNextPage={() =>
          pushSearch(buildProductSearch(status, Math.min(pageCount, page + 1)))
        }
        onPrevPage={() =>
          pushSearch(buildProductSearch(status, Math.max(1, page - 1)))
        }
        onSelectFeedback={(feedbackId) =>
          pushSearch(buildProductSearch(status, page, feedbackId))
        }
        onStatusFilterChange={changeStatus}
        onUpdateFeedback={(feedbackId, updates) =>
          productAdmin.updateFeedback(feedbackId, updates, filters)
        }
        page={page}
        pageSize={productAdmin.adminPageSize}
        selectedFeedbackId={selectedId}
        statusFilter={status}
        total={hasLoadedCurrentFilters ? productAdmin.feedbackTotal : 0}
        updatingFeedbackId={productAdmin.updatingFeedbackId}
      />
    </section>
  );
}

export default function AdminProductoPage() {
  return (
    <Suspense
      fallback={
        <section className="panel">
          <p className="muted">Cargando…</p>
        </section>
      }
    >
      <AdminProductoPageInner />
    </Suspense>
  );
}
