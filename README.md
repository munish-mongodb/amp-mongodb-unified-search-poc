# AMP on MongoDB Atlas: Unified Multi-Tenant, Vector & Hybrid Search POC

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/munish-mongodb/amp-mongodb-unified-search-poc/blob/main/notebooks/amp_mongodb_poc.ipynb)

This is an executable proof-of-concept for [`spec.md`](spec.md)
(SPEC-001-AMP-MONGO): can a single MongoDB Atlas cluster replace a
split-brain MongoDB + PostgreSQL architecture for an OEM asset management
platform (vehicles, EV chargers, e-bikes), handling polymorphic asset data,
hierarchical multi-tenant authorization, and hybrid keyword/vector search
in one query pass?

The notebook runs against a **live 50,019-document collection** (50,000
synthetic vehicles across 5 fleet-customer tenants + a small 19-document
curated correctness fixture), modeling the real entity-relationship
structure Rivian's fleet architecture uses -- not a toy dataset. See
[Entity relationship model](#entity-relationship-model) below.

Every claim below was verified by actually running the code against live
Atlas clusters -- nothing here is illustrative pseudocode. This was
validated on two clusters: MongoDB 8.0.30 (where native `$rerank` isn't
available yet, exercising the Voyage AI API fallback path) and MongoDB
9.0.0 (where native `$rerank` works, exercising the server-side path).
Both paths produce the same reordering result; the notebook auto-detects
and reports which one ran. Open `notebooks/amp_mongodb_poc.ipynb` in Colab
to run it yourself.

## Spec vs. reality

The original spec sketched two capabilities using syntax that turned out not
to match what's actually shipped. Building against a live cluster surfaced
the real behavior:

| Spec said | Reality (verified live) |
|---|---|
| `autoEmbed` as a `mappings.fields` entry, model `voyage-3` | `autoEmbed` is real, but it's a top-level `fields` array entry (`type: "autoEmbed"`, `modality: "text"`), and only supports the `voyage-4` model family -- `voyage-3.x` is rejected. Native server-side embedding **works** once you use the right shape. |
| `$rerank` aggregation stage, params `queryText`/`field`/`topK` | `$rerank` is a real MongoDB 8.3+ **Preview** stage, but with different params (`model`, `query.text`, `path`, `numDocsToRerank`) and two hard requirements: (1) Native Reranking enabled in Atlas **Project Settings**, and (2) the **cluster itself** running MongoDB 8.3+. Enabling the project setting alone does nothing on an older mongod -- confirmed by testing on a 8.0.30 cluster with the project setting on, which still returned `Unrecognized pipeline stage name: '$rerank'`. It also cannot take `$rankFusion`/`$scoreFusion` as input. **Confirmed working** on a 9.0.0 cluster. The notebook tries native `$rerank` first and falls back to the Voyage AI `.rerank()` API automatically if the server rejects the stage -- both paths verified live. |
| *(not mentioned)* | MongoDB ships a native **`$rankFusion`** stage that combines keyword (`$search`) and vector (`$vectorSearch`) sub-pipelines with reciprocal rank fusion in a single aggregation call -- a better hybrid-search primitive than the spec assumed existed. |

## Entity relationship model

This is the actual production architecture the schema is built against
(hierarchical multi-tenant fleet access, described by Rivian's platform
team), not a simplified textbook version:

| Entity pair | Relationship | Key characteristics |
|---|---|---|
| Tenant <-> Segment | One-to-many | A tenant (Rivian, Amazon, ...) owns multiple segments; a segment cannot be shared across two tenants. |
| Tenant <-> Asset (vehicle) | **Many-to-many** | A vehicle can belong to multiple tenants at once -- an EDV sold to Amazon is accessible by both Amazon (buyer) and Rivian (OEM), but strictly hidden from an unrelated third party. |
| Segment <-> Asset | Many-to-many | Segments group multiple assets; an asset can be linked to multiple segments, including dynamically via **rule-based segments**. |
| User/Role <-> Segment | Hierarchical | Access to a parent node (e.g. a region) recursively grants visibility to all child nodes and their connected vehicles. |

The consequence that most changes the schema: since a segment is
1-tenant-owned but an asset can have multiple tenants, `segmentAssignments`
has to be **tenant-scoped**, not global -- the same vehicle sits in Rivian's
internal fleet-health hierarchy *and* the fleet customer's operational
hierarchy simultaneously, as two independent array entries. See
[Requirements coverage](#requirements-coverage) below for exactly where
each relationship is proven live.

## Requirements coverage

| Req | Feature | Where | Result |
|---|---|---|---|
| REQ-01 | Single-pass authorization, no cross-DB fan-out | Notebook Part D | Correctness-verified (identical result sets) and **3.79x faster** (median 666ms vs 2525ms) than a simulated 2-round-trip fan-out, at real ~50K-document scale with a realistic ~6,600-doc candidate set -- no longer a toy-scale caveat |
| REQ-02 | Polymorphic schema across asset classes | Notebook Part B | 3 asset types (`vehicle`, `ev_charger`, `e_bike`), different attribute shapes, no migrations |
| REQ-03 | Hybrid keyword + vector search | Notebook Part E | Native `$rankFusion`, tenant/role filter applied inside each sub-pipeline |
| REQ-04 | Native Atlas auto-embedding via Voyage AI | Notebook Part C/E | `autoEmbed` index genuinely builds and queries server-side (voyage-4, 1024 dims) |
| REQ-05 | In-engine / integrated reranking | Notebook Part F | `rerank-2.5` demonstrably reorders top-5 results (not just relabels scores). Uses native server-side `$rerank` on MongoDB 8.3+ with Native Reranking enabled (verified on 9.0.0); falls back to the Voyage AI API automatically on older clusters (verified on 8.0.30) -- both paths tested live, notebook prints which one ran |
| REQ-06 | Multi-tenant assets (many-to-many) + tenant-scoped segments | Notebook Part D2 | `VIN_RIVIAN_001` verified visible to Rivian OEM + Acme (its buyer), invisible to Globex even though Globex uses the *identical role string* Acme uses internally |
| REQ-07 | Rule-based / dynamic segment membership | Notebook Part H | A `stateOfCharge < 20` rule segment evaluated live against 50K vehicles, tagging 2,950 matches with a new segment assignment in ~1.7s |
| REQ-08 | Hierarchical role-grant propagation at scale | Notebook Part I | Granting a role at `seg_california` propagated to 6,600 descendant assets via one `update_many` + `arrayFilters` call in ~1.4s -- the honest cost side of precomputing `authorizedRolesOrTeams` |
| REQ-09 | Tenant transfer as an ACID transaction | Notebook Part J | `tenantIds` update + segment reassignment + `tenant_transfer_events` log entry committed atomically in one multi-document transaction |
| REQ-10 | Faceted search backend (filter panel + fleet tree) | Notebook Parts K/L/M, `api/main.py`, `frontend/index.html` | `$facet` aggregation (numeric + categorical buckets), Atlas Search `autocomplete` VIN substring search, and a segment-tree rollup-count aggregation -- wired behind a FastAPI service and a real clickable UI, verified end-to-end with a headless-browser test, not just notebook cells |
| REQ-11 | Single-vehicle CRUD | `api/main.py`, `frontend/index.html` | `GET/PATCH/DELETE /vehicles/{vin}` + `POST /vehicles`, wired into the UI (click a VIN -> detail drawer with edit/delete; "+ Add Vehicle" -> create form). Create reuses `topology.compute_assignment` so a newly created vehicle's `segmentAssignments` are computed identically to the bulk generator, not hand-set. All four operations enforce the same tenant+role authorization as list/search (a 404, not just a 403, for both "doesn't exist" and "exists but you're not authorized"). Found and fixed 3 real bugs building this -- see "Data layer audit" below |

## Why `asset_segments` and `assets` are separate collections

Every query in this POC (Parts D-F) only ever reads `assets` -- `asset_segments`
is never joined at query time. That raises a fair question: if nothing joins
them, why not one denormalized collection?

Because `assets.segmentAssignments[].ancestorSegments` stores only **IDs**
(`"seg_hayward_team"`), never the segment's mutable, human-facing metadata
(display name, owner, status). That split is what buys two things a single
fully-denormalized collection can't:

1. Renaming or reparenting a segment is a **single-document write with zero
   writes to `assets`**, regardless of how many assets reference it. If
   segment names were inlined onto every asset instead of just an ID,
   renaming a region would require a fan-out update across every asset in
   it -- the real anti-pattern.
2. You can browse and manage the org tree itself -- including segments with
   zero assets currently assigned -- which a purely asset-denormalized model
   has no place to represent.

**Schema v2 addition:** `segmentAssignments` entries are now tenant-scoped
(`{tenantId, segmentId, ancestorSegments, authorizedRolesOrTeams}`), and
`authorizedRolesOrTeams` is no longer hand-set -- it's the **computed**
union of a new `grantedRoles` field (on `asset_segments`, the actual source
of truth for "which role is granted at this exact node") across a segment
and all its ancestors. This is what makes "access to a parent node
recursively grants visibility to all child nodes" literally true rather
than asserted: granting a role at `seg_california` and then querying
descendant assets is proven live in notebook Part I, including the honest
cost (a bulk update across every descendant asset, not free).

Notebook Part D includes a live demonstration, not just this assertion: a
path-prefix query against `asset_segments` for hierarchy browsing, followed
by an actual rename of a segment that a preceding/following byte-equality
check confirms touches 0 documents in `assets`, immediately followed by
re-running the REQ-01 authorization query to confirm it's completely
unaffected by the rename.

`segmentAssignments` is also an array specifically because the
asset-to-segment relationship is **many-to-many**, not one-to-many. Part D
demonstrates both directions live: many assets already map to one segment
(6+ assets under `seg_hayward_team`), and a seeded pool vehicle
(`VIN_RIVIAN_010`) maps to two segments simultaneously, proving a shared
asset is visible to *either* team's role independently (OR semantics), not
gated behind both.

## Schema design patterns applied (and one deliberately rejected)

This data model was cross-checked against *MongoDB Data Modeling and Schema
Design* (Coupal, Desmarets, Hoberman). Rather than treat that as a citation
exercise, each applicable pattern below was either already present, or was
added and verified live against the cluster:

- **Polymorphic / Inheritance Pattern** -- `assets.assetType` (`vehicle`,
  `ev_charger`, `e_bike`) determines which keys exist under `attributes`,
  in one collection, with no per-type migration. This is REQ-02, already
  built (Notebook Part B).
- **Tree Pattern (parent ref + ancestors array + materialized path)** --
  `asset_segments.hierarchy` stores `parentId`, `ancestors`, and `path`
  together, which is the book's recommended combination when you need both
  fast "all descendants" prefix queries (`path`) and fast "direct children"
  lookups (`parentId`) without a graph traversal. Already built.
- **Extended Reference Pattern, applied narrowly** --
  `assets.segmentAssignments[].ancestorSegments` copies segment **IDs**
  onto each asset (avoiding a join for REQ-01's authorization query) but
  deliberately does *not* copy the segment's display name, owner, or
  status. The book's own guidance for this pattern is to copy only fields
  that rarely change; segment names/owners do change (we demonstrate a live
  rename in Part D), so copying them would recreate the fan-out-on-update
  problem the pattern exists to avoid. Copying only the stable ID satisfies
  the pattern without the anti-pattern.
- **Attribute Pattern** -- added in this pass. `attributes` has a different
  key set per `assetType`, so `db.assets.create_index([("attributes.$**", 1)])`
  (a native MongoDB wildcard index) covers ad hoc filtering on *any*
  attribute -- present or added by a future asset type -- without hand
  -maintaining one single-field index per attribute per type. Notebook Part
  C1b proves it's actually used, not just created: the same query
  (`attributes.connectorType: "CCS1"`) is run through `explain()` before
  the index exists (`COLLSCAN`) and after (`IXSCAN` on
  `attributes_wildcard_idx`).
- **Schema Versioning Pattern** -- added in this pass. Every seed document
  now carries `schemaVersion: 1`. Cheap now, and the book is blunt that
  schema evolution ("not a matter of if, but when") is much easier to
  handle from day one than to retrofit after the field is missing on
  millions of existing documents.
- **Single Collection Pattern -- considered, rejected.** The book's own
  criterion for this pattern is when an application needs frequent,
  low-latency queries that span multiple entity types together. That's not
  this use case: REQ-01's authorization query only ever reads `assets`
  (segment metadata is never joined at query time, see below), so merging
  `asset_segments` and `assets` into one collection would add complexity
  (a `docType` discriminator, mixed indexes) for a join that never happens.

## Data layer audit: gotchas and optimizations found by testing, not review

Everything below was found by actually running `explain()` against the live
50K-document collection and measuring real numbers -- not a paper schema
review. Each one is demonstrated live in the notebook (Parts C1, C1a-note,
C5, D3).

### Gotchas (things that would silently break or reject data)

1. **MongoDB rejects a compound index across two different array fields.**
   `tenantIds` and `segmentAssignments` are both arrays on the same asset
   document. `db.assets.create_index([("tenantIds", 1), ("segmentAssignments.tenantId", 1)])`
   creates fine (Mongo doesn't know the data shape yet), but inserting a
   document where *both* arrays have more than one element fails with
   `cannot index parallel arrays [segmentAssignments] [tenantIds]`
   (verified live, notebook Part C1). The fix: `tenantIds` gets its own
   single-field index; the authorization-query index compounds
   `segmentAssignments.tenantId` + `segmentAssignments.authorizedRolesOrTeams`
   together instead (fine -- same array).
2. **Atlas Search `embeddedDocuments` filters need `equals`, not `text`, for
   `token`-type fields.** Filtering inside an `embeddedDocument` operator
   (the search-index equivalent of `$elemMatch`) using `{"text": {"query":
   ..., "path": "segmentAssignments.tenantId"}}` against a `token`-typed
   field **silently returns zero results** -- no error, it just doesn't
   match. Switching to `{"equals": {"value": ..., "path": ...}}` fixes it.
   This isn't clearly documented anywhere; found by bisecting a hybrid
   search query that returned 10 results with a plain `$elemMatch` `find()`
   but 0 results through `$search`.
3. **`_id` and the vehicle's real VIN are different values, and confusing
   them broke every single-vehicle endpoint.** `assets._id` is an internal
   document key (`VIN_SCALE_000001`, `VIN_RIVIAN_001`); `attributes.vin` is
   the vehicle's actual VIN (`7FCEHEB...`), a separate value shown in the
   UI and clicked by the user. The first version of
   `GET/PATCH/DELETE /vehicles/{vin}` looked up by `_id` -- every one of
   them 404'd on every vehicle, since the value the frontend actually sends
   is `attributes.vin`. Caught immediately by testing the click-through
   flow with Playwright (not by code review, which didn't flag it because
   both fields are called "vin"-ish and the list/search endpoints already
   happened to display `attributes.vin` correctly via an unrelated dict-
   unpacking quirk). Fixed by making every single-vehicle endpoint query
   `attributes.vin` explicitly, and added a partial unique index on it
   (see below) since nothing previously enforced VIN uniqueness at all.
4. **`/vehicles` had no `assetType` filter, so it silently returned
   `ev_charger`/`e_bike` documents mixed into the vehicle table.** `assets`
   is polymorphic (REQ-02); the "Vehicle Tracker" endpoint needs to scope
   to `assetType: "vehicle"` explicitly, not rely on every doc happening to
   look like a vehicle. Found the same way as #3 -- clicking a charger's
   "VIN" (actually its `_id`, since chargers have no `attributes.vin`)
   404'd. Fixed by adding the filter to the shared `auth_filter()` helper
   so every endpoint built on it (list, facets, search, detail/update/
   delete) inherits it in one place, plus the segment-tree rollup-count
   aggregation (which has its own separate pipeline).
5. **Atlas Search `autocomplete` only guarantees correctness for queries up
   to its own `maxGrams` setting.** Pasting a *full* 17-character VIN into
   the search box (rather than a short partial string) returned dozens of
   unrelated vehicles, every one scored **identically** -- the operator
   fragments a query longer than `maxGrams` (7 here) into its own 3-7
   character grams internally and matches any document sharing *any* one
   of them, not the literal full string. Every synthetic VIN in this
   dataset also shares a literal 7-character prefix (realistic -- real
   VINs share a manufacturer WMI code across a whole fleet too), which
   made the false-positive rate especially bad: searching a full VIN could
   match most of a tenant's fleet, and the actual match sometimes didn't
   even appear in the first 500 candidates fetched by relevance.
   Demonstrated live (notebook Part L): a 6-character substring query
   returns 10/10 genuine matches; the same vehicle's full VIN returns 10
   candidates with 1 genuine match. Fixed with a length-based hybrid in
   `/vehicles/search`: `autocomplete` for queries <= `maxGrams` (fast,
   verified correct), exact case-insensitive regex within the same
   auth-filtered candidate set used everywhere else in this API for
   anything longer (guaranteed correct, ~50-320ms at this data volume --
   no search-index tuning needed since the candidate set is already
   narrowed to one tenant by the existing operational index).

### Optimizations found by measuring, not guessing

3. **The operational auth index had a dead trailing field.** The original
   3-field index ended in `attributes.make` -- but every vehicle's make is
   `"RIVIAN"` (Rivian is the only OEM here), so that field contributed zero
   selectivity while still being maintained on every write. Dropped it down
   to a clean 2-field `segment_auth_idx`. Measured impact of the *real*
   query patterns the API runs: a narrow team-level role (~500-vehicle
   candidate set) gets a ~5x overscan ratio from this index; a broad
   tenant-wide admin role (~10,000 candidates) can't do meaningfully better
   with *any* index once arbitrary attribute filters are layered on top --
   that's inherent to faceted/multi-attribute filtering (10 optional filter
   fields don't compose into one compound index without a combinatorial
   explosion), not a fixable index problem at this data volume.
4. **`asset_segments` and `tenant_transfer_events` had zero indexes beyond
   `_id`.** Invisible at ~100 segments and a handful of transfer events
   (both fully scan in a few milliseconds regardless), but confirmed live
   via `explain()` to be `COLLSCAN`s for every hierarchy-browsing query and
   every audit-log lookup. Added `segment_tenant_idx`, `segment_path_idx`
   on `asset_segments`, and `transfer_asset_history_idx`,
   `transfer_to_tenant_idx` on `tenant_transfer_events` -- the correct
   baseline for collections outside the main hot-path table, which are
   easy to overlook precisely because they don't show up in day-to-day
   query latency until they do.
5. **Pagination: `skip/limit` cost grows with page depth; measured, not
   assumed.** At page 399 (skip=9,950) against a ~10,000-doc candidate set,
   `skip/limit` took ~101ms vs. ~50ms for page 1 -- a real, if modest,
   growth trend that would compound badly at millions of documents or much
   deeper pagination. Added `afterId` range/keyset pagination as an
   alternative on `/vehicles` (`_id > afterId`, sorted by `_id`), which
   measured flat (~42-52ms) regardless of depth -- at the cost of losing
   direct "jump to page N" navigation, which is why the reference UI still
   uses page-number pagination and `afterId` is offered as the scalable
   option for programmatic consumers.
6. **`schemaVersion` was inconsistently applied.** Segments/tenants created
   programmatically (`scripts/topology.py`) didn't get a `schemaVersion`
   field at all, while the curated fixture had a stale value left over from
   an earlier migration. Centralized it as a single `SCHEMA_VERSION`
   constant in `topology.py`, applied via `materialize_segment_tree()` and
   the `TENANTS` list, so it can't drift between the programmatic and
   curated paths again.
7. **VIN uniqueness was never enforced.** A real VIN is a legally unique
   identifier; nothing in this schema said so. Added a **partial unique
   index** on `attributes.vin` (`partialFilterExpression: {assetType:
   "vehicle"}`) -- partial because `ev_charger`/`e_bike` docs don't have a
   `vin` field at all, so a plain unique index would need every non-vehicle
   document to somehow also satisfy a constraint on a field it doesn't
   have. Verified live: inserting a second vehicle with an already-used VIN
   is correctly rejected (`E11000 duplicate key error`); two `ev_charger`
   docs with no `vin` field at all both insert fine, confirming the
   partial filter correctly excludes them from the constraint.

### Patterns already correctly applied, confirmed on review

- **Subset Pattern**: `/vehicles`' list query projects only `{_id,
  attributes}`, never `segmentAssignments` -- the list view doesn't need
  the full authorization/hierarchy payload per row, just the display
  fields. Already true before this review; confirmed as correct rather
  than an oversight.
- **Computed Pattern**: `authorizedRolesOrTeams` (see "Schema design
  patterns" below) remains the main computed field; no new candidates for
  this pattern emerged from the review.

### Considered, not implemented (documented rather than built)

- **Bucket / Time Series Pattern for telemetry history.** This schema only
  stores each vehicle's *current* telemetry snapshot (`stateOfCharge`,
  `mileage`, `vehicleSpeed`, etc.) -- there's no history of how those
  values changed over time. A real fleet-telemetry system would almost
  certainly want a native MongoDB **time series collection** for that
  (readings ingested continuously, bucketed internally by time), kept
  separate from the "current state" snapshot on the asset document. Out of
  scope for this pass (would need a redesign plus synthetic historical
  data with no additional schema-design signal to prove), but worth
  flagging as the obvious next real gap if this went to production.

## Repo layout

```
├── spec.md                        # original technical spec this POC validates
├── notebooks/
│   └── amp_mongodb_poc.ipynb      # the executable, Colab-shareable demo (start here)
├── data/
│   ├── segments_seed.json         # curated asset_segments fixture (rivian_oem + 2 fleet-customer tenants)
│   ├── assets_seed.json           # 19 curated polymorphic assets, incl. deliberate leak-test decoys
│   └── generated/                 # gitignored -- 50K-vehicle cache from scripts/generate_fleet_data.py
├── scripts/
│   ├── topology.py                # shared tenant/segment/authorization-closure logic;
│   │                              #   embedded verbatim into the notebook (Part A3) so it's
│   │                              #   self-contained without the repo checked out
│   ├── generate_fleet_data.py     # generates + bulk-inserts the ~50K-vehicle scale dataset
│   ├── seed.py                    # CLI seed script for the curated fixture, reads data/*.json
│   ├── create_indexes.py          # CLI index setup (mirrors notebook Part C)
│   ├── build_notebook.py          # generates the .ipynb; seed data is loaded from data/*.json
│   │                              #   and topology.py at generation time (not duplicated by
│   │                              #   hand), so the CLI scripts and notebook stay identical
│   └── execute_notebook.py        # runs the .ipynb end-to-end and saves outputs (dev tool)
├── api/
│   └── main.py                    # small FastAPI service exposing the notebook's queries as
│                                  #   real HTTP JSON endpoints (/vehicles, /facets,
│                                  #   /segments/tree, /vehicles/search) -- see below
├── frontend/
│   └── index.html                 # single-page vanilla-JS reference UI, served by api/main.py
│                                  #   at "/" -- table, search, filter panel, fleet tree
├── .env.example
└── LICENSE
```

## Running it

### Option A: Google Colab (recommended, no local setup)
1. Click the "Open in Colab" badge above.
2. Add two Colab Secrets (key icon in the left sidebar): `MONGODB_URI` and
   `VOYAGE_API_KEY`.
3. Run all cells top to bottom.

Requires an Atlas cluster with Atlas Search + Vector Search enabled and a
[Voyage AI](https://www.voyageai.com/) API key. REQ-01 through REQ-04 were
validated on a free M0 cluster. REQ-05's native `$rerank` path additionally
requires MongoDB 8.3+ and Native Reranking enabled in Atlas Project
Settings; on older clusters the notebook automatically falls back to the
Voyage AI rerank API with an identical result.

### Option B: Local / CI
```bash
cp .env.example .env   # fill in MONGODB_URI and VOYAGE_API_KEY
pip install -r <(python3 -c "print('pymongo[srv]\npandas\nvoyageai\ncertifi\npython-dotenv\nnbformat\nnbclient\nipykernel')")
python scripts/seed.py                 # curated 19-doc fixture (+ 6 tenants, small hierarchies)
python scripts/generate_fleet_data.py  # ~50,000 synthetic vehicles across 5 fleet customers
python scripts/create_indexes.py       # operational + Atlas Search/Vector/autocomplete indexes
```

### Option C: Faceted-search API + reference UI

A small FastAPI service (`api/main.py`) exposes the Part K/L/M queries as
real HTTP JSON endpoints, and serves a single-page vanilla-JS UI
(`frontend/index.html`) that actually calls them -- a real, clickable demo,
not just notebook cells or curl output. It approximates the reference
fleet-portal screenshots **functionally** (same table columns, filter
panel, fleet-selection tree with rollup counts) -- it is not a
pixel-accurate clone of any product's design system.

```bash
pip install fastapi uvicorn
python3 -m uvicorn api.main:app --reload --port 8000
# (use `python3 -m uvicorn`, not bare `uvicorn`, if pip installed its
# console script somewhere not on your PATH)
```

Open **http://localhost:8000** for the UI (served same-origin, no CORS
needed), or hit the API directly:

```bash
curl "http://localhost:8000/tenants"
curl "http://localhost:8000/tenants/amazon_logistics/roles"
curl "http://localhost:8000/vehicles?tenant=amazon_logistics&role=role_fleet_admin&page=1&pageSize=10"
curl "http://localhost:8000/vehicles?tenant=amazon_logistics&role=role_fleet_admin&segment=seg_amazon_logistics_region0_depot1"
curl "http://localhost:8000/vehicles?tenant=amazon_logistics&role=role_fleet_admin&pageSize=10&afterId=VIN_SCALE_000010"  # keyset pagination, see "Data layer audit" below
curl "http://localhost:8000/facets?tenant=amazon_logistics&role=role_fleet_admin"
curl "http://localhost:8000/segments/tree?tenant=amazon_logistics"
curl "http://localhost:8000/vehicles/search?vin=6493&tenant=amazon_logistics&role=role_fleet_admin"

# Single-vehicle CRUD (REQ-11) -- also wired into the UI (click a VIN)
curl "http://localhost:8000/vehicles/7FCEHEB.../?tenant=amazon_logistics&role=role_fleet_admin"   # Read
curl -X PATCH "http://localhost:8000/vehicles/7FCEHEB...?tenant=amazon_logistics&role=role_fleet_admin" \
  -H "Content-Type: application/json" -d '{"chargingStatus": "Ready to Charge", "mileage": 15000}'
curl -X DELETE "http://localhost:8000/vehicles/7FCEHEB...?tenant=amazon_logistics&role=role_fleet_admin"
curl -X POST "http://localhost:8000/vehicles" -H "Content-Type: application/json" \
  -d '{"tenant":"amazon_logistics","role":"role_fleet_admin","model":"RPV","segmentId":"seg_amazon_logistics_region0_depot1_team0"}'
```

`tenant`/`role` are plain query params standing in for what a real
deployment would pull from an authenticated session/JWT -- there's no auth
system here, the point is proving the MongoDB query patterns work behind a
real API. The UI's tenant/role selectors are the same stand-in, made
explicit in a banner rather than hidden.

The UI was verified end-to-end with a headless-browser test (Playwright) --
tenant/role switching, fleet-tree drill-down, filter clicks, VIN search, and
pagination all exercised against the live 50K-document API, not just
loaded and eyeballed. That test caught a real bug: `role_fleet_admin` was
scoped narrowly (only granted on one safety-hold segment) for the two
curated-fixture tenants, while it's a broad tenant-wide role for the 3
generated-scale tenants -- same role name, inconsistent meaning, which
showed up as "1 vehicle" instead of "10,000+" when switching tenants in
the UI. Fixed by granting `role_fleet_admin` at the curated tenants' global
root too, then recomputing every affected `authorizedRolesOrTeams` closure.

The CRUD flow (click VIN -> read detail -> edit + save -> reopen to confirm
persistence -> delete -> confirm it's gone from search -> create a new one)
was verified the same way, end-to-end with Playwright, and caught 3 more
real bugs -- see "Data layer audit" above for what they were and how they
were fixed. `POST /vehicles` enforces the same authorization a read would:
the creating role must actually be granted (directly or via ancestor
segments) for the segment the new vehicle is being assigned into, not just
"granted somewhere in the tenant."

## Data model notes

`data/assets_seed.json` deliberately includes three "trap" documents used to
prove security filtering actually works, not just that queries compile:

- `VIN_RIVIAN_009` -- admin-only asset. Modeled as a dedicated
  `seg_ca_north_restricted` segment (`role_fleet_admin` only) that is a
  **sibling** of `seg_california_north` (parented directly under
  `seg_global`), not a child of it. This matters: hierarchical access here
  is monotonic (a child can only add grants on top of its ancestors', never
  subtract one), so nesting a "restricted" segment under the region it was
  pulled from would still inherit `region_california_north` from that
  ancestor and silently un-restrict it -- a bug caught by actually running
  the computed-closure logic against this fixture, not by inspection.
- `VIN_GLOBEX_001` -- a *different tenant* (`globex_logistics`) whose
  `authorizedRolesOrTeams` array happens to contain the exact same role
  string (`region_california_north`) used elsewhere for `acme_fleet_corp`.
  This specifically catches an authorization filter that checks role but
  forgets tenant.

Every query in the notebook asserts these are excluded from results, not
just prints output for a human to eyeball.

`VIN_RIVIAN_010` is the opposite kind of test case: a pool vehicle
deliberately assigned to **two** segments at once (`seg_hayward_team` and
`seg_san_jose_team`), proving `segmentAssignments`/`authorizedRolesOrTeams`
are genuinely many-to-many, not one-to-many -- an asset can belong to
multiple teams simultaneously (shared/pooled equipment), and membership in
*either* team's role is sufficient for access (`$in` is OR, not AND). The
notebook asserts a user with only `team_hayward` and a separate user with
only `team_san_jose` **both** see it, and a `team_austin` user does not.

## Known limitations of this POC

- The "fan-out" benchmark in REQ-01 simulates the 2-round-trip pattern by
  issuing two queries against the *same* MongoDB cluster (there's no real
  PostgreSQL in this environment). It correctly isolates the cost of one
  extra network round trip + app-layer `$in` assembly, but understates what
  a true cross-database (different systems, connection pools, serialization
  formats) fan-out would cost at production scale. That said, the benchmark
  now runs against ~50,000 real documents with a realistic ~6,600-doc
  candidate set (3.79x speedup), so it's no longer a toy-scale caveat --
  just a same-cluster-on-both-sides one.
- The hybrid-search/rerank demos (Parts E/F) stay on the small 19-doc
  curated fixture, not the full 50K -- generated vehicles deliberately don't
  get `unstructuredNotes`/embeddings, since embedding 50K docs via Voyage
  would cost real time/money for no additional demo signal. 19 documents is
  enough to demonstrate correctness and relative ordering effects, not to
  make statistically rigorous precision/recall claims at scale.
- Generated VINs (`scripts/generate_fleet_data.py`) loosely mimic real
  17-character VIN shape for search/autocomplete demos; they are not valid
  check-digit VINs.
- The `/facets` API endpoint scopes by tenant+role+segment, but not by the
  *other* active categorical/range filters simultaneously (e.g. "facet
  counts for Ready-to-Charge vehicles only") -- a real product would likely
  fold the active filter set into the `$match` stage before `$facet`, which
  this POC's aggregation already supports mechanically, it's just not
  exposed as a query param yet.
- `frontend/index.html` matches the reference screenshots **functionally**
  (same columns, same filter categories, same fleet-tree rollup-count
  behavior) using a generic clean style -- it does not replicate Rivian's
  actual design system (fonts, exact spacing/icons/colors). That was a
  deliberate scope call, not an oversight.
- There's no auth system in the API/UI -- `tenant`/`role` are plain,
  unvalidated query params/dropdowns anyone can set to anything. A real
  deployment would derive these from an authenticated session/JWT and would
  never trust a client-supplied role.
- Atlas's search index management control plane occasionally returns a
  transient error under heavy index create/drop churn
  (`Error connecting to Search Index Management service`); the notebook and
  scripts retry automatically, but if you see this once and it's the first
  time creating indexes on a fresh cluster, just re-run the cell.
