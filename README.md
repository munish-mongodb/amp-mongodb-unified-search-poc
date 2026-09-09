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

## Two real "gotchas" found building this at scale

Both surfaced by actually running against a live 50K-document collection,
not from documentation:

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
curl "http://localhost:8000/facets?tenant=amazon_logistics&role=role_fleet_admin"
curl "http://localhost:8000/segments/tree?tenant=amazon_logistics"
curl "http://localhost:8000/vehicles/search?vin=6493&tenant=amazon_logistics&role=role_fleet_admin"
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
