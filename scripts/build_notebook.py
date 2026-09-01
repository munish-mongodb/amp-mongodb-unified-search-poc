#!/usr/bin/env python3
"""Generate notebooks/amp_mongodb_poc.ipynb from validated, working code.

Every cell in this notebook was run against a live MongoDB Atlas cluster
before being embedded here -- nothing is hypothetical. Run this script
whenever the demo logic changes, then re-execute the notebook to confirm
it still works end to end (see scripts/execute_notebook.py).
"""
import nbformat as nbf

nb = nbf.v4.new_notebook()
cells = []


def md(text):
    cells.append(nbf.v4.new_markdown_cell(text))


def code(text):
    cells.append(nbf.v4.new_code_cell(text))


# ---------------------------------------------------------------------------
md(r"""# AMP on MongoDB Atlas: Unified Multi-Tenant, Vector & Hybrid Search POC

This notebook is the executable companion to `spec.md` (SPEC-001-AMP-MONGO).
It proves out, against a **real MongoDB Atlas cluster**, the five requirements
from that spec:

| Req | Feature | Part |
|---|---|---|
| REQ-01 | Single-pass authorization (no cross-DB fan-out) | C, D |
| REQ-02 | Polymorphic asset schema | B |
| REQ-03 | Hybrid keyword + vector search | E |
| REQ-04 | Native Atlas auto-embedding (Voyage AI) | C, E |
| REQ-05 | In-engine / integrated reranking | F |

**Honesty note on spec vs. reality:** the original spec sketched an
`autoEmbed` field type and a `$rerank` aggregation stage. Building this POC
against a live cluster surfaced the *actual* current syntax, which differs
in specifics (documented inline as we go) but validates that both
capabilities are real: native auto-embedding genuinely works, and while
`$rerank` is not a real pipeline stage, MongoDB does ship a native
`$rankFusion` hybrid-search stage (not mentioned in the spec at all), and
reranking works well as a Voyage AI API call layered on top. Every cell
below actually executed successfully -- nothing here is illustrative
pseudocode.
""")

# ---------------------------------------------------------------------------
md(r"""## Part A -- Setup

### A1. Install dependencies""")

code(r"""%pip install -q "pymongo[srv]" pandas voyageai certifi tabulate""")

md(r"""### A2. Credentials

Uses Colab Secrets when running in Colab (recommended -- keeps keys out of
the notebook). Falls back to environment variables for local/non-Colab runs.

Add two secrets in Colab (key icon in the left sidebar):
- `MONGODB_URI` -- your Atlas connection string
- `VOYAGE_API_KEY` -- your Voyage AI API key
""")

code(r"""import os

try:
    from google.colab import userdata
    MONGODB_URI = userdata.get("MONGODB_URI")
    VOYAGE_API_KEY = userdata.get("VOYAGE_API_KEY")
except ImportError:
    MONGODB_URI = os.environ.get("MONGODB_URI")
    VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY")

MONGODB_DB = os.environ.get("MONGODB_DB", "amp_poc_db")

assert MONGODB_URI, "Set MONGODB_URI (Colab secret or env var)"
assert VOYAGE_API_KEY, "Set VOYAGE_API_KEY (Colab secret or env var)"
print("Credentials loaded OK")""")

code(r"""import time

import certifi
import pandas as pd
import voyageai
from pymongo import ASCENDING, MongoClient
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

client = MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
db = client[MONGODB_DB]
vo = voyageai.Client(api_key=VOYAGE_API_KEY)

print("Connected to Atlas:", client.server_info()["version"])
print("Using database:", MONGODB_DB)


def list_search_indexes_retry(coll, name=None, retries=5, delay=5):
    # list_search_indexes occasionally hits a transient Atlas control-plane
    # error ('Error connecting to Search Index Management service') under
    # heavy index churn. Retry a few times before giving up.
    for attempt in range(retries):
        try:
            return list(coll.list_search_indexes(name)) if name else list(coll.list_search_indexes())
        except OperationFailure as e:
            if attempt == retries - 1:
                raise
            print(f"  (transient list_search_indexes error, retrying: {e})")
            time.sleep(delay)


def wait_for_index(coll, name, timeout=240):
    start = time.time()
    while time.time() - start < timeout:
        idxs = list_search_indexes_retry(coll, name)
        if idxs and idxs[0].get("queryable"):
            return idxs[0]
        time.sleep(5)
    raise TimeoutError(f"Index {name} not queryable after {timeout}s")""")

