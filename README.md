# AMP on MongoDB Atlas: Unified Multi-Tenant, Vector & Hybrid Search POC

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/munish-mongodb/amp-mongodb-unified-search-poc/blob/main/notebooks/amp_mongodb_poc.ipynb)

This is an executable proof-of-concept for [`spec.md`](spec.md)
(SPEC-001-AMP-MONGO): can a single MongoDB Atlas cluster replace a
split-brain MongoDB + PostgreSQL architecture for an OEM asset management
platform (vehicles, EV chargers, e-bikes), handling polymorphic asset data,
hierarchical multi-tenant authorization, and hybrid keyword/vector search
in one query pass?

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

## Requirements coverage

| Req | Feature | Where | Result |
|---|---|---|---|
| REQ-01 | Single-pass authorization, no cross-DB fan-out | Notebook Part D | Correctness-verified (identical result sets) and ~1.6-1.7x faster than a simulated 2-round-trip fan-out, at toy (19-doc) scale |
| REQ-02 | Polymorphic schema across asset classes | Notebook Part B | 3 asset types (`vehicle`, `ev_charger`, `e_bike`), different attribute shapes, no migrations |
| REQ-03 | Hybrid keyword + vector search | Notebook Part E | Native `$rankFusion`, tenant/role filter applied inside each sub-pipeline |
| REQ-04 | Native Atlas auto-embedding via Voyage AI | Notebook Part C/E | `autoEmbed` index genuinely builds and queries server-side (voyage-4, 1024 dims) |
| REQ-05 | In-engine / integrated reranking | Notebook Part F | `rerank-2.5` demonstrably reorders top-5 results (not just relabels scores). Uses native server-side `$rerank` on MongoDB 8.3+ with Native Reranking enabled (verified on 9.0.0); falls back to the Voyage AI API automatically on older clusters (verified on 8.0.30) -- both paths tested live, notebook prints which one ran |

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

## Why `asset_segments` and `assets` are separate collections

```
├── spec.md                        # original technical spec this POC validates
├── notebooks/
│   └── amp_mongodb_poc.ipynb      # the executable, Colab-shareable demo (start here)
├── data/
│   ├── segments_seed.json         # asset_segments hierarchy (2 tenants, multi-level)
│   └── assets_seed.json           # 19 polymorphic assets, incl. deliberate leak-test decoys
├── scripts/
│   ├── seed.py                    # CLI seed script, reads data/*.json
│   ├── create_indexes.py          # CLI index setup (mirrors notebook Part C)
│   ├── build_notebook.py          # generates the .ipynb; Part B's seed data is
│   │                              #   loaded from data/*.json at generation time
│   │                              #   (not duplicated by hand), so seed.py and the
│   │                              #   notebook are guaranteed to seed identical data
│   └── execute_notebook.py        # runs the .ipynb end-to-end and saves outputs (dev tool)
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
python scripts/seed.py
python scripts/create_indexes.py
```

## Data model notes

`data/assets_seed.json` deliberately includes three "trap" documents used to
prove security filtering actually works, not just that queries compile:

- `VIN_RIVIAN_009` -- admin-only asset (`role_fleet_admin`), semantically the
  single most relevant document for the demo query, and must be excluded for
  a regular regional-manager role.
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
  formats) fan-out would cost at production scale (~10,000 resolved IDs, per
  the spec's own numbers).
- 19 seed documents is enough to demonstrate correctness and relative
  ordering effects (hybrid search, reranking), not to make statistically
  rigorous precision/recall claims at production data volumes.
- Atlas's search index management control plane occasionally returns a
  transient error under heavy index create/drop churn
  (`Error connecting to Search Index Management service`); the notebook and
  scripts retry automatically, but if you see this once and it's the first
  time creating indexes on a fresh cluster, just re-run the cell.
