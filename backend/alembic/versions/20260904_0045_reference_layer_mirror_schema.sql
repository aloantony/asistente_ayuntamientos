-- Esquema del espejo cartográfico, tal y como quedaba en la revisión
-- 20260903_0044. Lo usa únicamente el downgrade de 20260904_0045.
--
-- No se escribió a mano: es un volcado (pg_dump --schema-only) de las treinta
-- tablas antes de soltarlas, sin propietarios ni privilegios. Existe para que
-- la cadena de migraciones siga siendo reversible de punta a punta —lo que este
-- repositorio comprueba en test_migrations.py— pese a que los modelos que
-- describían estas tablas ya no están en el árbol. Nadie debe editarlo: si
-- alguna vez hiciera falta tocarlo, es señal de que el espejo ha vuelto, y
-- entonces vuelve con sus modelos y sus propias migraciones.
CREATE TABLE public.organization_reference_layer_settings (
    id integer NOT NULL,
    organization_id integer NOT NULL,
    layer_id integer NOT NULL,
    visible boolean,
    opacity numeric(4,3),
    updated_by_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_org_reference_layer_settings_has_override CHECK (((visible IS NOT NULL) OR (opacity IS NOT NULL))),
    CONSTRAINT ck_org_reference_layer_settings_opacity CHECK (((opacity IS NULL) OR ((opacity >= (0)::numeric) AND (opacity <= (1)::numeric))))
);
CREATE SEQUENCE public.organization_reference_layer_settings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.organization_reference_layer_settings_id_seq OWNED BY public.organization_reference_layer_settings.id;
CREATE TABLE public.reference_catalog_observed_versions (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    source_url text NOT NULL,
    final_url text NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    raw_sha256 character varying(64) NOT NULL,
    size_bytes bigint NOT NULL,
    raw_catalog_json json NOT NULL,
    analysis_json json NOT NULL,
    retrieved_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_catalog_observed_versions_bounds CHECK (((length(source_url) <= 8192) AND (length(final_url) <= 8192) AND (octet_length((raw_catalog_json)::text) <= 16777216) AND (octet_length((analysis_json)::text) <= 1048576))),
    CONSTRAINT ck_reference_catalog_observed_versions_hashes CHECK ((((content_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((raw_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_catalog_observed_versions_provider_nonempty CHECK ((btrim((provider_key)::text) <> ''::text)),
    CONSTRAINT ck_reference_catalog_observed_versions_size CHECK (((size_bytes > 0) AND (size_bytes <= 2097152))),
    CONSTRAINT ck_reference_catalog_observed_versions_urls_https CHECK (((source_url ~~ 'https://%'::text) AND (final_url ~~ 'https://%'::text)))
);
CREATE SEQUENCE public.reference_catalog_observed_versions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_catalog_observed_versions_id_seq OWNED BY public.reference_catalog_observed_versions.id;
CREATE TABLE public.reference_catalog_snapshots (
    id integer NOT NULL,
    provider_key character varying(64) NOT NULL,
    source_url text NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    definition_sha256 character varying(64) NOT NULL,
    raw_catalog_json json NOT NULL,
    normalized_definition_json json NOT NULL,
    retrieved_at timestamp with time zone NOT NULL,
    service_count integer NOT NULL,
    group_count integer NOT NULL,
    layer_count integer NOT NULL,
    unresolved_count integer DEFAULT 0 NOT NULL,
    status character varying(20) NOT NULL,
    is_current boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_catalog_snapshots_counts CHECK (((service_count >= 0) AND (group_count >= 0) AND (layer_count >= 0) AND (unresolved_count >= 0))),
    CONSTRAINT ck_reference_catalog_snapshots_current_applied CHECK (((NOT is_current) OR ((status)::text = 'applied'::text))),
    CONSTRAINT ck_reference_catalog_snapshots_definition_sha256 CHECK (((definition_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_reference_catalog_snapshots_provider_nonempty CHECK ((btrim((provider_key)::text) <> ''::text)),
    CONSTRAINT ck_reference_catalog_snapshots_sha256 CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_reference_catalog_snapshots_source_https CHECK ((source_url ~~ 'https://%'::text)),
    CONSTRAINT ck_reference_catalog_snapshots_status CHECK (((status)::text = ANY ((ARRAY['validated'::character varying, 'applied'::character varying, 'rejected'::character varying])::text[])))
);
CREATE SEQUENCE public.reference_catalog_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_catalog_snapshots_id_seq OWNED BY public.reference_catalog_snapshots.id;
CREATE TABLE public.reference_catalog_update_checks (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    trigger_kind character varying(20) NOT NULL,
    source_url text NOT NULL,
    baseline_snapshot_id integer,
    observed_version_id bigint,
    status character varying(24) NOT NULL,
    checked_at timestamp with time zone NOT NULL,
    next_check_at timestamp with time zone NOT NULL,
    duration_ms integer DEFAULT 0 NOT NULL,
    request_etag text,
    request_last_modified text,
    http_status smallint,
    not_modified boolean DEFAULT false NOT NULL,
    response_final_url text,
    response_etag text,
    response_last_modified text,
    response_size_bytes bigint DEFAULT 0 NOT NULL,
    response_raw_sha256 character varying(64),
    response_redirect_chain_json json DEFAULT '[]'::json NOT NULL,
    error_code character varying(64),
    error_message text,
    error_retryable boolean,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_catalog_update_checks_bounds CHECK (((length((idempotency_key)::text) <= 128) AND (length(source_url) <= 8192) AND ((response_final_url IS NULL) OR (length(response_final_url) <= 8192)) AND ((request_etag IS NULL) OR (length(request_etag) <= 4096)) AND ((request_last_modified IS NULL) OR (length(request_last_modified) <= 4096)) AND ((response_etag IS NULL) OR (length(response_etag) <= 4096)) AND ((response_last_modified IS NULL) OR (length(response_last_modified) <= 4096)) AND ((error_message IS NULL) OR (length(error_message) <= 4096)) AND (octet_length((response_redirect_chain_json)::text) <= 65536))),
    CONSTRAINT ck_reference_catalog_update_checks_hash CHECK (((response_raw_sha256 IS NULL) OR ((response_raw_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_catalog_update_checks_identity_nonempty CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((idempotency_key)::text) <> ''::text))),
    CONSTRAINT ck_reference_catalog_update_checks_measurements CHECK (((response_size_bytes >= 0) AND (response_size_bytes <= 2097152) AND (duration_ms >= 0) AND (next_check_at > checked_at))),
    CONSTRAINT ck_reference_catalog_update_checks_not_modified CHECK (((not_modified AND (http_status = 304) AND (response_size_bytes = 0) AND (response_raw_sha256 IS NULL)) OR ((NOT not_modified) AND ((http_status IS NULL) OR (http_status <> 304))))),
    CONSTRAINT ck_reference_catalog_update_checks_result_shape CHECK (((((status)::text = ANY ((ARRAY['unchanged'::character varying, 'update_available'::character varying])::text[])) AND (observed_version_id IS NOT NULL) AND (http_status IS NOT NULL) AND (http_status = ANY (ARRAY[200, 304])) AND (error_code IS NULL) AND (error_message IS NULL) AND (error_retryable IS NULL)) OR (((status)::text = 'error'::text) AND (observed_version_id IS NULL) AND (error_code IS NOT NULL) AND (error_message IS NOT NULL) AND (btrim((error_code)::text) <> ''::text) AND (btrim(error_message) <> ''::text) AND (error_retryable IS NOT NULL)))),
    CONSTRAINT ck_reference_catalog_update_checks_status CHECK (((status)::text = ANY ((ARRAY['unchanged'::character varying, 'update_available'::character varying, 'error'::character varying])::text[]))),
    CONSTRAINT ck_reference_catalog_update_checks_trigger_kind CHECK (((trigger_kind)::text = ANY ((ARRAY['scheduled'::character varying, 'manual'::character varying])::text[]))),
    CONSTRAINT ck_reference_catalog_update_checks_urls_https CHECK (((source_url ~~ 'https://%'::text) AND ((response_final_url IS NULL) OR (response_final_url ~~ 'https://%'::text))))
);
CREATE SEQUENCE public.reference_catalog_update_checks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_catalog_update_checks_id_seq OWNED BY public.reference_catalog_update_checks.id;
CREATE TABLE public.reference_delivery_assets (
    id bigint NOT NULL,
    version_id bigint NOT NULL,
    asset_key character varying(255) NOT NULL,
    asset_kind character varying(24) NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    storage_backend character varying(16) NOT NULL,
    storage_key text NOT NULL,
    media_type text NOT NULL,
    sha256 character varying(64) NOT NULL,
    size_bytes bigint,
    metadata_json json DEFAULT '{}'::json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_delivery_assets_content CHECK ((((sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((size_bytes IS NULL) OR (size_bytes >= 0)) AND (((storage_backend)::text = 'postgres'::text) OR (size_bytes IS NOT NULL)))),
    CONSTRAINT ck_reference_delivery_assets_kind CHECK (((asset_kind)::text = ANY ((ARRAY['vector_table'::character varying, 'raster_cog'::character varying, 'tile_archive'::character varying, 'tile_prefix'::character varying, 'style_sld'::character varying, 'style_package'::character varying, 'legend'::character varying, 'metadata'::character varying])::text[]))),
    CONSTRAINT ck_reference_delivery_assets_metadata_bounds CHECK ((octet_length((metadata_json)::text) <= 4194304)),
    CONSTRAINT ck_reference_delivery_assets_required_text CHECK (((btrim((asset_key)::text) <> ''::text) AND (btrim(storage_key) <> ''::text) AND (btrim(media_type) <> ''::text))),
    CONSTRAINT ck_reference_delivery_assets_storage_backend CHECK (((storage_backend)::text = ANY ((ARRAY['postgres'::character varying, 'filesystem'::character varying, 's3'::character varying])::text[])))
);
CREATE SEQUENCE public.reference_delivery_assets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_delivery_assets_id_seq OWNED BY public.reference_delivery_assets.id;
CREATE TABLE public.reference_delivery_attestations (
    id integer NOT NULL,
    provider_key character varying(64) NOT NULL,
    service_id integer NOT NULL,
    catalog_snapshot_id integer NOT NULL,
    catalog_definition_sha256 character varying(64) NOT NULL,
    capabilities_snapshot_id integer NOT NULL,
    license_review_id integer NOT NULL,
    attestation_kind character varying(20) NOT NULL,
    sequence_number integer NOT NULL,
    previous_attestation_id integer,
    previous_attestation_sha256 character varying(64),
    attestation_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_delivery_attestations_chain CHECK ((((sequence_number = 1) AND (previous_attestation_id IS NULL) AND (previous_attestation_sha256 IS NULL)) OR ((sequence_number > 1) AND (previous_attestation_id IS NOT NULL) AND (previous_attestation_sha256 IS NOT NULL)))),
    CONSTRAINT ck_reference_delivery_attestations_hashes CHECK ((((catalog_definition_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((attestation_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((previous_attestation_sha256 IS NULL) OR ((previous_attestation_sha256)::text ~ '^[0-9a-f]{64}$'::text)))),
    CONSTRAINT ck_reference_delivery_attestations_kind CHECK (((attestation_kind)::text = ANY ((ARRAY['delivery'::character varying, 'revocation'::character varying])::text[]))),
    CONSTRAINT ck_reference_delivery_attestations_provider_nonempty CHECK ((btrim((provider_key)::text) <> ''::text))
);
CREATE SEQUENCE public.reference_delivery_attestations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_delivery_attestations_id_seq OWNED BY public.reference_delivery_attestations.id;
CREATE TABLE public.reference_delivery_promotions (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    sequence_number bigint NOT NULL,
    action character varying(20) NOT NULL,
    from_version_id bigint,
    to_version_id bigint,
    run_id bigint,
    actor_id integer,
    reason text NOT NULL,
    previous_event_id bigint,
    previous_event_sha256 character varying(64),
    event_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_delivery_promotions_action CHECK (((action)::text = ANY ((ARRAY['promote'::character varying, 'rollback'::character varying, 'deactivate'::character varying, 'reactivate'::character varying])::text[]))),
    CONSTRAINT ck_reference_delivery_promotions_chain CHECK ((((sequence_number = 1) AND (previous_event_id IS NULL) AND (previous_event_sha256 IS NULL)) OR ((sequence_number > 1) AND (previous_event_id IS NOT NULL) AND (previous_event_sha256 IS NOT NULL)))),
    CONSTRAINT ck_reference_delivery_promotions_changes_version CHECK (((from_version_id IS NULL) OR (to_version_id IS NULL) OR (from_version_id <> to_version_id))),
    CONSTRAINT ck_reference_delivery_promotions_evidence CHECK ((((event_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((previous_event_sha256 IS NULL) OR ((previous_event_sha256)::text ~ '^[0-9a-f]{64}$'::text)) AND (btrim(reason) <> ''::text))),
    CONSTRAINT ck_reference_delivery_promotions_shape CHECK (((((action)::text = 'promote'::text) AND (to_version_id IS NOT NULL)) OR (((action)::text = 'rollback'::text) AND (from_version_id IS NOT NULL) AND (to_version_id IS NOT NULL)) OR (((action)::text = 'deactivate'::text) AND (from_version_id IS NOT NULL) AND (to_version_id IS NULL)) OR (((action)::text = 'reactivate'::text) AND (from_version_id IS NULL) AND (to_version_id IS NOT NULL))))
);
CREATE SEQUENCE public.reference_delivery_promotions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_delivery_promotions_id_seq OWNED BY public.reference_delivery_promotions.id;
CREATE TABLE public.reference_delivery_style_parities (
    id bigint NOT NULL,
    version_id bigint NOT NULL,
    plan_item_id bigint NOT NULL,
    parity_kind character varying(16) NOT NULL,
    verified boolean NOT NULL,
    delivery_asset_id bigint NOT NULL,
    resource_count integer NOT NULL,
    evidence_json json NOT NULL,
    evidence_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_delivery_style_parities_evidence CHECK (((resource_count >= 0) AND ((((parity_kind)::text = 'adapted'::text) AND (resource_count >= 0)) OR (((parity_kind)::text = ANY ((ARRAY['exact'::character varying, 'baked'::character varying])::text[])) AND (resource_count = 0))) AND ((evidence_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND (octet_length((evidence_json)::text) <= 4194304))),
    CONSTRAINT ck_reference_delivery_style_parities_verified CHECK ((((parity_kind)::text = ANY ((ARRAY['exact'::character varying, 'adapted'::character varying, 'baked'::character varying])::text[])) AND verified))
);
CREATE SEQUENCE public.reference_delivery_style_parities_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_delivery_style_parities_id_seq OWNED BY public.reference_delivery_style_parities.id;
CREATE TABLE public.reference_delivery_style_resources (
    delivery_parity_id bigint NOT NULL,
    plan_resource_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.reference_delivery_version_artifacts (
    source_id bigint NOT NULL,
    version_id bigint NOT NULL,
    artifact_id bigint NOT NULL,
    role character varying(20) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_delivery_version_artifacts_role CHECK (((role)::text = ANY ((ARRAY['input'::character varying, 'style'::character varying, 'style_package'::character varying, 'style_resource'::character varying, 'metadata'::character varying])::text[])))
);
CREATE TABLE public.reference_delivery_versions (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    source_id bigint NOT NULL,
    sync_run_id bigint NOT NULL,
    catalog_snapshot_id integer NOT NULL,
    catalog_definition_sha256 character varying(64) NOT NULL,
    sequence_number bigint NOT NULL,
    delivery_kind character varying(16) NOT NULL,
    source_version text,
    content_sha256 character varying(64) NOT NULL,
    manifest_sha256 character varying(64) NOT NULL,
    validation_sha256 character varying(64) NOT NULL,
    reference_at timestamp with time zone,
    crs text NOT NULL,
    bounds_json json NOT NULL,
    feature_count bigint,
    validation_json json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    mirror_authorization_review_id bigint,
    mirror_authorization_review_sha256 character varying(64),
    CONSTRAINT ck_reference_delivery_versions_authorization CHECK ((((mirror_authorization_review_id IS NULL) AND (mirror_authorization_review_sha256 IS NULL)) OR ((mirror_authorization_review_id IS NOT NULL) AND ((mirror_authorization_review_sha256)::text ~ '^[0-9a-f]{64}$'::text)))),
    CONSTRAINT ck_reference_delivery_versions_counts CHECK (((sequence_number > 0) AND ((feature_count IS NULL) OR (feature_count >= 0)))),
    CONSTRAINT ck_reference_delivery_versions_hashes CHECK ((((content_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((manifest_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((validation_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((catalog_definition_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_delivery_versions_kind CHECK (((delivery_kind)::text = ANY ((ARRAY['vector'::character varying, 'raster'::character varying, 'tiles'::character varying])::text[]))),
    CONSTRAINT ck_reference_delivery_versions_required_text CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim(crs) <> ''::text))),
    CONSTRAINT ck_reference_delivery_versions_source_version_bounds CHECK (((source_version IS NULL) OR (length(source_version) <= 2048)))
);
CREATE SEQUENCE public.reference_delivery_versions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_delivery_versions_id_seq OWNED BY public.reference_delivery_versions.id;
CREATE TABLE public.reference_layer_delivery_state (
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    status character varying(20) NOT NULL,
    active_version_id bigint,
    generation bigint NOT NULL,
    last_promotion_id bigint NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_layer_delivery_state_generation CHECK ((generation > 0)),
    CONSTRAINT ck_reference_layer_delivery_state_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'disabled'::character varying])::text[]))),
    CONSTRAINT ck_reference_layer_delivery_state_version CHECK (((((status)::text = 'active'::text) AND (active_version_id IS NOT NULL)) OR (((status)::text = 'disabled'::text) AND (active_version_id IS NULL))))
);
CREATE TABLE public.reference_layer_mirror_strategies (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    catalog_snapshot_id integer NOT NULL,
    catalog_definition_sha256 character varying(64) NOT NULL,
    strategy character varying(20) NOT NULL,
    source_id bigint,
    strategy_reason_code character varying(64) NOT NULL,
    strategy_reason text NOT NULL,
    evidence_json json NOT NULL,
    evidence_sha256 character varying(64) NOT NULL,
    generation bigint NOT NULL,
    validated_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_layer_mirror_strategies_identity CHECK ((((catalog_definition_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((evidence_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND (generation > 0) AND (length(strategy_reason) <= 4096))),
    CONSTRAINT ck_reference_layer_mirror_strategies_reason_code CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((strategy_reason_code)::text) <> ''::text))),
    CONSTRAINT ck_reference_layer_mirror_strategies_source_shape CHECK (((((strategy)::text = ANY ((ARRAY['vector'::character varying, 'raster'::character varying, 'tiles'::character varying])::text[])) AND (source_id IS NOT NULL)) OR (((strategy)::text = ANY ((ARRAY['composition'::character varying, 'blocked'::character varying])::text[])) AND (source_id IS NULL)))),
    CONSTRAINT ck_reference_layer_mirror_strategies_strategy CHECK (((strategy)::text = ANY ((ARRAY['vector'::character varying, 'raster'::character varying, 'tiles'::character varying, 'composition'::character varying, 'blocked'::character varying])::text[])))
);
CREATE SEQUENCE public.reference_layer_mirror_strategies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_layer_mirror_strategies_id_seq OWNED BY public.reference_layer_mirror_strategies.id;
CREATE TABLE public.reference_layer_mirror_strategy_dependencies (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    strategy_id bigint NOT NULL,
    strategy_layer_id integer NOT NULL,
    dependency_layer_id integer NOT NULL,
    dependency_order integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_layer_mirror_strategy_dependencies_shape CHECK (((dependency_order >= 0) AND (dependency_layer_id <> strategy_layer_id)))
);
CREATE SEQUENCE public.reference_layer_mirror_strategy_dependencies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_layer_mirror_strategy_dependencies_id_seq OWNED BY public.reference_layer_mirror_strategy_dependencies.id;
CREATE TABLE public.reference_layer_sources (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    source_key character varying(255) NOT NULL,
    protocol character varying(32) NOT NULL,
    target_kind character varying(16) NOT NULL,
    endpoint_url text,
    remote_name text,
    source_format text,
    sync_strategy character varying(32) NOT NULL,
    config_json json DEFAULT '{}'::json NOT NULL,
    definition_sha256 character varying(64) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    priority smallint DEFAULT '0'::smallint NOT NULL,
    check_interval_seconds integer DEFAULT 86400 NOT NULL,
    full_refresh_interval_seconds integer DEFAULT 2592000 NOT NULL,
    next_check_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_layer_sources_definition_sha256 CHECK (((definition_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_reference_layer_sources_endpoint CHECK (((((protocol)::text = 'local'::text) AND (endpoint_url IS NULL)) OR (((protocol)::text <> 'local'::text) AND (endpoint_url ~~ 'https://%'::text)))),
    CONSTRAINT ck_reference_layer_sources_identity_nonempty CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((source_key)::text) <> ''::text))),
    CONSTRAINT ck_reference_layer_sources_protocol CHECK (((protocol)::text = ANY ((ARRAY['wfs'::character varying, 'ogc_api_features'::character varying, 'wcs'::character varying, 'arcgis_rest'::character varying, 'atom'::character varying, 'download'::character varying, 'wmts'::character varying, 'xyz'::character varying, 'wms_tiles'::character varying, 'local'::character varying])::text[]))),
    CONSTRAINT ck_reference_layer_sources_schedule CHECK (((priority >= 0) AND (check_interval_seconds >= 300) AND (full_refresh_interval_seconds >= check_interval_seconds))),
    CONSTRAINT ck_reference_layer_sources_sync_strategy CHECK (((sync_strategy)::text = ANY ((ARRAY['conditional_get'::character varying, 'full_snapshot'::character varying, 'paged_snapshot'::character varying, 'tile_seed'::character varying, 'manual'::character varying])::text[]))),
    CONSTRAINT ck_reference_layer_sources_target_kind CHECK (((target_kind)::text = ANY ((ARRAY['vector'::character varying, 'raster'::character varying, 'tiles'::character varying])::text[])))
);
CREATE SEQUENCE public.reference_layer_sources_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_layer_sources_id_seq OWNED BY public.reference_layer_sources.id;
CREATE TABLE public.reference_layer_styles (
    id integer NOT NULL,
    last_seen_snapshot_id integer NOT NULL,
    layer_id integer NOT NULL,
    provider_key character varying(64) NOT NULL,
    source_key character varying(255) NOT NULL,
    title character varying(500) NOT NULL,
    description text,
    legend_url text,
    sort_order integer DEFAULT 0 NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    remote_name character varying(255) NOT NULL,
    CONSTRAINT ck_reference_layer_styles_identity_nonempty CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((source_key)::text) <> ''::text) AND (btrim((remote_name)::text) <> ''::text) AND (btrim((title)::text) <> ''::text))),
    CONSTRAINT ck_reference_layer_styles_legend_url CHECK (((legend_url IS NULL) OR (legend_url ~ '^https?://'::text))),
    CONSTRAINT ck_reference_layer_styles_sort_order CHECK ((sort_order >= 0)),
    CONSTRAINT ck_reference_layer_styles_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'degraded'::character varying, 'missing'::character varying, 'disabled'::character varying])::text[])))
);
CREATE SEQUENCE public.reference_layer_styles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_layer_styles_id_seq OWNED BY public.reference_layer_styles.id;
CREATE TABLE public.reference_layers (
    id integer NOT NULL,
    last_seen_snapshot_id integer NOT NULL,
    service_id integer,
    parent_id integer,
    provider_key character varying(64) NOT NULL,
    source_key character varying(255) NOT NULL,
    node_type character varying(20) NOT NULL,
    title character varying(500) NOT NULL,
    description text,
    remote_name character varying(500),
    role character varying(20),
    renderer character varying(30),
    delivery_mode character varying(20),
    style_name character varying(255),
    image_format character varying(100),
    supported_crs_json json,
    bounds_json json,
    options_json json,
    sort_order integer DEFAULT 0 NOT NULL,
    default_visible boolean DEFAULT false NOT NULL,
    default_opacity numeric(4,3) DEFAULT '1'::numeric NOT NULL,
    min_zoom smallint,
    max_zoom smallint,
    min_scale_denominator numeric(18,3),
    max_scale_denominator numeric(18,3),
    queryable boolean DEFAULT false NOT NULL,
    downloadable boolean DEFAULT false NOT NULL,
    legend_url text,
    metadata_url text,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_layers_default_opacity CHECK (((default_opacity >= (0)::numeric) AND (default_opacity <= (1)::numeric))),
    CONSTRAINT ck_reference_layers_delivery_mode CHECK (((delivery_mode IS NULL) OR ((delivery_mode)::text = ANY ((ARRAY['proxy'::character varying, 'mirror'::character varying])::text[])))),
    CONSTRAINT ck_reference_layers_identity_nonempty CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((source_key)::text) <> ''::text) AND (btrim((title)::text) <> ''::text))),
    CONSTRAINT ck_reference_layers_node_shape CHECK (((((node_type)::text = 'group'::text) AND (service_id IS NULL) AND (role IS NULL) AND (renderer IS NULL) AND (delivery_mode IS NULL)) OR (((node_type)::text = 'layer'::text) AND (service_id IS NOT NULL) AND (role IS NOT NULL) AND (renderer IS NOT NULL) AND (delivery_mode IS NOT NULL)))),
    CONSTRAINT ck_reference_layers_node_type CHECK (((node_type)::text = ANY ((ARRAY['group'::character varying, 'layer'::character varying])::text[]))),
    CONSTRAINT ck_reference_layers_renderer CHECK (((renderer IS NULL) OR ((renderer)::text = ANY ((ARRAY['raster_tile'::character varying, 'vector_tile'::character varying])::text[])))),
    CONSTRAINT ck_reference_layers_role CHECK (((role IS NULL) OR ((role)::text = ANY ((ARRAY['base'::character varying, 'overlay'::character varying])::text[])))),
    CONSTRAINT ck_reference_layers_scale_positive CHECK ((((min_scale_denominator IS NULL) OR (min_scale_denominator > (0)::numeric)) AND ((max_scale_denominator IS NULL) OR (max_scale_denominator > (0)::numeric)))),
    CONSTRAINT ck_reference_layers_sort_order CHECK ((sort_order >= 0)),
    CONSTRAINT ck_reference_layers_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'degraded'::character varying, 'missing'::character varying, 'disabled'::character varying])::text[]))),
    CONSTRAINT ck_reference_layers_urls CHECK ((((legend_url IS NULL) OR (legend_url ~ '^https?://'::text)) AND ((metadata_url IS NULL) OR (metadata_url ~ '^https?://'::text)))),
    CONSTRAINT ck_reference_layers_zoom_range CHECK ((((min_zoom IS NULL) OR ((min_zoom >= 0) AND (min_zoom <= 24))) AND ((max_zoom IS NULL) OR ((max_zoom >= 0) AND (max_zoom <= 24))) AND ((min_zoom IS NULL) OR (max_zoom IS NULL) OR (min_zoom <= max_zoom))))
);
CREATE SEQUENCE public.reference_layers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_layers_id_seq OWNED BY public.reference_layers.id;
CREATE TABLE public.reference_license_reviews (
    id integer NOT NULL,
    provider_key character varying(64) NOT NULL,
    service_id integer NOT NULL,
    reviewed_document bytea NOT NULL,
    document_size_bytes integer NOT NULL,
    evidence_sha256 character varying(64) NOT NULL,
    review_sha256 character varying(64) NOT NULL,
    supersedes_review_sha256 character varying(64),
    decision character varying(20) NOT NULL,
    reviewer character varying(255) NOT NULL,
    reviewed_at timestamp with time zone NOT NULL,
    license_name character varying(500) NOT NULL,
    license_url text,
    license_terms text NOT NULL,
    allow_proxy boolean DEFAULT false NOT NULL,
    allow_cache boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_license_reviews_cache_requires_proxy CHECK (((NOT allow_cache) OR allow_proxy)),
    CONSTRAINT ck_reference_license_reviews_decision CHECK (((decision)::text = ANY ((ARRAY['approved'::character varying, 'restricted'::character varying, 'rejected'::character varying])::text[]))),
    CONSTRAINT ck_reference_license_reviews_document_size CHECK ((((document_size_bytes >= 1) AND (document_size_bytes <= 262144)) AND (document_size_bytes = octet_length(reviewed_document)))),
    CONSTRAINT ck_reference_license_reviews_hashes CHECK ((((evidence_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((review_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((supersedes_review_sha256 IS NULL) OR ((supersedes_review_sha256)::text ~ '^[0-9a-f]{64}$'::text)))),
    CONSTRAINT ck_reference_license_reviews_license_url CHECK (((license_url IS NULL) OR (license_url ~~ 'https://%'::text))),
    CONSTRAINT ck_reference_license_reviews_permissions_approved CHECK ((((NOT allow_proxy) AND (NOT allow_cache)) OR ((decision)::text = 'approved'::text))),
    CONSTRAINT ck_reference_license_reviews_required_text CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((reviewer)::text) <> ''::text) AND (btrim((license_name)::text) <> ''::text) AND (btrim(license_terms) <> ''::text)))
);
CREATE SEQUENCE public.reference_license_reviews_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_license_reviews_id_seq OWNED BY public.reference_license_reviews.id;
CREATE TABLE public.reference_mirror_authorization_reviews (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    service_id integer NOT NULL,
    layer_id integer NOT NULL,
    source_id bigint NOT NULL,
    source_definition_sha256 character varying(64) NOT NULL,
    protocol character varying(32) NOT NULL,
    target_kind character varying(16) NOT NULL,
    canonical_origin text NOT NULL,
    allowed_origins_json json NOT NULL,
    reviewed_document bytea NOT NULL,
    document_size_bytes integer NOT NULL,
    document_sha256 character varying(64) NOT NULL,
    review_sha256 character varying(64) NOT NULL,
    supersedes_review_id bigint,
    supersedes_review_sha256 character varying(64),
    decision character varying(20) NOT NULL,
    reviewer character varying(255) NOT NULL,
    reviewed_at timestamp with time zone NOT NULL,
    license_name character varying(500) NOT NULL,
    license_url text NOT NULL,
    license_terms text NOT NULL,
    attribution text,
    allow_metadata_probe boolean DEFAULT false NOT NULL,
    allow_dataset_download boolean DEFAULT false NOT NULL,
    allow_local_storage boolean DEFAULT false NOT NULL,
    allow_local_service boolean DEFAULT false NOT NULL,
    allow_bulk_tile_seed boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_mirror_authorizations_approved_permissions CHECK ((((NOT allow_metadata_probe) AND (NOT allow_dataset_download) AND (NOT allow_local_storage) AND (NOT allow_local_service) AND (NOT allow_bulk_tile_seed)) OR ((decision)::text = 'approved'::text))),
    CONSTRAINT ck_reference_mirror_authorizations_chain_shape CHECK ((((supersedes_review_id IS NULL) AND (supersedes_review_sha256 IS NULL)) OR ((supersedes_review_id IS NOT NULL) AND (supersedes_review_sha256 IS NOT NULL)))),
    CONSTRAINT ck_reference_mirror_authorizations_decision CHECK (((decision)::text = ANY ((ARRAY['approved'::character varying, 'restricted'::character varying, 'rejected'::character varying])::text[]))),
    CONSTRAINT ck_reference_mirror_authorizations_document_size CHECK ((((document_size_bytes >= 1) AND (document_size_bytes <= 262144)) AND (document_size_bytes = octet_length(reviewed_document)))),
    CONSTRAINT ck_reference_mirror_authorizations_hashes CHECK ((((document_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((review_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((source_definition_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((supersedes_review_sha256 IS NULL) OR ((supersedes_review_sha256)::text ~ '^[0-9a-f]{64}$'::text)))),
    CONSTRAINT ck_reference_mirror_authorizations_required_text CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((reviewer)::text) <> ''::text) AND (btrim((license_name)::text) <> ''::text) AND (btrim(license_terms) <> ''::text))),
    CONSTRAINT ck_reference_mirror_authorizations_service_attribution CHECK (((NOT allow_local_service) OR ((attribution IS NOT NULL) AND (btrim(attribution) <> ''::text)))),
    CONSTRAINT ck_reference_mirror_authorizations_service_storage CHECK (((NOT allow_local_service) OR allow_local_storage)),
    CONSTRAINT ck_reference_mirror_authorizations_source_kind CHECK ((((protocol)::text = ANY ((ARRAY['wfs'::character varying, 'ogc_api_features'::character varying, 'wcs'::character varying, 'arcgis_rest'::character varying, 'atom'::character varying, 'download'::character varying, 'wmts'::character varying, 'xyz'::character varying, 'wms_tiles'::character varying, 'local'::character varying])::text[])) AND ((target_kind)::text = ANY ((ARRAY['vector'::character varying, 'raster'::character varying, 'tiles'::character varying])::text[])))),
    CONSTRAINT ck_reference_mirror_authorizations_storage_download CHECK (((NOT allow_local_storage) OR allow_dataset_download)),
    CONSTRAINT ck_reference_mirror_authorizations_tiles_seed CHECK ((((target_kind)::text <> 'tiles'::text) OR (NOT allow_local_service) OR allow_bulk_tile_seed)),
    CONSTRAINT ck_reference_mirror_authorizations_urls CHECK (((canonical_origin ~~ 'https://%'::text) AND (license_url ~~ 'https://%'::text) AND (json_typeof(allowed_origins_json) = 'array'::text) AND ((json_array_length(allowed_origins_json) >= 1) AND (json_array_length(allowed_origins_json) <= 32))))
);
CREATE SEQUENCE public.reference_mirror_authorization_reviews_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_mirror_authorization_reviews_id_seq OWNED BY public.reference_mirror_authorization_reviews.id;
CREATE TABLE public.reference_services (
    id integer NOT NULL,
    last_seen_snapshot_id integer NOT NULL,
    provider_key character varying(64) NOT NULL,
    source_key character varying(255) NOT NULL,
    title character varying(500) NOT NULL,
    upstream_protocol character varying(30) NOT NULL,
    base_url text NOT NULL,
    capabilities_url text,
    version character varying(30),
    default_crs character varying(64),
    default_format character varying(100),
    attribution text,
    license_name character varying(255),
    license_url text,
    license_status character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    cache_policy character varying(20) DEFAULT 'none'::character varying NOT NULL,
    capabilities_sha256 character varying(64),
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_services_cache_policy CHECK (((cache_policy)::text = ANY ((ARRAY['none'::character varying, 'on_demand'::character varying, 'mirror'::character varying])::text[]))),
    CONSTRAINT ck_reference_services_capabilities_sha256 CHECK (((capabilities_sha256 IS NULL) OR ((capabilities_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_services_identity_nonempty CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((source_key)::text) <> ''::text) AND (btrim((title)::text) <> ''::text))),
    CONSTRAINT ck_reference_services_license_status CHECK (((license_status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'restricted'::character varying])::text[]))),
    CONSTRAINT ck_reference_services_protocol CHECK (((upstream_protocol)::text = ANY ((ARRAY['wms'::character varying, 'wfs'::character varying, 'wmts'::character varying, 'xyz'::character varying, 'arcgis_rest'::character varying, 'local'::character varying])::text[]))),
    CONSTRAINT ck_reference_services_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'degraded'::character varying, 'missing'::character varying, 'disabled'::character varying])::text[]))),
    CONSTRAINT ck_reference_services_urls CHECK (((base_url ~ '^https?://'::text) AND ((capabilities_url IS NULL) OR (capabilities_url ~ '^https?://'::text)) AND ((license_url IS NULL) OR (license_url ~ '^https?://'::text))))
);
CREATE SEQUENCE public.reference_services_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_services_id_seq OWNED BY public.reference_services.id;
CREATE TABLE public.reference_source_artifacts (
    id bigint NOT NULL,
    source_id bigint NOT NULL,
    artifact_kind character varying(24) NOT NULL,
    source_url text,
    final_url text,
    source_version text,
    upstream_etag text,
    upstream_last_modified timestamp with time zone,
    media_type text NOT NULL,
    storage_backend character varying(16) NOT NULL,
    storage_key text NOT NULL,
    size_bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    metadata_json json DEFAULT '{}'::json NOT NULL,
    retrieved_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_source_artifacts_content CHECK (((size_bytes > 0) AND ((sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_source_artifacts_kind CHECK (((artifact_kind)::text = ANY ((ARRAY['capabilities'::character varying, 'manifest'::character varying, 'dataset'::character varying, 'style'::character varying, 'style_package'::character varying, 'style_resource'::character varying, 'metadata'::character varying, 'tile_archive'::character varying])::text[]))),
    CONSTRAINT ck_reference_source_artifacts_metadata_bounds CHECK ((((source_version IS NULL) OR (length(source_version) <= 2048)) AND ((upstream_etag IS NULL) OR (length(upstream_etag) <= 4096)) AND (octet_length((metadata_json)::text) <= 4194304))),
    CONSTRAINT ck_reference_source_artifacts_required_text CHECK (((btrim(media_type) <> ''::text) AND (btrim(storage_key) <> ''::text))),
    CONSTRAINT ck_reference_source_artifacts_storage_backend CHECK (((storage_backend)::text = ANY ((ARRAY['filesystem'::character varying, 's3'::character varying])::text[]))),
    CONSTRAINT ck_reference_source_artifacts_urls CHECK ((((source_url IS NULL) OR (source_url ~~ 'https://%'::text)) AND ((final_url IS NULL) OR (final_url ~~ 'https://%'::text))))
);
CREATE SEQUENCE public.reference_source_artifacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_source_artifacts_id_seq OWNED BY public.reference_source_artifacts.id;
CREATE TABLE public.reference_style_observed_versions (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    source_id bigint NOT NULL,
    source_definition_sha256 character varying(64) NOT NULL,
    profile character varying(128) NOT NULL,
    source_url text NOT NULL,
    final_url text NOT NULL,
    raw_sha256 character varying(64) NOT NULL,
    semantic_sha256 character varying(64) NOT NULL,
    size_bytes integer NOT NULL,
    storage_backend character varying(32) NOT NULL,
    storage_key text NOT NULL,
    semantic_summary_json json NOT NULL,
    retrieved_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_style_observed_bounds CHECK (((length((profile)::text) <= 128) AND (length(source_url) <= 8192) AND (length(final_url) <= 8192) AND (length(storage_key) <= 256) AND (json_typeof(semantic_summary_json) = 'object'::text) AND (octet_length((semantic_summary_json)::text) <= 65536))),
    CONSTRAINT ck_reference_style_observed_hashes CHECK ((((source_definition_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((raw_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((semantic_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_style_observed_identity CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((profile)::text) <> ''::text))),
    CONSTRAINT ck_reference_style_observed_size CHECK (((size_bytes >= 1) AND (size_bytes <= 65536))),
    CONSTRAINT ck_reference_style_observed_storage CHECK ((((storage_backend)::text = 'filesystem'::text) AND (storage_key ~ '^blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_style_observed_url CHECK (((source_url ~~ 'https://%'::text) AND (final_url = source_url)))
);
CREATE SEQUENCE public.reference_style_observed_versions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_style_observed_versions_id_seq OWNED BY public.reference_style_observed_versions.id;
CREATE TABLE public.reference_style_parity_plan_items (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    source_id bigint NOT NULL,
    style_id integer,
    style_source_key character varying(255) NOT NULL,
    remote_name character varying(255) NOT NULL,
    is_default boolean NOT NULL,
    parity_kind character varying(16) NOT NULL,
    verified boolean NOT NULL,
    source_style_artifact_id bigint,
    source_package_artifact_id bigint,
    resource_count integer NOT NULL,
    reason_code character varying(64),
    evidence_json json NOT NULL,
    evidence_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_style_parity_plan_items_artifacts CHECK (((((parity_kind)::text = 'exact'::text) AND (source_style_artifact_id IS NOT NULL) AND (source_package_artifact_id IS NULL) AND (resource_count = 0)) OR (((parity_kind)::text = 'adapted'::text) AND (source_style_artifact_id IS NOT NULL) AND (source_package_artifact_id IS NOT NULL) AND (resource_count >= 0)) OR (((parity_kind)::text = ANY ((ARRAY['baked'::character varying, 'missing'::character varying])::text[])) AND (source_style_artifact_id IS NULL) AND (source_package_artifact_id IS NULL) AND (resource_count = 0)))),
    CONSTRAINT ck_reference_style_parity_plan_items_evidence CHECK (((btrim((style_source_key)::text) <> ''::text) AND (btrim((remote_name)::text) <> ''::text) AND ((evidence_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND (resource_count >= 0) AND (octet_length((evidence_json)::text) <= 4194304))),
    CONSTRAINT ck_reference_style_parity_plan_items_kind CHECK (((parity_kind)::text = ANY ((ARRAY['exact'::character varying, 'adapted'::character varying, 'baked'::character varying, 'missing'::character varying])::text[]))),
    CONSTRAINT ck_reference_style_parity_plan_items_verification CHECK (((((parity_kind)::text = 'missing'::text) AND (NOT verified) AND (reason_code IS NOT NULL) AND (btrim((reason_code)::text) <> ''::text)) OR (((parity_kind)::text <> 'missing'::text) AND verified AND (reason_code IS NULL))))
);
CREATE SEQUENCE public.reference_style_parity_plan_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_style_parity_plan_items_id_seq OWNED BY public.reference_style_parity_plan_items.id;
CREATE TABLE public.reference_style_parity_plan_resources (
    id bigint NOT NULL,
    plan_item_id bigint NOT NULL,
    source_id bigint NOT NULL,
    artifact_id bigint NOT NULL,
    original_href text NOT NULL,
    resolved_url text NOT NULL,
    local_path character varying(96) NOT NULL,
    media_type character varying(255) NOT NULL,
    sha256 character varying(64) NOT NULL,
    evidence_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_style_parity_plan_resources_evidence CHECK (((resolved_url ~~ 'https://%'::text) AND (btrim(original_href) <> ''::text) AND ((local_path)::text ~ '^resources/[0-9a-f]{64}\.[a-z0-9]{1,8}$'::text) AND ((sha256)::text ~ '^[0-9a-f]{64}$'::text) AND (btrim((media_type)::text) <> ''::text) AND ((evidence_sha256)::text ~ '^[0-9a-f]{64}$'::text)))
);
CREATE SEQUENCE public.reference_style_parity_plan_resources_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_style_parity_plan_resources_id_seq OWNED BY public.reference_style_parity_plan_resources.id;
CREATE TABLE public.reference_style_parity_plans (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    catalog_snapshot_id integer NOT NULL,
    catalog_definition_sha256 character varying(64) NOT NULL,
    source_id bigint NOT NULL,
    sync_run_id bigint NOT NULL,
    delivery_kind character varying(16) NOT NULL,
    required_style_count integer NOT NULL,
    missing_style_count integer NOT NULL,
    complete boolean NOT NULL,
    evidence_json json NOT NULL,
    evidence_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_style_parity_plans_counts CHECK (((required_style_count > 0) AND (missing_style_count >= 0) AND (missing_style_count <= required_style_count) AND (complete = (missing_style_count = 0)))),
    CONSTRAINT ck_reference_style_parity_plans_evidence CHECK (((btrim((provider_key)::text) <> ''::text) AND (octet_length((evidence_json)::text) <= 4194304))),
    CONSTRAINT ck_reference_style_parity_plans_hashes CHECK ((((catalog_definition_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((evidence_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_style_parity_plans_kind CHECK (((delivery_kind)::text = ANY ((ARRAY['vector'::character varying, 'raster'::character varying, 'tiles'::character varying])::text[])))
);
CREATE SEQUENCE public.reference_style_parity_plans_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_style_parity_plans_id_seq OWNED BY public.reference_style_parity_plans.id;
CREATE TABLE public.reference_style_update_checks (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    source_id bigint NOT NULL,
    source_definition_sha256 character varying(64) NOT NULL,
    profile character varying(128) NOT NULL,
    idempotency_key character varying(160) NOT NULL,
    trigger_kind character varying(20) NOT NULL,
    source_url text NOT NULL,
    baseline_raw_sha256 character varying(64) NOT NULL,
    baseline_semantic_sha256 character varying(64) NOT NULL,
    observed_version_id bigint,
    authorization_review_id bigint,
    authorization_review_sha256 character varying(64),
    status character varying(32) NOT NULL,
    checked_at timestamp with time zone NOT NULL,
    next_check_at timestamp with time zone NOT NULL,
    duration_ms integer DEFAULT 0 NOT NULL,
    request_etag text,
    request_last_modified text,
    http_status smallint,
    not_modified boolean DEFAULT false NOT NULL,
    response_final_url text,
    response_etag text,
    response_last_modified text,
    response_size_bytes bigint DEFAULT 0 NOT NULL,
    response_raw_sha256 character varying(64),
    response_redirect_chain_json json DEFAULT '[]'::json NOT NULL,
    error_code character varying(64),
    error_message text,
    error_retryable boolean,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_style_checks_authorization CHECK ((((authorization_review_id IS NULL) AND (authorization_review_sha256 IS NULL)) OR ((authorization_review_id IS NOT NULL) AND (authorization_review_sha256 IS NOT NULL)))),
    CONSTRAINT ck_reference_style_checks_bounds CHECK (((length((profile)::text) <= 128) AND (length((idempotency_key)::text) <= 160) AND (length(source_url) <= 8192) AND ((request_etag IS NULL) OR (length(request_etag) <= 4096)) AND ((request_last_modified IS NULL) OR (length(request_last_modified) <= 4096)) AND ((response_etag IS NULL) OR (length(response_etag) <= 4096)) AND ((response_last_modified IS NULL) OR (length(response_last_modified) <= 4096)) AND ((error_message IS NULL) OR (length(error_message) <= 4096)) AND (json_typeof(response_redirect_chain_json) = 'array'::text) AND (octet_length((response_redirect_chain_json)::text) <= 65536))),
    CONSTRAINT ck_reference_style_checks_candidate CHECK (((((status)::text = 'style_review_required'::text) AND (observed_version_id IS NOT NULL)) OR ((status)::text <> 'style_review_required'::text))),
    CONSTRAINT ck_reference_style_checks_hashes CHECK ((((source_definition_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((baseline_raw_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((baseline_semantic_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((authorization_review_sha256 IS NULL) OR ((authorization_review_sha256)::text ~ '^[0-9a-f]{64}$'::text)) AND ((response_raw_sha256 IS NULL) OR ((response_raw_sha256)::text ~ '^[0-9a-f]{64}$'::text)))),
    CONSTRAINT ck_reference_style_checks_http_200 CHECK ((((status)::text = 'error'::text) OR ((http_status = 200) AND (response_size_bytes > 0) AND (response_raw_sha256 IS NOT NULL)) OR ((http_status IS NULL) OR (http_status <> 200)))),
    CONSTRAINT ck_reference_style_checks_identity CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((profile)::text) <> ''::text) AND (btrim((idempotency_key)::text) <> ''::text))),
    CONSTRAINT ck_reference_style_checks_measurements CHECK (((duration_ms >= 0) AND (next_check_at > checked_at) AND ((response_size_bytes >= 0) AND (response_size_bytes <= 65536)))),
    CONSTRAINT ck_reference_style_checks_not_modified CHECK (((not_modified AND (http_status = 304) AND (response_size_bytes = 0) AND (response_raw_sha256 IS NULL)) OR ((NOT not_modified) AND ((http_status IS NULL) OR (http_status <> 304) OR ((status)::text = 'error'::text))))),
    CONSTRAINT ck_reference_style_checks_result CHECK (((((status)::text = ANY ((ARRAY['unchanged'::character varying, 'style_review_required'::character varying])::text[])) AND (authorization_review_id IS NOT NULL) AND (http_status = ANY (ARRAY[200, 304])) AND (error_code IS NULL) AND (error_message IS NULL) AND (error_retryable IS NULL)) OR (((status)::text = 'error'::text) AND (observed_version_id IS NULL) AND (error_code IS NOT NULL) AND (btrim((error_code)::text) <> ''::text) AND (error_message IS NOT NULL) AND (btrim(error_message) <> ''::text) AND (error_retryable IS NOT NULL)))),
    CONSTRAINT ck_reference_style_checks_status CHECK ((((trigger_kind)::text = ANY ((ARRAY['scheduled'::character varying, 'manual'::character varying])::text[])) AND ((status)::text = ANY ((ARRAY['unchanged'::character varying, 'style_review_required'::character varying, 'error'::character varying])::text[])))),
    CONSTRAINT ck_reference_style_checks_urls CHECK (((source_url ~~ 'https://%'::text) AND ((response_final_url IS NULL) OR (response_final_url = source_url))))
);
CREATE SEQUENCE public.reference_style_update_checks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_style_update_checks_id_seq OWNED BY public.reference_style_update_checks.id;
CREATE TABLE public.reference_style_update_reviews (
    id bigint NOT NULL,
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    source_id bigint NOT NULL,
    source_definition_sha256 character varying(64) NOT NULL,
    profile character varying(128) NOT NULL,
    source_url text NOT NULL,
    baseline_raw_sha256 character varying(64) NOT NULL,
    baseline_semantic_sha256 character varying(64) NOT NULL,
    observed_version_id bigint NOT NULL,
    observed_raw_sha256 character varying(64) NOT NULL,
    observed_semantic_sha256 character varying(64) NOT NULL,
    decision character varying(32) NOT NULL,
    reviewer character varying(255) NOT NULL,
    reviewed_at timestamp with time zone NOT NULL,
    rationale text NOT NULL,
    reviewed_document bytea NOT NULL,
    document_size_bytes integer NOT NULL,
    document_sha256 character varying(64) NOT NULL,
    review_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_style_reviews_bounds CHECK (((length((profile)::text) <= 128) AND (length(source_url) <= 8192) AND (length((reviewer)::text) <= 255) AND (length(rationale) <= 4096))),
    CONSTRAINT ck_reference_style_reviews_decision CHECK (((source_url ~~ 'https://%'::text) AND ((decision)::text = ANY ((ARRAY['retain_vendored'::character varying, 'vendor_update_required'::character varying])::text[])))),
    CONSTRAINT ck_reference_style_reviews_document CHECK ((((document_size_bytes >= 1) AND (document_size_bytes <= 65536)) AND (document_size_bytes = octet_length(reviewed_document)))),
    CONSTRAINT ck_reference_style_reviews_hashes CHECK ((((source_definition_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((baseline_raw_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((baseline_semantic_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((observed_raw_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((observed_semantic_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((document_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((review_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_style_reviews_identity CHECK (((btrim((provider_key)::text) <> ''::text) AND (btrim((profile)::text) <> ''::text) AND (btrim((reviewer)::text) <> ''::text) AND (btrim(rationale) <> ''::text)))
);
CREATE SEQUENCE public.reference_style_update_reviews_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_style_update_reviews_id_seq OWNED BY public.reference_style_update_reviews.id;
CREATE TABLE public.reference_sync_run_artifacts (
    source_id bigint NOT NULL,
    run_id bigint NOT NULL,
    artifact_id bigint NOT NULL,
    role character varying(20) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_sync_run_artifacts_role CHECK (((role)::text = ANY ((ARRAY['observation'::character varying, 'input'::character varying, 'style'::character varying, 'style_package'::character varying, 'style_resource'::character varying, 'metadata'::character varying])::text[])))
);
CREATE TABLE public.reference_sync_runs (
    id bigint NOT NULL,
    source_id bigint NOT NULL,
    requested_by_id integer,
    source_definition_json json NOT NULL,
    source_definition_sha256 character varying(64) NOT NULL,
    trigger_kind character varying(20) NOT NULL,
    check_mode character varying(16) NOT NULL,
    status character varying(20) NOT NULL,
    attempt_no integer DEFAULT 1 NOT NULL,
    expected_active_generation bigint DEFAULT '0'::bigint NOT NULL,
    lease_token character varying(64),
    lease_expires_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    queued_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    observed_etag text,
    observed_last_modified timestamp with time zone,
    observed_version text,
    observed_manifest_sha256 character varying(64),
    error_code character varying(64),
    error_summary text,
    stats_json json DEFAULT '{}'::json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    provider_key character varying(64) NOT NULL,
    layer_id integer NOT NULL,
    parent_run_id bigint,
    fallback_depth integer DEFAULT 0 NOT NULL,
    mirror_authorization_review_id bigint,
    mirror_authorization_review_sha256 character varying(64),
    CONSTRAINT ck_reference_sync_runs_authorization CHECK ((((mirror_authorization_review_id IS NULL) AND (mirror_authorization_review_sha256 IS NULL)) OR ((mirror_authorization_review_id IS NOT NULL) AND ((mirror_authorization_review_sha256)::text ~ '^[0-9a-f]{64}$'::text)))),
    CONSTRAINT ck_reference_sync_runs_check_mode CHECK (((check_mode)::text = ANY ((ARRAY['conditional'::character varying, 'full'::character varying])::text[]))),
    CONSTRAINT ck_reference_sync_runs_failure_error CHECK ((((status)::text <> ALL ((ARRAY['rejected'::character varying, 'failed'::character varying])::text[])) OR ((error_code IS NOT NULL) AND (btrim((error_code)::text) <> ''::text)))),
    CONSTRAINT ck_reference_sync_runs_fallback_chain CHECK ((((parent_run_id IS NULL) AND (fallback_depth = 0)) OR ((parent_run_id IS NOT NULL) AND (fallback_depth > 0) AND ((trigger_kind)::text = 'retry'::text)))),
    CONSTRAINT ck_reference_sync_runs_identity CHECK ((((source_definition_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND (attempt_no > 0) AND (expected_active_generation >= 0) AND (fallback_depth >= 0))),
    CONSTRAINT ck_reference_sync_runs_lifecycle CHECK (((((status)::text = 'queued'::text) AND (started_at IS NULL) AND (finished_at IS NULL) AND (lease_token IS NULL) AND (lease_expires_at IS NULL) AND (heartbeat_at IS NULL)) OR (((status)::text = 'running'::text) AND (started_at IS NOT NULL) AND (finished_at IS NULL) AND (lease_token IS NOT NULL) AND (lease_expires_at IS NOT NULL) AND (heartbeat_at IS NOT NULL)) OR (((status)::text = ANY ((ARRAY['unchanged'::character varying, 'succeeded'::character varying, 'rejected'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])) AND (finished_at IS NOT NULL) AND (lease_token IS NULL) AND (lease_expires_at IS NULL)))),
    CONSTRAINT ck_reference_sync_runs_manifest_sha256 CHECK (((observed_manifest_sha256 IS NULL) OR ((observed_manifest_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_sync_runs_observed_bounds CHECK ((((observed_etag IS NULL) OR (length(observed_etag) <= 4096)) AND ((observed_version IS NULL) OR (length(observed_version) <= 2048)) AND ((error_summary IS NULL) OR (length(error_summary) <= 4096)) AND (octet_length((stats_json)::text) <= 1048576))),
    CONSTRAINT ck_reference_sync_runs_parent_not_self CHECK (((parent_run_id IS NULL) OR (parent_run_id <> id))),
    CONSTRAINT ck_reference_sync_runs_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'unchanged'::character varying, 'succeeded'::character varying, 'rejected'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT ck_reference_sync_runs_trigger_kind CHECK (((trigger_kind)::text = ANY ((ARRAY['scheduled'::character varying, 'manual'::character varying, 'retry'::character varying, 'backfill'::character varying])::text[])))
);
CREATE SEQUENCE public.reference_sync_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_sync_runs_id_seq OWNED BY public.reference_sync_runs.id;
CREATE TABLE public.reference_wms_capabilities_snapshots (
    id integer NOT NULL,
    provider_key character varying(64) NOT NULL,
    service_id integer NOT NULL,
    raw_xml bytea NOT NULL,
    raw_size_bytes integer NOT NULL,
    raw_sha256 character varying(64) NOT NULL,
    normalized_sha256 character varying(64) NOT NULL,
    normalization_version character varying(64) NOT NULL,
    wms_version character varying(16) NOT NULL,
    get_map_endpoint text NOT NULL,
    get_legend_endpoint text,
    get_feature_info_endpoint text,
    get_map_formats_json json NOT NULL,
    get_legend_formats_json json NOT NULL,
    get_feature_info_formats_json json NOT NULL,
    layer_manifest_json json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reference_wms_capabilities_endpoints CHECK (((get_map_endpoint ~~ 'https://%'::text) AND ((get_legend_endpoint IS NULL) OR (get_legend_endpoint ~~ 'https://%'::text)) AND ((get_feature_info_endpoint IS NULL) OR (get_feature_info_endpoint ~~ 'https://%'::text)))),
    CONSTRAINT ck_reference_wms_capabilities_hashes CHECK ((((raw_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((normalized_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_reference_wms_capabilities_normalization CHECK (((normalization_version)::text = 'siur-wms-capabilities-v1'::text)),
    CONSTRAINT ck_reference_wms_capabilities_provider_nonempty CHECK ((btrim((provider_key)::text) <> ''::text)),
    CONSTRAINT ck_reference_wms_capabilities_raw_size CHECK ((((raw_size_bytes >= 1) AND (raw_size_bytes <= 4194304)) AND (raw_size_bytes = octet_length(raw_xml)))),
    CONSTRAINT ck_reference_wms_capabilities_version CHECK (((wms_version)::text = ANY ((ARRAY['1.1.1'::character varying, '1.3.0'::character varying])::text[])))
);
CREATE SEQUENCE public.reference_wms_capabilities_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.reference_wms_capabilities_snapshots_id_seq OWNED BY public.reference_wms_capabilities_snapshots.id;
ALTER TABLE ONLY public.organization_reference_layer_settings ALTER COLUMN id SET DEFAULT nextval('public.organization_reference_layer_settings_id_seq'::regclass);
ALTER TABLE ONLY public.reference_catalog_observed_versions ALTER COLUMN id SET DEFAULT nextval('public.reference_catalog_observed_versions_id_seq'::regclass);
ALTER TABLE ONLY public.reference_catalog_snapshots ALTER COLUMN id SET DEFAULT nextval('public.reference_catalog_snapshots_id_seq'::regclass);
ALTER TABLE ONLY public.reference_catalog_update_checks ALTER COLUMN id SET DEFAULT nextval('public.reference_catalog_update_checks_id_seq'::regclass);
ALTER TABLE ONLY public.reference_delivery_assets ALTER COLUMN id SET DEFAULT nextval('public.reference_delivery_assets_id_seq'::regclass);
ALTER TABLE ONLY public.reference_delivery_attestations ALTER COLUMN id SET DEFAULT nextval('public.reference_delivery_attestations_id_seq'::regclass);
ALTER TABLE ONLY public.reference_delivery_promotions ALTER COLUMN id SET DEFAULT nextval('public.reference_delivery_promotions_id_seq'::regclass);
ALTER TABLE ONLY public.reference_delivery_style_parities ALTER COLUMN id SET DEFAULT nextval('public.reference_delivery_style_parities_id_seq'::regclass);
ALTER TABLE ONLY public.reference_delivery_versions ALTER COLUMN id SET DEFAULT nextval('public.reference_delivery_versions_id_seq'::regclass);
ALTER TABLE ONLY public.reference_layer_mirror_strategies ALTER COLUMN id SET DEFAULT nextval('public.reference_layer_mirror_strategies_id_seq'::regclass);
ALTER TABLE ONLY public.reference_layer_mirror_strategy_dependencies ALTER COLUMN id SET DEFAULT nextval('public.reference_layer_mirror_strategy_dependencies_id_seq'::regclass);
ALTER TABLE ONLY public.reference_layer_sources ALTER COLUMN id SET DEFAULT nextval('public.reference_layer_sources_id_seq'::regclass);
ALTER TABLE ONLY public.reference_layer_styles ALTER COLUMN id SET DEFAULT nextval('public.reference_layer_styles_id_seq'::regclass);
ALTER TABLE ONLY public.reference_layers ALTER COLUMN id SET DEFAULT nextval('public.reference_layers_id_seq'::regclass);
ALTER TABLE ONLY public.reference_license_reviews ALTER COLUMN id SET DEFAULT nextval('public.reference_license_reviews_id_seq'::regclass);
ALTER TABLE ONLY public.reference_mirror_authorization_reviews ALTER COLUMN id SET DEFAULT nextval('public.reference_mirror_authorization_reviews_id_seq'::regclass);
ALTER TABLE ONLY public.reference_services ALTER COLUMN id SET DEFAULT nextval('public.reference_services_id_seq'::regclass);
ALTER TABLE ONLY public.reference_source_artifacts ALTER COLUMN id SET DEFAULT nextval('public.reference_source_artifacts_id_seq'::regclass);
ALTER TABLE ONLY public.reference_style_observed_versions ALTER COLUMN id SET DEFAULT nextval('public.reference_style_observed_versions_id_seq'::regclass);
ALTER TABLE ONLY public.reference_style_parity_plan_items ALTER COLUMN id SET DEFAULT nextval('public.reference_style_parity_plan_items_id_seq'::regclass);
ALTER TABLE ONLY public.reference_style_parity_plan_resources ALTER COLUMN id SET DEFAULT nextval('public.reference_style_parity_plan_resources_id_seq'::regclass);
ALTER TABLE ONLY public.reference_style_parity_plans ALTER COLUMN id SET DEFAULT nextval('public.reference_style_parity_plans_id_seq'::regclass);
ALTER TABLE ONLY public.reference_style_update_checks ALTER COLUMN id SET DEFAULT nextval('public.reference_style_update_checks_id_seq'::regclass);
ALTER TABLE ONLY public.reference_style_update_reviews ALTER COLUMN id SET DEFAULT nextval('public.reference_style_update_reviews_id_seq'::regclass);
ALTER TABLE ONLY public.reference_sync_runs ALTER COLUMN id SET DEFAULT nextval('public.reference_sync_runs_id_seq'::regclass);
ALTER TABLE ONLY public.reference_wms_capabilities_snapshots ALTER COLUMN id SET DEFAULT nextval('public.reference_wms_capabilities_snapshots_id_seq'::regclass);
ALTER TABLE ONLY public.organization_reference_layer_settings
    ADD CONSTRAINT organization_reference_layer_settings_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_catalog_observed_versions
    ADD CONSTRAINT pk_reference_catalog_observed_versions PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_catalog_update_checks
    ADD CONSTRAINT pk_reference_catalog_update_checks PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_delivery_style_parities
    ADD CONSTRAINT pk_reference_delivery_style_parities PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_delivery_style_resources
    ADD CONSTRAINT pk_reference_delivery_style_resources PRIMARY KEY (delivery_parity_id, plan_resource_id);
ALTER TABLE ONLY public.reference_layer_mirror_strategies
    ADD CONSTRAINT pk_reference_layer_mirror_strategies PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_layer_mirror_strategy_dependencies
    ADD CONSTRAINT pk_reference_layer_mirror_strategy_dependencies PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_mirror_authorization_reviews
    ADD CONSTRAINT pk_reference_mirror_authorization_reviews PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_style_observed_versions
    ADD CONSTRAINT pk_reference_style_observed_versions PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_style_parity_plan_items
    ADD CONSTRAINT pk_reference_style_parity_plan_items PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_style_parity_plan_resources
    ADD CONSTRAINT pk_reference_style_parity_plan_resources PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_style_parity_plans
    ADD CONSTRAINT pk_reference_style_parity_plans PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_style_update_checks
    ADD CONSTRAINT pk_reference_style_update_checks PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_style_update_reviews
    ADD CONSTRAINT pk_reference_style_update_reviews PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_catalog_snapshots
    ADD CONSTRAINT reference_catalog_snapshots_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_delivery_assets
    ADD CONSTRAINT reference_delivery_assets_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_delivery_attestations
    ADD CONSTRAINT reference_delivery_attestations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT reference_delivery_promotions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_delivery_version_artifacts
    ADD CONSTRAINT reference_delivery_version_artifacts_pkey PRIMARY KEY (source_id, version_id, artifact_id, role);
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT reference_delivery_versions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_layer_delivery_state
    ADD CONSTRAINT reference_layer_delivery_state_pkey PRIMARY KEY (provider_key, layer_id);
ALTER TABLE ONLY public.reference_layer_sources
    ADD CONSTRAINT reference_layer_sources_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_layer_styles
    ADD CONSTRAINT reference_layer_styles_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_layers
    ADD CONSTRAINT reference_layers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_license_reviews
    ADD CONSTRAINT reference_license_reviews_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_services
    ADD CONSTRAINT reference_services_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_source_artifacts
    ADD CONSTRAINT reference_source_artifacts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_sync_run_artifacts
    ADD CONSTRAINT reference_sync_run_artifacts_pkey PRIMARY KEY (source_id, run_id, artifact_id, role);
ALTER TABLE ONLY public.reference_sync_runs
    ADD CONSTRAINT reference_sync_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.reference_wms_capabilities_snapshots
    ADD CONSTRAINT reference_wms_capabilities_snapshots_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.organization_reference_layer_settings
    ADD CONSTRAINT uq_org_reference_layer_settings_org_layer UNIQUE (organization_id, layer_id);
ALTER TABLE ONLY public.reference_catalog_observed_versions
    ADD CONSTRAINT uq_reference_catalog_observed_versions_content UNIQUE (provider_key, content_sha256);
ALTER TABLE ONLY public.reference_catalog_observed_versions
    ADD CONSTRAINT uq_reference_catalog_observed_versions_provider_id UNIQUE (provider_key, id);
ALTER TABLE ONLY public.reference_catalog_snapshots
    ADD CONSTRAINT uq_reference_catalog_snapshots_provider_hashes UNIQUE (provider_key, content_sha256, definition_sha256);
ALTER TABLE ONLY public.reference_catalog_snapshots
    ADD CONSTRAINT uq_reference_catalog_snapshots_provider_id UNIQUE (provider_key, id);
ALTER TABLE ONLY public.reference_catalog_snapshots
    ADD CONSTRAINT uq_reference_catalog_snapshots_provider_id_definition UNIQUE (provider_key, id, definition_sha256);
ALTER TABLE ONLY public.reference_catalog_update_checks
    ADD CONSTRAINT uq_reference_catalog_update_checks_idempotency UNIQUE (provider_key, idempotency_key);
ALTER TABLE ONLY public.reference_delivery_assets
    ADD CONSTRAINT uq_reference_delivery_assets_version_key UNIQUE (version_id, asset_key);
ALTER TABLE ONLY public.reference_delivery_attestations
    ADD CONSTRAINT uq_reference_delivery_attestations_chain_target UNIQUE (provider_key, service_id, id, attestation_sha256);
ALTER TABLE ONLY public.reference_delivery_attestations
    ADD CONSTRAINT uq_reference_delivery_attestations_hash UNIQUE (attestation_sha256);
ALTER TABLE ONLY public.reference_delivery_attestations
    ADD CONSTRAINT uq_reference_delivery_attestations_sequence UNIQUE (provider_key, service_id, sequence_number);
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT uq_reference_delivery_promotions_chain_target UNIQUE (provider_key, layer_id, id, event_sha256);
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT uq_reference_delivery_promotions_hash UNIQUE (event_sha256);
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT uq_reference_delivery_promotions_provider_layer_id UNIQUE (provider_key, layer_id, id);
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT uq_reference_delivery_promotions_sequence UNIQUE (provider_key, layer_id, sequence_number);
ALTER TABLE ONLY public.reference_delivery_style_parities
    ADD CONSTRAINT uq_reference_delivery_style_parities_item UNIQUE (version_id, plan_item_id);
ALTER TABLE ONLY public.reference_delivery_style_parities
    ADD CONSTRAINT uq_reference_delivery_style_parities_version_id UNIQUE (version_id, id);
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT uq_reference_delivery_versions_manifest UNIQUE (provider_key, layer_id, manifest_sha256);
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT uq_reference_delivery_versions_provider_layer_id UNIQUE (provider_key, layer_id, id);
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT uq_reference_delivery_versions_sequence UNIQUE (provider_key, layer_id, sequence_number);
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT uq_reference_delivery_versions_source_id UNIQUE (source_id, id);
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT uq_reference_delivery_versions_sync_run UNIQUE (sync_run_id);
ALTER TABLE ONLY public.reference_layer_mirror_strategies
    ADD CONSTRAINT uq_reference_layer_mirror_strategies_provider_id UNIQUE (provider_key, id);
ALTER TABLE ONLY public.reference_layer_mirror_strategies
    ADD CONSTRAINT uq_reference_layer_mirror_strategies_snapshot_generation_layer UNIQUE (provider_key, catalog_snapshot_id, generation, layer_id);
ALTER TABLE ONLY public.reference_layer_mirror_strategy_dependencies
    ADD CONSTRAINT uq_reference_layer_mirror_strategy_dependencies_item UNIQUE (provider_key, strategy_id, dependency_layer_id);
ALTER TABLE ONLY public.reference_layer_sources
    ADD CONSTRAINT uq_reference_layer_sources_layer_source UNIQUE (provider_key, layer_id, source_key);
ALTER TABLE ONLY public.reference_layer_sources
    ADD CONSTRAINT uq_reference_layer_sources_provider_layer_id UNIQUE (provider_key, layer_id, id);
ALTER TABLE ONLY public.reference_layer_styles
    ADD CONSTRAINT uq_reference_layer_styles_provider_id UNIQUE (provider_key, id);
ALTER TABLE ONLY public.reference_layer_styles
    ADD CONSTRAINT uq_reference_layer_styles_provider_layer_source UNIQUE (provider_key, layer_id, source_key);
ALTER TABLE ONLY public.reference_layers
    ADD CONSTRAINT uq_reference_layers_provider_id UNIQUE (provider_key, id);
ALTER TABLE ONLY public.reference_layers
    ADD CONSTRAINT uq_reference_layers_provider_id_service UNIQUE (provider_key, id, service_id);
ALTER TABLE ONLY public.reference_layers
    ADD CONSTRAINT uq_reference_layers_provider_source UNIQUE (provider_key, source_key);
ALTER TABLE ONLY public.reference_license_reviews
    ADD CONSTRAINT uq_reference_license_reviews_content UNIQUE (provider_key, service_id, evidence_sha256, review_sha256);
ALTER TABLE ONLY public.reference_license_reviews
    ADD CONSTRAINT uq_reference_license_reviews_provider_service_id UNIQUE (provider_key, service_id, id);
ALTER TABLE ONLY public.reference_license_reviews
    ADD CONSTRAINT uq_reference_license_reviews_review_hash UNIQUE (provider_key, service_id, review_sha256);
ALTER TABLE ONLY public.reference_mirror_authorization_reviews
    ADD CONSTRAINT uq_reference_mirror_authorizations_chain_target UNIQUE (provider_key, layer_id, source_id, id, review_sha256);
ALTER TABLE ONLY public.reference_mirror_authorization_reviews
    ADD CONSTRAINT uq_reference_mirror_authorizations_content UNIQUE (provider_key, layer_id, source_id, document_sha256, review_sha256);
ALTER TABLE ONLY public.reference_mirror_authorization_reviews
    ADD CONSTRAINT uq_reference_mirror_authorizations_review_hash UNIQUE (provider_key, layer_id, source_id, review_sha256);
ALTER TABLE ONLY public.reference_mirror_authorization_reviews
    ADD CONSTRAINT uq_reference_mirror_authorizations_run_target UNIQUE (provider_key, layer_id, source_id, source_definition_sha256, id, review_sha256);
ALTER TABLE ONLY public.reference_services
    ADD CONSTRAINT uq_reference_services_provider_id UNIQUE (provider_key, id);
ALTER TABLE ONLY public.reference_services
    ADD CONSTRAINT uq_reference_services_provider_source UNIQUE (provider_key, source_key);
ALTER TABLE ONLY public.reference_source_artifacts
    ADD CONSTRAINT uq_reference_source_artifacts_content UNIQUE (source_id, artifact_kind, sha256);
ALTER TABLE ONLY public.reference_source_artifacts
    ADD CONSTRAINT uq_reference_source_artifacts_source_id UNIQUE (source_id, id);
ALTER TABLE ONLY public.reference_style_update_checks
    ADD CONSTRAINT uq_reference_style_checks_idempotency UNIQUE (source_id, source_definition_sha256, idempotency_key);
ALTER TABLE ONLY public.reference_style_observed_versions
    ADD CONSTRAINT uq_reference_style_observed_content UNIQUE (provider_key, layer_id, source_id, source_definition_sha256, source_url, raw_sha256);
ALTER TABLE ONLY public.reference_style_observed_versions
    ADD CONSTRAINT uq_reference_style_observed_target UNIQUE (provider_key, layer_id, source_id, source_definition_sha256, id);
ALTER TABLE ONLY public.reference_style_parity_plan_items
    ADD CONSTRAINT uq_reference_style_parity_plan_items_identity UNIQUE (plan_id, style_source_key);
ALTER TABLE ONLY public.reference_style_parity_plan_items
    ADD CONSTRAINT uq_reference_style_parity_plan_items_plan_id UNIQUE (plan_id, id);
ALTER TABLE ONLY public.reference_style_parity_plan_resources
    ADD CONSTRAINT uq_reference_style_parity_plan_resources_href UNIQUE (plan_item_id, original_href);
ALTER TABLE ONLY public.reference_style_parity_plan_resources
    ADD CONSTRAINT uq_reference_style_parity_plan_resources_item_id UNIQUE (plan_item_id, id);
ALTER TABLE ONLY public.reference_style_parity_plans
    ADD CONSTRAINT uq_reference_style_parity_plans_layer_id UNIQUE (provider_key, layer_id, id);
ALTER TABLE ONLY public.reference_style_parity_plans
    ADD CONSTRAINT uq_reference_style_parity_plans_run UNIQUE (source_id, sync_run_id);
ALTER TABLE ONLY public.reference_style_update_reviews
    ADD CONSTRAINT uq_reference_style_reviews_hash UNIQUE (provider_key, layer_id, source_id, review_sha256);
ALTER TABLE ONLY public.reference_style_update_reviews
    ADD CONSTRAINT uq_reference_style_reviews_observed UNIQUE (observed_version_id);
ALTER TABLE ONLY public.reference_sync_runs
    ADD CONSTRAINT uq_reference_sync_runs_authorization UNIQUE (source_id, id, mirror_authorization_review_id, mirror_authorization_review_sha256);
ALTER TABLE ONLY public.reference_sync_runs
    ADD CONSTRAINT uq_reference_sync_runs_layer_id UNIQUE (provider_key, layer_id, id);
ALTER TABLE ONLY public.reference_sync_runs
    ADD CONSTRAINT uq_reference_sync_runs_source_id UNIQUE (source_id, id);
ALTER TABLE ONLY public.reference_wms_capabilities_snapshots
    ADD CONSTRAINT uq_reference_wms_capabilities_content UNIQUE (provider_key, service_id, raw_sha256, normalized_sha256);
ALTER TABLE ONLY public.reference_wms_capabilities_snapshots
    ADD CONSTRAINT uq_reference_wms_capabilities_provider_service_id UNIQUE (provider_key, service_id, id);
CREATE INDEX ix_org_reference_layer_settings_layer ON public.organization_reference_layer_settings USING btree (layer_id);
CREATE INDEX ix_org_reference_layer_settings_updated_by ON public.organization_reference_layer_settings USING btree (updated_by_id);
CREATE INDEX ix_reference_catalog_observed_versions_retrieved ON public.reference_catalog_observed_versions USING btree (provider_key, retrieved_at, id);
CREATE INDEX ix_reference_catalog_update_checks_latest ON public.reference_catalog_update_checks USING btree (provider_key, checked_at, id);
CREATE INDEX ix_reference_catalog_update_checks_status ON public.reference_catalog_update_checks USING btree (provider_key, status, checked_at);
CREATE INDEX ix_reference_delivery_assets_storage ON public.reference_delivery_assets USING btree (storage_backend, storage_key);
CREATE INDEX ix_reference_delivery_attestations_current_lookup ON public.reference_delivery_attestations USING btree (provider_key, service_id, sequence_number);
CREATE INDEX ix_reference_delivery_promotions_actor ON public.reference_delivery_promotions USING btree (actor_id);
CREATE INDEX ix_reference_delivery_promotions_current_lookup ON public.reference_delivery_promotions USING btree (provider_key, layer_id, sequence_number);
CREATE INDEX ix_reference_delivery_promotions_from_version ON public.reference_delivery_promotions USING btree (provider_key, layer_id, from_version_id);
CREATE INDEX ix_reference_delivery_promotions_run ON public.reference_delivery_promotions USING btree (run_id);
CREATE INDEX ix_reference_delivery_promotions_to_version ON public.reference_delivery_promotions USING btree (provider_key, layer_id, to_version_id);
CREATE INDEX ix_reference_delivery_style_parities_version ON public.reference_delivery_style_parities USING btree (version_id, parity_kind, id);
CREATE INDEX ix_reference_delivery_version_artifacts_artifact ON public.reference_delivery_version_artifacts USING btree (artifact_id, version_id);
CREATE INDEX ix_reference_delivery_versions_catalog_snapshot ON public.reference_delivery_versions USING btree (catalog_snapshot_id);
CREATE INDEX ix_reference_delivery_versions_source_history ON public.reference_delivery_versions USING btree (source_id, id);
CREATE INDEX ix_reference_layer_delivery_state_active_version ON public.reference_layer_delivery_state USING btree (active_version_id);
CREATE INDEX ix_reference_layer_delivery_state_last_promotion ON public.reference_layer_delivery_state USING btree (last_promotion_id);
CREATE INDEX ix_reference_layer_mirror_strategies_current ON public.reference_layer_mirror_strategies USING btree (provider_key, catalog_snapshot_id, generation, layer_id);
CREATE INDEX ix_reference_layer_mirror_strategy_dependencies_order ON public.reference_layer_mirror_strategy_dependencies USING btree (strategy_id, dependency_order);
CREATE INDEX ix_reference_layer_sources_due ON public.reference_layer_sources USING btree (next_check_at, id) WHERE enabled;
CREATE INDEX ix_reference_layer_sources_layer_priority ON public.reference_layer_sources USING btree (layer_id, enabled, priority, id);
CREATE INDEX ix_reference_layer_styles_layer_order ON public.reference_layer_styles USING btree (layer_id, sort_order, id);
CREATE INDEX ix_reference_layer_styles_snapshot ON public.reference_layer_styles USING btree (last_seen_snapshot_id);
CREATE INDEX ix_reference_layers_parent_order ON public.reference_layers USING btree (parent_id, sort_order, id);
CREATE INDEX ix_reference_layers_service_status ON public.reference_layers USING btree (service_id, status);
CREATE INDEX ix_reference_layers_snapshot ON public.reference_layers USING btree (last_seen_snapshot_id);
CREATE INDEX ix_reference_license_reviews_service_reviewed ON public.reference_license_reviews USING btree (provider_key, service_id, id);
CREATE INDEX ix_reference_mirror_authorizations_source_reviewed ON public.reference_mirror_authorization_reviews USING btree (provider_key, layer_id, source_id, reviewed_at, id);
CREATE INDEX ix_reference_services_snapshot ON public.reference_services USING btree (last_seen_snapshot_id);
CREATE INDEX ix_reference_services_status ON public.reference_services USING btree (provider_key, status);
CREATE INDEX ix_reference_source_artifacts_source_history ON public.reference_source_artifacts USING btree (source_id, id);
CREATE INDEX ix_reference_source_artifacts_storage ON public.reference_source_artifacts USING btree (storage_backend, storage_key);
CREATE INDEX ix_reference_style_checks_latest ON public.reference_style_update_checks USING btree (source_id, source_definition_sha256, checked_at, id);
CREATE INDEX ix_reference_style_checks_pending ON public.reference_style_update_checks USING btree (source_id, status, checked_at);
CREATE INDEX ix_reference_style_observed_source_retrieved ON public.reference_style_observed_versions USING btree (source_id, retrieved_at, id);
CREATE INDEX ix_reference_style_parity_plan_items_status ON public.reference_style_parity_plan_items USING btree (plan_id, parity_kind, id);
CREATE INDEX ix_reference_style_parity_plan_resources_artifact ON public.reference_style_parity_plan_resources USING btree (artifact_id, plan_item_id);
CREATE INDEX ix_reference_style_parity_plans_snapshot ON public.reference_style_parity_plans USING btree (provider_key, catalog_snapshot_id, layer_id);
CREATE INDEX ix_reference_style_reviews_source_reviewed ON public.reference_style_update_reviews USING btree (source_id, reviewed_at, id);
CREATE INDEX ix_reference_sync_run_artifacts_artifact ON public.reference_sync_run_artifacts USING btree (artifact_id, run_id);
CREATE INDEX ix_reference_sync_runs_layer_history ON public.reference_sync_runs USING btree (provider_key, layer_id, id);
CREATE INDEX ix_reference_sync_runs_queued ON public.reference_sync_runs USING btree (queued_at, id) WHERE ((status)::text = 'queued'::text);
CREATE INDEX ix_reference_sync_runs_requested_by ON public.reference_sync_runs USING btree (requested_by_id);
CREATE INDEX ix_reference_sync_runs_running_lease ON public.reference_sync_runs USING btree (lease_expires_at, id) WHERE ((status)::text = 'running'::text);
CREATE INDEX ix_reference_sync_runs_source_history ON public.reference_sync_runs USING btree (source_id, id);
CREATE INDEX ix_reference_wms_capabilities_service_created ON public.reference_wms_capabilities_snapshots USING btree (provider_key, service_id, id);
CREATE UNIQUE INDEX uq_reference_catalog_snapshots_current_provider ON public.reference_catalog_snapshots USING btree (provider_key) WHERE is_current;
CREATE UNIQUE INDEX uq_reference_delivery_assets_primary ON public.reference_delivery_assets USING btree (version_id) WHERE is_primary;
CREATE UNIQUE INDEX uq_reference_delivery_attestations_genesis ON public.reference_delivery_attestations USING btree (provider_key, service_id) WHERE (previous_attestation_id IS NULL);
CREATE UNIQUE INDEX uq_reference_delivery_attestations_successor ON public.reference_delivery_attestations USING btree (provider_key, service_id, previous_attestation_id) WHERE (previous_attestation_id IS NOT NULL);
CREATE UNIQUE INDEX uq_reference_delivery_promotions_genesis ON public.reference_delivery_promotions USING btree (provider_key, layer_id) WHERE (previous_event_id IS NULL);
CREATE UNIQUE INDEX uq_reference_delivery_promotions_successor ON public.reference_delivery_promotions USING btree (provider_key, layer_id, previous_event_id) WHERE (previous_event_id IS NOT NULL);
CREATE UNIQUE INDEX uq_reference_layer_sources_primary ON public.reference_layer_sources USING btree (provider_key, layer_id) WHERE (enabled AND is_primary);
CREATE UNIQUE INDEX uq_reference_layer_styles_default ON public.reference_layer_styles USING btree (provider_key, layer_id) WHERE is_default;
CREATE UNIQUE INDEX uq_reference_license_reviews_genesis ON public.reference_license_reviews USING btree (provider_key, service_id) WHERE (supersedes_review_sha256 IS NULL);
CREATE UNIQUE INDEX uq_reference_license_reviews_successor ON public.reference_license_reviews USING btree (provider_key, service_id, supersedes_review_sha256) WHERE (supersedes_review_sha256 IS NOT NULL);
CREATE UNIQUE INDEX uq_reference_mirror_authorizations_genesis ON public.reference_mirror_authorization_reviews USING btree (provider_key, layer_id, source_id) WHERE (supersedes_review_id IS NULL);
CREATE UNIQUE INDEX uq_reference_mirror_authorizations_successor ON public.reference_mirror_authorization_reviews USING btree (provider_key, layer_id, source_id, supersedes_review_id) WHERE (supersedes_review_id IS NOT NULL);
CREATE UNIQUE INDEX uq_reference_sync_runs_fallback_child ON public.reference_sync_runs USING btree (parent_run_id) WHERE (parent_run_id IS NOT NULL);
CREATE UNIQUE INDEX uq_reference_sync_runs_open_layer ON public.reference_sync_runs USING btree (provider_key, layer_id) WHERE ((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying])::text[]));
CREATE UNIQUE INDEX uq_reference_sync_runs_open_source ON public.reference_sync_runs USING btree (source_id) WHERE ((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying])::text[]));
CREATE TRIGGER trg_reference_catalog_observed_versions_immutable BEFORE DELETE OR UPDATE ON public.reference_catalog_observed_versions FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_catalog_observed_version_mutation();
CREATE TRIGGER trg_reference_catalog_observed_versions_truncate_immutable BEFORE TRUNCATE ON public.reference_catalog_observed_versions FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_catalog_observed_version_mutation();
CREATE TRIGGER trg_reference_catalog_update_checks_immutable BEFORE DELETE OR UPDATE ON public.reference_catalog_update_checks FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_catalog_update_check_mutation();
CREATE TRIGGER trg_reference_catalog_update_checks_truncate_immutable BEFORE TRUNCATE ON public.reference_catalog_update_checks FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_catalog_update_check_mutation();
CREATE TRIGGER trg_reference_delivery_assets_immutable BEFORE DELETE OR UPDATE ON public.reference_delivery_assets FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_delivery_asset_mutation();
CREATE TRIGGER trg_reference_delivery_assets_truncate_immutable BEFORE TRUNCATE ON public.reference_delivery_assets FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_delivery_asset_mutation();
CREATE TRIGGER trg_reference_delivery_attestations_immutable BEFORE DELETE OR UPDATE ON public.reference_delivery_attestations FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_delivery_attestation_mutation();
CREATE TRIGGER trg_reference_delivery_promotions_immutable BEFORE DELETE OR UPDATE ON public.reference_delivery_promotions FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_delivery_promotion_mutation();
CREATE TRIGGER trg_reference_delivery_promotions_truncate_immutable BEFORE TRUNCATE ON public.reference_delivery_promotions FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_delivery_promotion_mutation();
CREATE TRIGGER trg_reference_delivery_state_head BEFORE INSERT OR UPDATE ON public.reference_layer_delivery_state FOR EACH ROW EXECUTE FUNCTION public.validate_reference_delivery_state_head();
CREATE TRIGGER trg_reference_delivery_style_parities_immutable BEFORE DELETE OR UPDATE ON public.reference_delivery_style_parities FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_delivery_style_parity_mutation();
CREATE TRIGGER trg_reference_delivery_style_parities_truncate_immutable BEFORE TRUNCATE ON public.reference_delivery_style_parities FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_delivery_style_parity_mutation();
CREATE TRIGGER trg_reference_delivery_style_parities_validate BEFORE INSERT ON public.reference_delivery_style_parities FOR EACH ROW EXECUTE FUNCTION public.validate_reference_delivery_style_parity();
CREATE TRIGGER trg_reference_delivery_style_resources_immutable BEFORE DELETE OR UPDATE ON public.reference_delivery_style_resources FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_delivery_style_resource_mutation();
CREATE TRIGGER trg_reference_delivery_style_resources_truncate_immutable BEFORE TRUNCATE ON public.reference_delivery_style_resources FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_delivery_style_resource_mutation();
CREATE TRIGGER trg_reference_delivery_style_resources_validate BEFORE INSERT ON public.reference_delivery_style_resources FOR EACH ROW EXECUTE FUNCTION public.validate_reference_delivery_style_resource();
CREATE TRIGGER trg_reference_delivery_version_artifacts_immutable BEFORE DELETE OR UPDATE ON public.reference_delivery_version_artifacts FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_delivery_version_artifact_mutation();
CREATE TRIGGER trg_reference_delivery_version_artifacts_truncate_immutable BEFORE TRUNCATE ON public.reference_delivery_version_artifacts FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_delivery_version_artifact_mutation();
CREATE TRIGGER trg_reference_delivery_versions_immutable BEFORE DELETE OR UPDATE ON public.reference_delivery_versions FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_delivery_version_mutation();
CREATE TRIGGER trg_reference_delivery_versions_truncate_immutable BEFORE TRUNCATE ON public.reference_delivery_versions FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_delivery_version_mutation();
CREATE TRIGGER trg_reference_layer_mirror_strategy_dependency_immutable BEFORE DELETE OR UPDATE ON public.reference_layer_mirror_strategy_dependencies FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_layer_mirror_strategy_dependency_mutation();
CREATE TRIGGER trg_reference_layer_mirror_strategy_dependency_validate AFTER INSERT OR UPDATE ON public.reference_layer_mirror_strategy_dependencies FOR EACH ROW EXECUTE FUNCTION public.validate_reference_layer_mirror_strategy_dependency();
CREATE TRIGGER trg_reference_layer_mirror_strategy_immutable BEFORE DELETE OR UPDATE ON public.reference_layer_mirror_strategies FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_layer_mirror_strategy_mutation();
CREATE TRIGGER trg_reference_layer_mirror_strategy_truncate_immutable BEFORE TRUNCATE ON public.reference_layer_mirror_strategies FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_layer_mirror_strategy_mutation();
CREATE TRIGGER trg_reference_license_reviews_immutable BEFORE DELETE OR UPDATE ON public.reference_license_reviews FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_license_review_mutation();
CREATE TRIGGER trg_reference_mirror_authorizations_immutable BEFORE DELETE OR UPDATE ON public.reference_mirror_authorization_reviews FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_mirror_authorization_mutation();
CREATE TRIGGER trg_reference_mirror_authorizations_truncate_immutable BEFORE TRUNCATE ON public.reference_mirror_authorization_reviews FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_mirror_authorization_mutation();
CREATE TRIGGER trg_reference_mirror_authorizations_validate BEFORE INSERT ON public.reference_mirror_authorization_reviews FOR EACH ROW EXECUTE FUNCTION public.validate_reference_mirror_authorization();
CREATE CONSTRAINT TRIGGER trg_reference_promotion_state AFTER INSERT ON public.reference_delivery_promotions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_reference_promotion_state();
CREATE TRIGGER trg_reference_source_artifacts_immutable BEFORE DELETE OR UPDATE ON public.reference_source_artifacts FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_source_artifact_mutation();
CREATE TRIGGER trg_reference_source_artifacts_truncate_immutable BEFORE TRUNCATE ON public.reference_source_artifacts FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_source_artifact_mutation();
CREATE TRIGGER trg_reference_style_checks_immutable BEFORE DELETE OR UPDATE ON public.reference_style_update_checks FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_style_check_mutation();
CREATE TRIGGER trg_reference_style_checks_truncate_immutable BEFORE TRUNCATE ON public.reference_style_update_checks FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_style_check_mutation();
CREATE TRIGGER trg_reference_style_observed_immutable BEFORE DELETE OR UPDATE ON public.reference_style_observed_versions FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_style_observed_mutation();
CREATE TRIGGER trg_reference_style_observed_truncate_immutable BEFORE TRUNCATE ON public.reference_style_observed_versions FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_style_observed_mutation();
CREATE TRIGGER trg_reference_style_parity_plan_items_immutable BEFORE DELETE OR UPDATE ON public.reference_style_parity_plan_items FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_style_parity_plan_item_mutation();
CREATE TRIGGER trg_reference_style_parity_plan_items_truncate_immutable BEFORE TRUNCATE ON public.reference_style_parity_plan_items FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_style_parity_plan_item_mutation();
CREATE TRIGGER trg_reference_style_parity_plan_items_validate BEFORE INSERT ON public.reference_style_parity_plan_items FOR EACH ROW EXECUTE FUNCTION public.validate_reference_style_parity_plan_item();
CREATE TRIGGER trg_reference_style_parity_plan_resources_immutable BEFORE DELETE OR UPDATE ON public.reference_style_parity_plan_resources FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_style_parity_plan_resource_mutation();
CREATE TRIGGER trg_reference_style_parity_plan_resources_truncate_immutable BEFORE TRUNCATE ON public.reference_style_parity_plan_resources FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_style_parity_plan_resource_mutation();
CREATE TRIGGER trg_reference_style_parity_plan_resources_validate BEFORE INSERT ON public.reference_style_parity_plan_resources FOR EACH ROW EXECUTE FUNCTION public.validate_reference_style_parity_plan_resource();
CREATE TRIGGER trg_reference_style_parity_plans_immutable BEFORE DELETE OR UPDATE ON public.reference_style_parity_plans FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_style_parity_plan_mutation();
CREATE TRIGGER trg_reference_style_parity_plans_truncate_immutable BEFORE TRUNCATE ON public.reference_style_parity_plans FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_style_parity_plan_mutation();
CREATE TRIGGER trg_reference_style_reviews_immutable BEFORE DELETE OR UPDATE ON public.reference_style_update_reviews FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_style_review_mutation();
CREATE TRIGGER trg_reference_style_reviews_truncate_immutable BEFORE TRUNCATE ON public.reference_style_update_reviews FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_style_review_mutation();
CREATE TRIGGER trg_reference_sync_run_artifacts_immutable BEFORE DELETE OR UPDATE ON public.reference_sync_run_artifacts FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_sync_run_artifact_mutation();
CREATE TRIGGER trg_reference_sync_run_artifacts_truncate_immutable BEFORE TRUNCATE ON public.reference_sync_run_artifacts FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_reference_sync_run_artifact_mutation();
CREATE TRIGGER trg_reference_wms_capabilities_immutable BEFORE DELETE OR UPDATE ON public.reference_wms_capabilities_snapshots FOR EACH ROW EXECUTE FUNCTION public.prevent_reference_wms_capabilities_mutation();
ALTER TABLE ONLY public.reference_catalog_update_checks
    ADD CONSTRAINT fk_reference_catalog_update_checks_baseline FOREIGN KEY (provider_key, baseline_snapshot_id) REFERENCES public.reference_catalog_snapshots(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_catalog_update_checks
    ADD CONSTRAINT fk_reference_catalog_update_checks_observed FOREIGN KEY (provider_key, observed_version_id) REFERENCES public.reference_catalog_observed_versions(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_attestations
    ADD CONSTRAINT fk_reference_delivery_attestations_capabilities FOREIGN KEY (provider_key, service_id, capabilities_snapshot_id) REFERENCES public.reference_wms_capabilities_snapshots(provider_key, service_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_attestations
    ADD CONSTRAINT fk_reference_delivery_attestations_catalog FOREIGN KEY (provider_key, catalog_snapshot_id, catalog_definition_sha256) REFERENCES public.reference_catalog_snapshots(provider_key, id, definition_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_attestations
    ADD CONSTRAINT fk_reference_delivery_attestations_license FOREIGN KEY (provider_key, service_id, license_review_id) REFERENCES public.reference_license_reviews(provider_key, service_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_attestations
    ADD CONSTRAINT fk_reference_delivery_attestations_previous FOREIGN KEY (provider_key, service_id, previous_attestation_id, previous_attestation_sha256) REFERENCES public.reference_delivery_attestations(provider_key, service_id, id, attestation_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_attestations
    ADD CONSTRAINT fk_reference_delivery_attestations_provider_service FOREIGN KEY (provider_key, service_id) REFERENCES public.reference_services(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT fk_reference_delivery_promotions_from_version FOREIGN KEY (provider_key, layer_id, from_version_id) REFERENCES public.reference_delivery_versions(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT fk_reference_delivery_promotions_previous FOREIGN KEY (provider_key, layer_id, previous_event_id, previous_event_sha256) REFERENCES public.reference_delivery_promotions(provider_key, layer_id, id, event_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT fk_reference_delivery_promotions_provider_layer FOREIGN KEY (provider_key, layer_id) REFERENCES public.reference_layers(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT fk_reference_delivery_promotions_to_version FOREIGN KEY (provider_key, layer_id, to_version_id) REFERENCES public.reference_delivery_versions(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_style_parities
    ADD CONSTRAINT fk_reference_delivery_style_parities_asset FOREIGN KEY (delivery_asset_id) REFERENCES public.reference_delivery_assets(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_style_parities
    ADD CONSTRAINT fk_reference_delivery_style_parities_plan_item FOREIGN KEY (plan_item_id) REFERENCES public.reference_style_parity_plan_items(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_style_parities
    ADD CONSTRAINT fk_reference_delivery_style_parities_version FOREIGN KEY (version_id) REFERENCES public.reference_delivery_versions(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_style_resources
    ADD CONSTRAINT fk_reference_delivery_style_resources_parity FOREIGN KEY (delivery_parity_id) REFERENCES public.reference_delivery_style_parities(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_style_resources
    ADD CONSTRAINT fk_reference_delivery_style_resources_plan_resource FOREIGN KEY (plan_resource_id) REFERENCES public.reference_style_parity_plan_resources(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_version_artifacts
    ADD CONSTRAINT fk_reference_delivery_version_artifacts_source_artifact FOREIGN KEY (source_id, artifact_id) REFERENCES public.reference_source_artifacts(source_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_version_artifacts
    ADD CONSTRAINT fk_reference_delivery_version_artifacts_source_version FOREIGN KEY (source_id, version_id) REFERENCES public.reference_delivery_versions(source_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT fk_reference_delivery_versions_catalog FOREIGN KEY (provider_key, catalog_snapshot_id, catalog_definition_sha256) REFERENCES public.reference_catalog_snapshots(provider_key, id, definition_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT fk_reference_delivery_versions_layer_source FOREIGN KEY (provider_key, layer_id, source_id) REFERENCES public.reference_layer_sources(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT fk_reference_delivery_versions_mirror_authorization FOREIGN KEY (provider_key, layer_id, source_id, mirror_authorization_review_id, mirror_authorization_review_sha256) REFERENCES public.reference_mirror_authorization_reviews(provider_key, layer_id, source_id, id, review_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT fk_reference_delivery_versions_run_authorization FOREIGN KEY (source_id, sync_run_id, mirror_authorization_review_id, mirror_authorization_review_sha256) REFERENCES public.reference_sync_runs(source_id, id, mirror_authorization_review_id, mirror_authorization_review_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_versions
    ADD CONSTRAINT fk_reference_delivery_versions_source_run FOREIGN KEY (source_id, sync_run_id) REFERENCES public.reference_sync_runs(source_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_delivery_state
    ADD CONSTRAINT fk_reference_layer_delivery_state_active_version FOREIGN KEY (provider_key, layer_id, active_version_id) REFERENCES public.reference_delivery_versions(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_delivery_state
    ADD CONSTRAINT fk_reference_layer_delivery_state_last_promotion FOREIGN KEY (provider_key, layer_id, last_promotion_id) REFERENCES public.reference_delivery_promotions(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_delivery_state
    ADD CONSTRAINT fk_reference_layer_delivery_state_provider_layer FOREIGN KEY (provider_key, layer_id) REFERENCES public.reference_layers(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_mirror_strategies
    ADD CONSTRAINT fk_reference_layer_mirror_strategies_provider_layer FOREIGN KEY (provider_key, layer_id) REFERENCES public.reference_layers(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_mirror_strategies
    ADD CONSTRAINT fk_reference_layer_mirror_strategies_provider_snapshot FOREIGN KEY (provider_key, catalog_snapshot_id) REFERENCES public.reference_catalog_snapshots(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_mirror_strategies
    ADD CONSTRAINT fk_reference_layer_mirror_strategies_provider_source FOREIGN KEY (source_id) REFERENCES public.reference_layer_sources(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_mirror_strategy_dependencies
    ADD CONSTRAINT fk_reference_layer_mirror_strategy_dependencies_layer FOREIGN KEY (provider_key, dependency_layer_id) REFERENCES public.reference_layers(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_mirror_strategy_dependencies
    ADD CONSTRAINT fk_reference_layer_mirror_strategy_dependencies_strategy FOREIGN KEY (provider_key, strategy_id) REFERENCES public.reference_layer_mirror_strategies(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_sources
    ADD CONSTRAINT fk_reference_layer_sources_provider_layer FOREIGN KEY (provider_key, layer_id) REFERENCES public.reference_layers(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_styles
    ADD CONSTRAINT fk_reference_layer_styles_provider_layer FOREIGN KEY (provider_key, layer_id) REFERENCES public.reference_layers(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layer_styles
    ADD CONSTRAINT fk_reference_layer_styles_provider_snapshot FOREIGN KEY (provider_key, last_seen_snapshot_id) REFERENCES public.reference_catalog_snapshots(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layers
    ADD CONSTRAINT fk_reference_layers_provider_parent FOREIGN KEY (provider_key, parent_id) REFERENCES public.reference_layers(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layers
    ADD CONSTRAINT fk_reference_layers_provider_service FOREIGN KEY (provider_key, service_id) REFERENCES public.reference_services(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_layers
    ADD CONSTRAINT fk_reference_layers_provider_snapshot FOREIGN KEY (provider_key, last_seen_snapshot_id) REFERENCES public.reference_catalog_snapshots(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_license_reviews
    ADD CONSTRAINT fk_reference_license_reviews_provider_service FOREIGN KEY (provider_key, service_id) REFERENCES public.reference_services(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_license_reviews
    ADD CONSTRAINT fk_reference_license_reviews_supersedes FOREIGN KEY (provider_key, service_id, supersedes_review_sha256) REFERENCES public.reference_license_reviews(provider_key, service_id, review_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_mirror_authorization_reviews
    ADD CONSTRAINT fk_reference_mirror_authorizations_layer_service FOREIGN KEY (provider_key, layer_id, service_id) REFERENCES public.reference_layers(provider_key, id, service_id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_mirror_authorization_reviews
    ADD CONSTRAINT fk_reference_mirror_authorizations_source FOREIGN KEY (provider_key, layer_id, source_id) REFERENCES public.reference_layer_sources(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_mirror_authorization_reviews
    ADD CONSTRAINT fk_reference_mirror_authorizations_supersedes FOREIGN KEY (provider_key, layer_id, source_id, supersedes_review_id, supersedes_review_sha256) REFERENCES public.reference_mirror_authorization_reviews(provider_key, layer_id, source_id, id, review_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_services
    ADD CONSTRAINT fk_reference_services_provider_snapshot FOREIGN KEY (provider_key, last_seen_snapshot_id) REFERENCES public.reference_catalog_snapshots(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_update_checks
    ADD CONSTRAINT fk_reference_style_checks_authorization FOREIGN KEY (provider_key, layer_id, source_id, source_definition_sha256, authorization_review_id, authorization_review_sha256) REFERENCES public.reference_mirror_authorization_reviews(provider_key, layer_id, source_id, source_definition_sha256, id, review_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_update_checks
    ADD CONSTRAINT fk_reference_style_checks_observed FOREIGN KEY (provider_key, layer_id, source_id, source_definition_sha256, observed_version_id) REFERENCES public.reference_style_observed_versions(provider_key, layer_id, source_id, source_definition_sha256, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_update_checks
    ADD CONSTRAINT fk_reference_style_checks_source FOREIGN KEY (provider_key, layer_id, source_id) REFERENCES public.reference_layer_sources(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_observed_versions
    ADD CONSTRAINT fk_reference_style_observed_source FOREIGN KEY (provider_key, layer_id, source_id) REFERENCES public.reference_layer_sources(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_parity_plan_items
    ADD CONSTRAINT fk_reference_style_parity_plan_items_package_artifact FOREIGN KEY (source_id, source_package_artifact_id) REFERENCES public.reference_source_artifacts(source_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_parity_plan_items
    ADD CONSTRAINT fk_reference_style_parity_plan_items_plan FOREIGN KEY (plan_id) REFERENCES public.reference_style_parity_plans(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_parity_plan_items
    ADD CONSTRAINT fk_reference_style_parity_plan_items_style FOREIGN KEY (style_id) REFERENCES public.reference_layer_styles(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_parity_plan_items
    ADD CONSTRAINT fk_reference_style_parity_plan_items_style_artifact FOREIGN KEY (source_id, source_style_artifact_id) REFERENCES public.reference_source_artifacts(source_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_parity_plan_resources
    ADD CONSTRAINT fk_reference_style_parity_plan_resources_artifact FOREIGN KEY (source_id, artifact_id) REFERENCES public.reference_source_artifacts(source_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_parity_plan_resources
    ADD CONSTRAINT fk_reference_style_parity_plan_resources_item FOREIGN KEY (plan_item_id) REFERENCES public.reference_style_parity_plan_items(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_parity_plans
    ADD CONSTRAINT fk_reference_style_parity_plans_catalog FOREIGN KEY (provider_key, catalog_snapshot_id, catalog_definition_sha256) REFERENCES public.reference_catalog_snapshots(provider_key, id, definition_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_parity_plans
    ADD CONSTRAINT fk_reference_style_parity_plans_layer_source FOREIGN KEY (provider_key, layer_id, source_id) REFERENCES public.reference_layer_sources(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_parity_plans
    ADD CONSTRAINT fk_reference_style_parity_plans_source_run FOREIGN KEY (source_id, sync_run_id) REFERENCES public.reference_sync_runs(source_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_style_update_reviews
    ADD CONSTRAINT fk_reference_style_reviews_observed FOREIGN KEY (provider_key, layer_id, source_id, source_definition_sha256, observed_version_id) REFERENCES public.reference_style_observed_versions(provider_key, layer_id, source_id, source_definition_sha256, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_sync_run_artifacts
    ADD CONSTRAINT fk_reference_sync_run_artifacts_source_artifact FOREIGN KEY (source_id, artifact_id) REFERENCES public.reference_source_artifacts(source_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_sync_run_artifacts
    ADD CONSTRAINT fk_reference_sync_run_artifacts_source_run FOREIGN KEY (source_id, run_id) REFERENCES public.reference_sync_runs(source_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_sync_runs
    ADD CONSTRAINT fk_reference_sync_runs_layer_source FOREIGN KEY (provider_key, layer_id, source_id) REFERENCES public.reference_layer_sources(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_sync_runs
    ADD CONSTRAINT fk_reference_sync_runs_mirror_authorization FOREIGN KEY (provider_key, layer_id, source_id, source_definition_sha256, mirror_authorization_review_id, mirror_authorization_review_sha256) REFERENCES public.reference_mirror_authorization_reviews(provider_key, layer_id, source_id, source_definition_sha256, id, review_sha256) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_sync_runs
    ADD CONSTRAINT fk_reference_sync_runs_parent FOREIGN KEY (provider_key, layer_id, parent_run_id) REFERENCES public.reference_sync_runs(provider_key, layer_id, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_wms_capabilities_snapshots
    ADD CONSTRAINT fk_reference_wms_capabilities_provider_service FOREIGN KEY (provider_key, service_id) REFERENCES public.reference_services(provider_key, id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.organization_reference_layer_settings
    ADD CONSTRAINT organization_reference_layer_settings_layer_id_fkey FOREIGN KEY (layer_id) REFERENCES public.reference_layers(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.organization_reference_layer_settings
    ADD CONSTRAINT organization_reference_layer_settings_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.organization_reference_layer_settings
    ADD CONSTRAINT organization_reference_layer_settings_updated_by_id_fkey FOREIGN KEY (updated_by_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.reference_delivery_assets
    ADD CONSTRAINT reference_delivery_assets_version_id_fkey FOREIGN KEY (version_id) REFERENCES public.reference_delivery_versions(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT reference_delivery_promotions_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.reference_delivery_promotions
    ADD CONSTRAINT reference_delivery_promotions_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.reference_sync_runs(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_source_artifacts
    ADD CONSTRAINT reference_source_artifacts_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.reference_layer_sources(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.reference_sync_runs
    ADD CONSTRAINT reference_sync_runs_requested_by_id_fkey FOREIGN KEY (requested_by_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.reference_sync_runs
    ADD CONSTRAINT reference_sync_runs_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.reference_layer_sources(id) ON DELETE RESTRICT;