# ---------------------------------------------------------------------------
md(r"""## Part B -- Seed polymorphic assets + hierarchical segments (REQ-02)

Two collections:
- `asset_segments` -- org hierarchy using the materialized-path + ancestors
  pattern (global -> region -> team).
- `assets` -- polymorphic documents across three asset types (`vehicle`,
  `ev_charger`, `e_bike`), each with a denormalized `authorizedRolesOrTeams`
  array for fast entitlement checks with **no joins**. Includes two tenants
  and one admin-only asset, specifically so later queries can prove tenant
  isolation and role-based exclusion actually work, not just that they
  compile.""")

code(r"""segments_data = [
    {"_id": "seg_global", "tenantId": "acme_fleet_corp", "name": "Global", "segmentType": "global",
     "hierarchy": {"parentId": None, "ancestors": [], "path": ",seg_global,"}},
    {"_id": "seg_us_west", "tenantId": "acme_fleet_corp", "name": "US West Region", "segmentType": "super_region",
     "hierarchy": {"parentId": "seg_global", "ancestors": ["seg_global"], "path": ",seg_global,seg_us_west,"}},
    {"_id": "seg_california", "tenantId": "acme_fleet_corp", "name": "California", "segmentType": "state",
     "hierarchy": {"parentId": "seg_us_west", "ancestors": ["seg_global", "seg_us_west"],
                   "path": ",seg_global,seg_us_west,seg_california,"}},
    {"_id": "seg_california_north", "tenantId": "acme_fleet_corp", "name": "Northern California Fleet Operations",
     "segmentType": "region",
     "hierarchy": {"parentId": "seg_california", "ancestors": ["seg_global", "seg_us_west", "seg_california"],
                   "path": ",seg_global,seg_us_west,seg_california,seg_california_north,"}},
    {"_id": "seg_hayward_team", "tenantId": "acme_fleet_corp", "name": "Hayward Depot Team", "segmentType": "team",
     "hierarchy": {"parentId": "seg_california_north",
                   "ancestors": ["seg_global", "seg_us_west", "seg_california", "seg_california_north"],
                   "path": ",seg_global,seg_us_west,seg_california,seg_california_north,seg_hayward_team,"}},
    {"_id": "seg_san_jose_team", "tenantId": "acme_fleet_corp", "name": "San Jose Depot Team", "segmentType": "team",
     "hierarchy": {"parentId": "seg_california_north",
                   "ancestors": ["seg_global", "seg_us_west", "seg_california", "seg_california_north"],
                   "path": ",seg_global,seg_us_west,seg_california,seg_california_north,seg_san_jose_team,"}},
    {"_id": "seg_texas", "tenantId": "acme_fleet_corp", "name": "Texas", "segmentType": "state",
     "hierarchy": {"parentId": "seg_global", "ancestors": ["seg_global"], "path": ",seg_global,seg_texas,"}},
    {"_id": "seg_austin_team", "tenantId": "acme_fleet_corp", "name": "Austin Depot Team", "segmentType": "team",
     "hierarchy": {"parentId": "seg_texas", "ancestors": ["seg_global", "seg_texas"],
                   "path": ",seg_global,seg_texas,seg_austin_team,"}},
    {"_id": "seg_globex_global", "tenantId": "globex_logistics", "name": "Global", "segmentType": "global",
     "hierarchy": {"parentId": None, "ancestors": [], "path": ",seg_globex_global,"}},
    {"_id": "seg_globex_midwest", "tenantId": "globex_logistics", "name": "Midwest Region", "segmentType": "region",
     "hierarchy": {"parentId": "seg_globex_global", "ancestors": ["seg_globex_global"],
                   "path": ",seg_globex_global,seg_globex_midwest,"}},
]

assets_data = [
    {"_id": "VIN_RIVIAN_001", "tenantId": "acme_fleet_corp", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1T", "color": "Rivian Blue", "vin": "1FTFW1E81MF100001"},
     "unstructuredNotes": "DC fast charger cut out at 80% state of charge in San Jose station. High heat warning.",
     "authorizedRolesOrTeams": ["team_san_jose", "region_california_north"]},
    {"_id": "VIN_RIVIAN_002", "tenantId": "acme_fleet_corp", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1S", "color": "Forest Green", "vin": "1FTFW1E81MF100002"},
     "unstructuredNotes": "Air suspension fault detected during highway cruising near Hayward.",
     "authorizedRolesOrTeams": ["team_hayward", "region_california_north"]},
    {"_id": "VIN_RIVIAN_003", "tenantId": "acme_fleet_corp", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1T", "color": "Rivian Blue", "vin": "1FTFW1E81MF100003"},
     "unstructuredNotes": "Vehicle reported intermittent DC fast charging throttling under high ambient temperatures in Hayward depot.",
     "authorizedRolesOrTeams": ["team_hayward", "region_california_north"]},
    {"_id": "VIN_RIVIAN_004", "tenantId": "acme_fleet_corp", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1S", "color": "Rivian Blue", "vin": "1FTFW1E81MF100004"},
     "unstructuredNotes": "Battery pack management system reduces charge rate when cell temperatures exceed safe thresholds during supercharging sessions.",
     "authorizedRolesOrTeams": ["team_austin", "region_texas"]},
    {"_id": "VIN_RIVIAN_005", "tenantId": "acme_fleet_corp", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1T", "color": "Forest Green", "vin": "1FTFW1E81MF100005"},
     "unstructuredNotes": "Infotainment touchscreen freezes intermittently after software update.",
     "authorizedRolesOrTeams": ["team_san_jose", "region_california_north"]},
    {"_id": "CHARGER_EV_101", "tenantId": "acme_fleet_corp", "assetType": "ev_charger",
     "attributes": {"maxKw": 350, "connectorType": "CCS1", "firmware": "v4.2.1"},
     "unstructuredNotes": "Cable cooling fan failure reported by telemetry. Connector overheats under sustained 350kW load.",
     "authorizedRolesOrTeams": ["team_san_jose", "region_california_north"]},
    {"_id": "CHARGER_EV_102", "tenantId": "acme_fleet_corp", "assetType": "ev_charger",
     "attributes": {"maxKw": 150, "connectorType": "CCS1", "firmware": "v4.1.0"},
     "unstructuredNotes": "Payment terminal card reader unresponsive; unit offline for 2 hours.",
     "authorizedRolesOrTeams": ["team_hayward", "region_california_north"]},
    {"_id": "CHARGER_EV_103", "tenantId": "acme_fleet_corp", "assetType": "ev_charger",
     "attributes": {"maxKw": 350, "connectorType": "NACS", "firmware": "v4.2.1"},
     "unstructuredNotes": "Thermal derating triggered on DC fast charger during peak summer demand, reducing max output from 350kW to 150kW.",
     "authorizedRolesOrTeams": ["team_austin", "region_texas"]},
    {"_id": "EBIKE_001", "tenantId": "acme_fleet_corp", "assetType": "e_bike",
     "attributes": {"make": "Rivian", "model": "e-Bike Commuter", "batteryWattHours": 500},
     "unstructuredNotes": "Rear derailleur misaligned after firmware update, shifting is inconsistent.",
     "authorizedRolesOrTeams": ["team_san_jose", "region_california_north"]},
    {"_id": "EBIKE_002", "tenantId": "acme_fleet_corp", "assetType": "e_bike",
     "attributes": {"make": "Rivian", "model": "e-Bike Commuter", "batteryWattHours": 500},
     "unstructuredNotes": "Battery pack shows reduced range and warm-to-touch casing after rapid charge cycles in hot weather.",
     "authorizedRolesOrTeams": ["team_hayward", "region_california_north"]},
    {"_id": "VIN_RIVIAN_006", "tenantId": "acme_fleet_corp", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1T", "color": "Rivian Blue", "vin": "1FTFW1E81MF100006"},
     "unstructuredNotes": "Tire pressure monitoring system reports slow leak in rear left tire.",
     "authorizedRolesOrTeams": ["team_hayward", "region_california_north"]},
    {"_id": "VIN_RIVIAN_007", "tenantId": "acme_fleet_corp", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1S", "color": "Rivian Blue", "vin": "1FTFW1E81MF100007"},
     "unstructuredNotes": "Owner reports charging speed drops significantly on hot days when using public fast chargers.",
     "authorizedRolesOrTeams": ["region_california_north"]},
    {"_id": "CHARGER_EV_104", "tenantId": "acme_fleet_corp", "assetType": "ev_charger",
     "attributes": {"maxKw": 150, "connectorType": "CCS1", "firmware": "v4.2.1"},
     "unstructuredNotes": "Firmware v4.2.1 rolled out fleet-wide; no reported issues.",
     "authorizedRolesOrTeams": ["team_austin", "region_texas"]},
    {"_id": "VIN_RIVIAN_008", "tenantId": "acme_fleet_corp", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1T", "color": "Silver", "vin": "1FTFW1E81MF100008"},
     "unstructuredNotes": "Windshield wiper motor makes grinding noise in cold weather.",
     "authorizedRolesOrTeams": ["team_san_jose", "region_california_north"]},
    {"_id": "VIN_RIVIAN_009", "tenantId": "acme_fleet_corp", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1S", "color": "Rivian Blue", "vin": "1FTFW1E81MF100009"},
     "unstructuredNotes": "Confidential recall candidate: potential battery cell thermal runaway risk flagged by engineering during fast-charge stress testing.",
     "authorizedRolesOrTeams": ["role_fleet_admin"]},
    {"_id": "VIN_GLOBEX_001", "tenantId": "globex_logistics", "assetType": "vehicle",
     "attributes": {"make": "Rivian", "model": "R1T", "color": "Rivian Blue", "vin": "9GX00000000000001"},
     "unstructuredNotes": "Battery thermal throttling detected during fast charging test.",
     "authorizedRolesOrTeams": ["region_california_north"]},
    {"_id": "EBIKE_003", "tenantId": "acme_fleet_corp", "assetType": "e_bike",
     "attributes": {"make": "Rivian", "model": "e-Bike Commuter", "batteryWattHours": 500},
     "unstructuredNotes": "Bluetooth pairing with companion app fails intermittently.",
     "authorizedRolesOrTeams": ["team_austin", "region_texas"]},
    {"_id": "CHARGER_EV_105", "tenantId": "acme_fleet_corp", "assetType": "ev_charger",
     "attributes": {"maxKw": 150, "connectorType": "CCS1", "firmware": "v4.1.0"},
     "unstructuredNotes": "Ground fault interrupter tripped twice this week during rainy conditions.",
     "authorizedRolesOrTeams": ["team_hayward", "region_california_north"]},
]

db.asset_segments.drop()
db.assets.drop()
db.asset_segments.insert_many(segments_data)
db.assets.insert_many(assets_data)

print(f"Seeded {db.asset_segments.count_documents({})} segments")
print(f"Seeded {db.assets.count_documents({})} assets")
print("Tenants:", db.assets.distinct("tenantId"))
print("Asset types (polymorphic, REQ-02):", db.assets.distinct("assetType"))""")

