# SIUR composite direct downloads

Some official vector datasets are published as one archive per province rather
than as one regional file. The mirror represents such a publication as one
exact, ordered `download` source. This document describes the generic contract;
it does not authorize or configure any particular SIGPAC layer.

## Source contract

A composite source uses `protocol=download`, `target_kind=vector`,
`sync_strategy=conditional_get` and a `download_resources` list. The list order
is semantic and becomes the immutable `page_index` used by materialization.
Every entry has this exact shape:

```json
{
  "resource_key": "province-key",
  "url": "https://official.example/dataset/province.zip",
  "media_type": "application/zip",
  "data_format": "shapefile-zip",
  "max_download_bytes": 2147483648,
  "max_uncompressed_bytes": 8589934592,
  "validation": {
    "archive_member": "province.shp",
    "input_layer": "province"
  }
}
```

The source also declares `max_aggregate_download_bytes` and
`max_aggregate_uncompressed_bytes`. All URLs must be unique, HTTPS and on the
reviewed source origin; resource keys must be unique; all resources must use
one materializable vector format. ZIP resources are structurally inspected and
an exact Shapefile member is required. Optional reviewed archive-integrity or
content-parity evidence remains hash-bound in each resource's `validation`
object.

Single-resource options, remote style downloads, archive-extracted styles and
masked-GeoPackage transforms cannot be mixed with this contract. Locally
authored styles remain possible when their recipe is already bound into the
source definition.

## Conditional daily check

The active delivery already links every immutable dataset artifact. For each
resource, the worker reconstructs:

- the active CAS key, SHA-256, size and materialization metadata;
- the latest ETag and Last-Modified observation from a successful or unchanged
  run with the same source definition and active generation.

Each URL is checked independently:

- `304` reuses the exact active dataset blob;
- `200` is staged and fully validated;
- a `200` whose SHA-256 still equals the active blob updates its validators but
  is content-unchanged;
- a changed `200` replaces only that resource in the candidate snapshot.

All changed responses remain under one staging-batch lease until every resource
has passed. A changed candidate contains all input artifacts—new and reused—in
contiguous `page_index` order. An all-unchanged check persists small
per-resource observation artifacts and does not rematerialize the delivery.

The existing artifact and run-link tables hold this state, so no database
migration is required.

## Failure and atomicity boundaries

Per-file download/expansion limits and aggregate retained/expansion limits are
independent. Missing resources, partial or unexpected `304` responses,
duplicate content, unsafe archives, ordering gaps, incompatible active
metadata, and missing or size-altered CAS files fail closed.

The visible snapshot is atomic: database linkage, delivery construction and
promotion are fenced transactions, so a stale worker or partial response set
cannot become active. CAS adoption consists of independent
content-addressed renames; a crash between them can leave an unlinked,
recoverable blob, never a partial delivery version.

Daily reuse checks the canonical CAS key, safe path and recorded size. It does
not rehash every multi-gigabyte active archive on every `304`. CAS writes verify
the digest at commit time, and any changed aggregate is copied and hash-checked
again by multipage ingest before promotion.

This acquisition mechanism does not bypass mirror authorization. A source can
be discovered and reviewed without network acquisition; scheduling and
download still require the persisted conservation/service authorization.
