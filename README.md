# AMP on MongoDB Atlas: Unified Multi-Tenant, Vector & Hybrid Search POC

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/munish-mongodb/amp-mongodb-unified-search-poc/blob/main/notebooks/amp_mongodb_poc.ipynb)

This is an executable proof-of-concept for [`spec.md`](spec.md)
(SPEC-001-AMP-MONGO): can a single MongoDB Atlas cluster replace a
split-brain MongoDB + PostgreSQL architecture for an OEM asset management
platform (vehicles, EV chargers, e-bikes), handling polymorphic asset data,
hierarchical multi-tenant authorization, and hybrid keyword/vector search
in one query pass?

Every claim below was verified by actually running the code against a live
Atlas cluster (MongoDB 8.0.30) -- nothing here is illustrative pseudocode.
Open `notebooks/amp_mongodb_poc.ipynb` in Colab to run it yourself.

## Spec vs. reality

The original spec sketched two capabilities using syntax that turned out not
to match what's actually shipped. Building against a live cluster surfaced
the real behavior:

| Spec said | Reality (verified live) |
|---|---|
| `autoEmbed` as a `mappings.fields` entry, model `voyage-3` | `autoEmbed` is real, but it's a top-level `fields` array entry (`type: "autoEmbed"`, `modality: "text"`), and only supports the `voyage-4` model family -- `voyage-3.x` is rejected. Native server-side embedding **works** once you use the right shape. |
| `$rerank` aggregation stage | Not a real pipeline stage (`Unrecognized pipeline stage name: '$rerank'`). Reranking works today as a Voyage AI `.rerank()` API call layered on top of `$vectorSearch` results. |
| *(not mentioned)* | MongoDB ships a native **`$rankFusion`** stage that combines keyword (`$search`) and vector (`$vectorSearch`) sub-pipelines with reciprocal rank fusion in a single aggregation call -- a better hybrid-search primitive than the spec assumed existed. |

## Requirements coverage

| Req | Feature | Where | Result |
|---|---|---|---|
| REQ-01 | Single-pass authorization, no cross-DB fan-out | Notebook Part D | Correctness-verified (identical result sets) and ~1.6-1.7x faster than a simulated 2-round-trip fan-out, at toy (18-doc) scale |
| REQ-02 | Polymorphic schema across asset classes | Notebook Part B | 3 asset types (`vehicle`, `ev_charger`, `e_bike`), different attribute shapes, no migrations |
| REQ-03 | Hybrid keyword + vector search | Notebook Part E | Native `$rankFusion`, tenant/role filter applied inside each sub-pipeline |
| REQ-04 | Native Atlas auto-embedding via Voyage AI | Notebook Part C/E | `autoEmbed` index genuinely builds and queries server-side (voyage-4, 1024 dims) |
| REQ-05 | In-engine / integrated reranking | Notebook Part F | Voyage `rerank-2.5` demonstrably reorders top-5 results (not just relabels scores) |

## Repo layout

```
├── spec.md                        # original technical spec this POC validates
├── notebooks/
│   └── amp_mongodb_poc.ipynb      # the executable, Colab-shareable demo (start here)
├── data/
│   ├── segments_seed.json         # asset_segments hierarchy (2 tenants, multi-level)
│   └── assets_seed.json           # 18 polymorphic assets, incl. deliberate leak-test decoys
├── scripts/
│   ├── seed.py                    # CLI seed script (mirrors notebook Part B)
│   ├── create_indexes.py          # CLI index setup (mirrors notebook Part C)
│   ├── build_notebook.py          # generates the .ipynb from validated code (dev tool)
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

Requires an Atlas cluster with Atlas Search + Vector Search enabled (a free
M0 cluster is sufficient -- that's what this POC was validated against) and
a [Voyage AI](https://www.voyageai.com/) API key.

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

## Known limitations of this POC

- The "fan-out" benchmark in REQ-01 simulates the 2-round-trip pattern by
  issuing two queries against the *same* MongoDB cluster (there's no real
  PostgreSQL in this environment). It correctly isolates the cost of one
  extra network round trip + app-layer `$in` assembly, but understates what
  a true cross-database (different systems, connection pools, serialization
  formats) fan-out would cost at production scale (~10,000 resolved IDs, per
  the spec's own numbers).
- 18 seed documents is enough to demonstrate correctness and relative
  ordering effects (hybrid search, reranking), not to make statistically
  rigorous precision/recall claims at production data volumes.
- Atlas's search index management control plane occasionally returns a
  transient error under heavy index create/drop churn
  (`Error connecting to Search Index Management service`); the notebook and
  scripts retry automatically, but if you see this once and it's the first
  time creating indexes on a fresh cluster, just re-run the cell.