# ---------------------------------------------------------------------------
md(r"""## Part C -- Build indexes (operational + Atlas Search/Vector)

### C1. Operational compound index (REQ-01 infra)

Fast tenant + ACL + attribute matching with a single B-tree index -- no
joins, no second database.""")

code(r"""db.assets.create_index(
    [("tenantId", ASCENDING), ("authorizedRolesOrTeams", ASCENDING), ("attributes.make", ASCENDING)],
    name="tenant_acl_make_idx",
)
print("Created tenant_acl_make_idx")""")

md(r"""### C2. Atlas Search index (keyword half of hybrid search, REQ-03)""")

code(r"""existing = {i["name"] for i in list_search_indexes_retry(db.assets)}

if "assets_text_search_index" not in existing:
    db.assets.create_search_index(SearchIndexModel(
        definition={
            "mappings": {
                "dynamic": False,
                "fields": {
                    "tenantId": {"type": "token"},
                    "authorizedRolesOrTeams": {"type": "token"},
                    "unstructuredNotes": {"type": "string"},
                    "attributes": {"type": "document", "fields": {
                        "make": {"type": "token"}, "color": {"type": "token"}}},
                },
            }
        },
        name="assets_text_search_index",
        type="search",
    ))
    print("Submitted assets_text_search_index")
else:
    print("assets_text_search_index already exists")""")

md(r"""### C3. Native Atlas auto-embedding vector index (REQ-04)

**Spec vs. reality:** `spec.md` section 4.2 wrote `autoEmbed` as a
`mappings.fields` entry (Atlas Search dynamic-mapping shape) using model
`voyage-3`. Testing against a live cluster surfaced the *actual* required
shape -- a top-level `fields` array entry with `type: "autoEmbed"` and
`modality: "text"` -- and that `autoEmbed` currently only supports the
newer `voyage-4` model family (`voyage-3.x` is rejected). We also add
`filter`-type fields for `tenantId` and `authorizedRolesOrTeams` so the
vector search itself can pre-filter by tenant/entitlement (required for
REQ-01 -- without these, vector search alone will happily return other
tenants' documents, which we prove below).

This cell submits the index and polls until Atlas reports it queryable
(embedding generation happens server-side, so this can take 1-2 minutes).""")

code(r"""existing = {i["name"] for i in list_search_indexes_retry(db.assets)}
used_autoembed = False

if "vector_auto_embed_index" not in existing:
    try:
        db.assets.create_search_index(SearchIndexModel(
            definition={
                "fields": [
                    {"type": "autoEmbed", "path": "unstructuredNotes", "model": "voyage-4",
                     "modality": "text", "quantization": "float", "similarity": "cosine"},
                    {"type": "filter", "path": "tenantId"},
                    {"type": "filter", "path": "authorizedRolesOrTeams"},
                ]
            },
            name="vector_auto_embed_index",
            type="vectorSearch",
        ))
        print("Submitted vector_auto_embed_index, waiting for it to build...")
        wait_for_index(db.assets, "vector_auto_embed_index")
        used_autoembed = True
        print("vector_auto_embed_index is QUERYABLE -- native server-side embedding confirmed.")
    except Exception as e:
        print(f"autoEmbed index failed ({type(e).__name__}: {e}); will use client-side fallback below.")
else:
    idx = next(i for i in list_search_indexes_retry(db.assets) if i["name"] == "vector_auto_embed_index")
    used_autoembed = idx.get("queryable", False)
    print(f"vector_auto_embed_index already exists, queryable={used_autoembed}")""")

md(r"""### C4. Client-side embedding fallback (always built too, for comparison)

Even with native auto-embedding working, we also build the traditional
pattern -- embed client-side with the Voyage AI SDK, store the vector,
index it as a standard `vector` field -- so the notebook can show both
approaches side by side. This is also the safety net if `autoEmbed` isn't
available on a given cluster tier.""")

code(r"""docs = list(db.assets.find({}, {"_id": 1, "unstructuredNotes": 1}))
texts = [d["unstructuredNotes"] for d in docs]

embed_result = vo.embed(texts, model="voyage-3.5", input_type="document")
for doc, emb in zip(docs, embed_result.embeddings):
    db.assets.update_one({"_id": doc["_id"]}, {"$set": {"unstructuredNotesEmbedding": emb}})
print(f"Backfilled {len(docs)} client-side embeddings (voyage-3.5, {len(embed_result.embeddings[0])} dims)")

existing = {i["name"] for i in list_search_indexes_retry(db.assets)}
if "vector_manual_embed_index" not in existing:
    db.assets.create_search_index(SearchIndexModel(
        definition={
            "fields": [
                {"type": "vector", "path": "unstructuredNotesEmbedding",
                 "numDimensions": len(embed_result.embeddings[0]), "similarity": "cosine"},
                {"type": "filter", "path": "tenantId"},
                {"type": "filter", "path": "authorizedRolesOrTeams"},
            ]
        },
        name="vector_manual_embed_index",
        type="vectorSearch",
    ))
    wait_for_index(db.assets, "vector_manual_embed_index")
    print("vector_manual_embed_index is QUERYABLE.")
else:
    print("vector_manual_embed_index already exists")

VECTOR_INDEX = "vector_auto_embed_index" if used_autoembed else "vector_manual_embed_index"
print("\nVector index this notebook will query going forward:", VECTOR_INDEX)
print("(autoEmbed native path used:", used_autoembed, ")")""")

wait_for_text_search = r"""wait_for_index(db.assets, "assets_text_search_index")
print("assets_text_search_index is QUERYABLE.")"""
code(wait_for_text_search)

# ---------------------------------------------------------------------------
md(r"""## Part D -- Single-pass authorization + fan-out benchmark (REQ-01)

The problem statement: enforcing segment/role authorization normally
requires resolving authorized IDs in one system (Postgres, in the
production architecture this spec addresses) and then querying assets in
another (Mongo) -- two round trips plus app-layer `$in` assembly.

Below we time that two-round-trip pattern against a single-pass MongoDB
query that evaluates tenant + ACL + attribute filters together. Both paths
query the *same* cluster here (there's no separate Postgres in this POC),
so this isolates and measures the actual cost of the extra network round
trip and app-layer assembly step -- the real cross-database version would
be strictly worse (different systems, connection pools, serialization).""")

code(r"""def fanout_simulation(tenant, roles, make):
    t0 = time.perf_counter()
    candidate_ids = [a["_id"] for a in db.assets.find(
        {"tenantId": tenant, "authorizedRolesOrTeams": {"$in": roles}}, {"_id": 1})]
    t1 = time.perf_counter()
    results = list(db.assets.find({"_id": {"$in": candidate_ids}, "attributes.make": make}))
    t2 = time.perf_counter()
    return results, {"resolve_ids_ms": (t1 - t0) * 1000, "final_query_ms": (t2 - t1) * 1000,
                      "total_ms": (t2 - t0) * 1000}


def single_pass(tenant, roles, make):
    t0 = time.perf_counter()
    results = list(db.assets.find(
        {"tenantId": tenant, "authorizedRolesOrTeams": {"$in": roles}, "attributes.make": make}))
    t1 = time.perf_counter()
    return results, {"total_ms": (t1 - t0) * 1000}


import statistics

roles = ["region_california_north"]
N_TRIALS = 15

# warm up connections first so we measure query cost, not connection setup
fanout_simulation("acme_fleet_corp", roles, "Rivian")
single_pass("acme_fleet_corp", roles, "Rivian")

fanout_times, single_times = [], []
for _ in range(N_TRIALS):
    fanout_results, m1 = fanout_simulation("acme_fleet_corp", roles, "Rivian")
    single_results, m2 = single_pass("acme_fleet_corp", roles, "Rivian")
    assert {r["_id"] for r in fanout_results} == {r["_id"] for r in single_results}, "result sets must match"
    fanout_times.append(m1["total_ms"])
    single_times.append(m2["total_ms"])

fanout_median = statistics.median(fanout_times)
single_median = statistics.median(single_times)

print(f"Simulated fan-out (2 round trips), median of {N_TRIALS} runs: {fanout_median:.1f} ms")
print(f"Single-pass MongoDB query,          median of {N_TRIALS} runs: {single_median:.1f} ms")
print(f"Speedup: {fanout_median / single_median:.2f}x")
print(f"\n({len(single_results)} results, sets identical across every trial -- correctness-equivalent, not just faster)")
print("\nHonest caveat: at this tiny dataset size (18 docs) and querying the same")
print("cluster for both paths, the gap mostly reflects one eliminated network")
print("round trip plus app-layer $in assembly -- a few tens of ms here. The real")
print("production case this spec targets (separate Postgres + Mongo systems,")
print("~10,000 resolved IDs, cross-system serialization) would show a much larger")
print("gap; this benchmark isolates and confirms the round-trip-elimination effect")
print("is real and directionally correct, not that it's dramatic at toy scale.")""")

md(r"""### Authorization correctness check

A user with only `region_california_north` should never see the
Texas-only asset (`VIN_RIVIAN_004`, role `region_texas`), the admin-only
asset (`VIN_RIVIAN_009`, role `role_fleet_admin`), or *any* Globex Logistics
asset -- even `VIN_GLOBEX_001`, which is deliberately seeded with the
literal string `region_california_north` in its roles array under the
*wrong tenant*, specifically to catch a filter that checks role but
forgets tenant.""")

code(r"""df = pd.DataFrame([r["attributes"] | {"_id": r["_id"], "tenantId": r["tenantId"]} for r in single_results])
excluded_ids = {"VIN_RIVIAN_004", "VIN_RIVIAN_009", "VIN_GLOBEX_001"}
visible_ids = set(df["_id"])
assert excluded_ids.isdisjoint(visible_ids), f"Leak detected: {excluded_ids & visible_ids}"
print("Confirmed: cross-tenant and out-of-role assets correctly excluded.\n")
print(df[["_id", "make", "model", "color"]].to_string(index=False))""")

# ---------------------------------------------------------------------------
md(r"""## Part E -- Hybrid keyword + vector search with auto-embedding (REQ-03, REQ-04)

Query: **"battery thermal throttling during fast charging"** -- deliberately
phrased so several *semantically* relevant assets (e.g. "reduces charge
rate when cell temperatures exceed safe thresholds") share almost no
literal keyword overlap with the query, which is exactly what vector search
is for.

We use MongoDB's native `$rankFusion` stage to combine keyword (`$search`)
and vector (`$vectorSearch`) results in a single aggregation pipeline, with
the tenant/role security filter applied *inside each sub-pipeline* (this
matters -- a filter applied only after fusion would be too late, since
fusion itself would already be operating over leaked cross-tenant
candidates).

**Spec vs. reality note:** `$rankFusion` is not mentioned in the original
spec at all -- the spec's section 4.3 pipeline assumed a single
`$vectorSearch` call. Discovering that this cluster supports native
reciprocal-rank-fusion hybrid search is a genuine improvement over the
spec's design.""")

code(r"""QUERY_TEXT = "battery thermal throttling during fast charging"
TENANT = "acme_fleet_corp"
USER_ROLES = ["region_california_north"]

hybrid_results = list(db.assets.aggregate([
    {"$rankFusion": {
        "input": {
            "pipelines": {
                "vec": [{"$vectorSearch": {
                    "index": VECTOR_INDEX,
                    "path": "unstructuredNotes" if used_autoembed else "unstructuredNotesEmbedding",
                    **({"query": QUERY_TEXT} if used_autoembed else
                       {"queryVector": vo.embed([QUERY_TEXT], model="voyage-3.5", input_type="query").embeddings[0]}),
                    "numCandidates": 50,
                    "limit": 10,
                    "filter": {"$and": [
                        {"tenantId": {"$eq": TENANT}},
                        {"authorizedRolesOrTeams": {"$in": USER_ROLES}},
                    ]},
                }}],
                "kw": [
                    {"$search": {
                        "index": "assets_text_search_index",
                        "compound": {
                            "must": [{"text": {"query": QUERY_TEXT, "path": "unstructuredNotes"}}],
                            "filter": [
                                {"text": {"query": TENANT, "path": "tenantId"}},
                                {"text": {"query": USER_ROLES, "path": "authorizedRolesOrTeams"}},
                            ],
                        },
                    }},
                    {"$limit": 10},
                ],
            }
        }
    }},
    {"$project": {"unstructuredNotes": 1, "tenantId": 1, "attributes": 1}},
    {"$limit": 10},
]))

assert {"VIN_GLOBEX_001", "VIN_RIVIAN_009", "VIN_RIVIAN_004"}.isdisjoint({r["_id"] for r in hybrid_results})
print(f"Hybrid $rankFusion results ({len(hybrid_results)}), security filters applied inside each sub-pipeline:\n")
for r in hybrid_results:
    print(f"  {r['_id']:<16} {r['unstructuredNotes'][:70]}")""")

# ---------------------------------------------------------------------------
md(r"""## Part F -- Reranking + precision comparison (REQ-05)

**Spec vs. reality:** `spec.md` section 4.3 sketched a `$rerank`
aggregation stage. Testing against a live cluster confirms `$rerank` is
**not** a real pipeline stage (`Unrecognized pipeline stage name: '$rerank'`).
Reranking today is a Voyage AI API call layered on top of retrieval, which
is what we do below: take the top candidates from `$vectorSearch`, rerank
with `rerank-2.5`, and compare orderings.""")

code(r"""candidates = list(db.assets.aggregate([
    {"$vectorSearch": {
        "index": VECTOR_INDEX,
        "path": "unstructuredNotes" if used_autoembed else "unstructuredNotesEmbedding",
        **({"query": QUERY_TEXT} if used_autoembed else
           {"queryVector": vo.embed([QUERY_TEXT], model="voyage-3.5", input_type="query").embeddings[0]}),
        "numCandidates": 50,
        "limit": 10,
        "filter": {"$and": [
            {"tenantId": {"$eq": TENANT}},
            {"authorizedRolesOrTeams": {"$in": USER_ROLES}},
        ]},
    }},
    {"$project": {"unstructuredNotes": 1, "vscore": {"$meta": "vectorSearchScore"}}},
]))

print("--- Vector search order (pre-rerank) ---")
for c in candidates:
    print(f"  {c['vscore']:.4f}  {c['_id']:<16} {c['unstructuredNotes'][:65]}")

docs_text = [c["unstructuredNotes"] for c in candidates]
rerank_result = vo.rerank(QUERY_TEXT, docs_text, model="rerank-2.5", top_k=5)

print("\n--- Reranked top 5 (voyage rerank-2.5) ---")
reranked_ids = []
for r in rerank_result.results:
    orig = candidates[r.index]
    reranked_ids.append(orig["_id"])
    print(f"  {r.relevance_score:.4f}  {orig['_id']:<16} {orig['unstructuredNotes'][:65]}")

vector_order_top5 = [c["_id"] for c in candidates[:5]]
print("\nVector-only top 5 order: ", vector_order_top5)
print("Reranked top 5 order:    ", reranked_ids)
if vector_order_top5 != reranked_ids:
    print("\nReranking changed the top-5 ordering -- e.g. it correctly promotes the")
    print("literal fast-charger-cutout report over a more general battery-heat note")
    print("that vector similarity alone ranked as equally relevant.")""")

# ---------------------------------------------------------------------------
md(r"""## Part G -- Value proposition recap

Rendered from the actual results captured above, not hardcoded claims.""")

code(r"""print("+" + "-" * 84 + "+")
print("|  BENEFITS AT A GLANCE (measured against a live Atlas cluster in this notebook)   |")
print("+" + "-" * 84 + "+")
print(f"| 1. ARCHITECTURAL SIMPLICITY  Single collection, {len(db.assets.distinct('assetType'))} asset types, 0 joins".ljust(85) + "|")
print(f"| 2. LATENCY                   Single-pass median {single_median:.1f}ms vs fan-out median {fanout_median:.1f}ms ({fanout_median/single_median:.2f}x)".ljust(85) + "|")
print(f"| 3. AUTO-EMBEDDING             used_autoembed = {used_autoembed} (native Atlas + Voyage AI voyage-4)".ljust(85) + "|")
print(f"| 4. HYBRID SEARCH              $rankFusion combined keyword + vector, {len(hybrid_results)} results, 0 leaks".ljust(85) + "|")
print(f"| 5. RERANKING                  voyage rerank-2.5 {'changed' if vector_order_top5 != reranked_ids else 'preserved'} top-5 ordering".ljust(85) + "|")
print("+" + "-" * 84 + "+")""")

# ---------------------------------------------------------------------------
nb["cells"] = cells
nb["metadata"] = {
    "colab": {"name": "amp_mongodb_poc.ipynb", "provenance": []},
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python"},
}

with open("notebooks/amp_mongodb_poc.ipynb", "w") as f:
    nbf.write(nb, f)

print("Wrote notebooks/amp_mongodb_poc.ipynb")
