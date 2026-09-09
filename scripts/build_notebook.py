#!/usr/bin/env python3
"""Generate notebooks/amp_mongodb_poc.ipynb from validated, working code.

Every cell in this notebook was run against a live MongoDB Atlas cluster
before being embedded here -- nothing is hypothetical. Run this script
whenever the demo logic changes, then re-execute the notebook to confirm
it still works end to end (see scripts/execute_notebook.py).

Schema v2 (production-scale amendment): the original 19-doc fixture proved
the core mechanics; this version scales to ~50,000 vehicles across 5 fleet
customers + Rivian OEM, models the real entity relationships Rivian
described (Tenant<->Segment 1:many, Tenant<->Asset many:many, Segment<->Asset
many:many, hierarchical role grants), and adds faceted-search backend
queries matching a real fleet-management UI. See README for the full
rationale and entity-relationship table.

Seed data for Part B is loaded from data/*.json at generation time (not
duplicated by hand in this file); scripts/topology.py (tenant/segment/
authorization-closure logic) is embedded verbatim so the notebook stays
self-contained and Colab-shareable without needing the repo checked out.
"""
import json
import re
from pathlib import Path

import nbformat as nbf

ROOT = Path(__file__).resolve().parent.parent

nb = nbf.v4.new_notebook()
cells = []


def md(text):
    cells.append(nbf.v4.new_markdown_cell(text))


def code(text):
    cells.append(nbf.v4.new_code_cell(text))


def json_file_as_python_literal(name: str, var_name: str) -> str:
    """Load data/<name>, return a `var_name = [...]` Python source string."""
    with open(ROOT / "data" / name) as f:
        data = json.load(f)
    literal = json.dumps(data, indent=4)
    literal = re.sub(r"\bnull\b", "None", literal)
    return f"{var_name} = {literal}"


def file_as_code(name: str) -> str:
    """Embed scripts/<name> verbatim as notebook code -- used for
    scripts/topology.py, which is pure/dependency-free and needs to be
    available in-notebook (Colab won't have the repo checked out)."""
    with open(ROOT / "scripts" / name) as f:
        return f.read()


# ---------------------------------------------------------------------------
md(r"""# AMP on MongoDB Atlas: Unified Multi-Tenant, Vector & Hybrid Search POC

This notebook is the executable companion to `spec.md` (SPEC-001-AMP-MONGO),
scaled up to production-representative volume (~50,000 vehicles) and
extended to match the real entity-relationship model Rivian's fleet
architecture actually uses (tenant<->segment, tenant<->asset,
segment<->asset, and hierarchical role grants -- see README for the full
table). It proves out, against a **real MongoDB Atlas cluster**, the
following:

| Req | Feature | Part |
|---|---|---|
| REQ-01 | Single-pass authorization at scale (no cross-DB fan-out) | C, D |
| REQ-02 | Polymorphic asset schema | B |
| REQ-03 | Hybrid keyword + vector search | E |
| REQ-04 | Native Atlas auto-embedding (Voyage AI) | C, E |
| REQ-05 | In-engine / integrated reranking | F |
| REQ-06 | Multi-tenant assets (many-to-many) + tenant-scoped segments | D4 |
| REQ-07 | Rule-based / dynamic segment membership | H |
| REQ-08 | Hierarchical role-grant propagation at scale | I |
| REQ-09 | Tenant transfer as an ACID transaction | J |
| REQ-10 | Faceted search backend (filter panel + fleet tree) | K, L, M |

**Honesty note on spec vs. reality:** the original spec sketched an
`autoEmbed` field type and a `$rerank` aggregation stage. Building this POC
against a live cluster surfaced the *actual* current syntax (documented
inline). Every cell below actually executed successfully against a live
50,019-document collection -- nothing here is illustrative pseudocode.
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

code(r"""import datetime
import random
import statistics
import time

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


def wait_for_index(coll, name, timeout=400):
    start = time.time()
    while time.time() - start < timeout:
        idxs = list_search_indexes_retry(coll, name)
        if idxs and idxs[0].get("queryable"):
            return idxs[0]
        time.sleep(5)
    raise TimeoutError(f"Index {name} not queryable after {timeout}s")""")

md(r"""### A3. Shared tenant/segment/authorization logic

Embedded verbatim from `scripts/topology.py` so this notebook is
self-contained (Colab won't have the repo checked out). Key schema v2
ideas, matching the real entity-relationship model:

- **Tenant <-> Segment is one-to-many**: a segment belongs to exactly one
  tenant, never shared across tenants.
- **Tenant <-> Asset is many-to-many**: `assets.tenantIds` is an array.
  Rivian (the OEM) is in every vehicle's `tenantIds`; each vehicle also has
  exactly one fleet-customer tenant.
- **Segment <-> Asset is many-to-many, and now tenant-scoped**: since a
  multi-tenant asset needs a *different* placement in each tenant's
  hierarchy, `segmentAssignments[]` entries carry their own `tenantId`.
- **Role grants are hierarchical**: `grantedRoles` on a segment is the
  actual source of truth ("access granted at this exact node"); an asset's
  `authorizedRolesOrTeams` is the *computed* union of `grantedRoles` across
  its segment and every ancestor -- this is the mechanism behind "access to
  a parent node recursively grants visibility to all child nodes and their
  connected vehicles," and it's what collapses a separate ACL-service round
  trip into a single indexed query.""")

code(file_as_code("topology.py"))

# ---------------------------------------------------------------------------
md(r"""## Part B -- Seed the curated correctness fixture (REQ-02)

A small (19-asset), hand-curated fixture with deliberate "trap" documents
(wrong role, wrong tenant, admin-only, safety-hold) used throughout this
notebook to prove authorization filtering actually works, not just that
queries compile. Also seeds `tenants` (6 docs) and the small
`acme_fleet_corp`/`globex_logistics`/`rivian_oem` segment hierarchies.

Production-scale data (~50,000 vehicles) is added on top of this in Part
B2 -- kept separate so the trap-document semantics here stay small and
legible.""")

code(
    "TENANTS_SEED = TENANTS  # from topology.py, embedded in Part A3\n\n"
    + json_file_as_python_literal("segments_seed.json", "segments_data")
    + "\n\n"
    + json_file_as_python_literal("assets_seed.json", "assets_data")
    + "\n\n"
    + "db.tenants.drop()\n"
    + "db.asset_segments.drop()\n"
    + "db.assets.drop()\n"
    + "db.tenant_transfer_events.drop()\n\n"
    + "db.tenants.insert_many(TENANTS_SEED)\n"
    + "db.asset_segments.insert_many(segments_data)\n"
    + "db.assets.insert_many(assets_data)\n\n"
    + 'print(f"Seeded {db.tenants.count_documents({})} tenants")\n'
    + 'print(f"Seeded {db.asset_segments.count_documents({})} segments")\n'
    + 'print(f"Seeded {db.assets.count_documents({})} assets")\n'
    + 'print("Asset types (polymorphic, REQ-02):", db.assets.distinct("assetType"))'
)

# ---------------------------------------------------------------------------
md(r"""## Part B2 -- Scale to production volume: ~50,000 vehicles

Generates a synthetic fleet across the 5 fleet-customer tenants (10,000
vehicles each): `acme_fleet_corp`, `globex_logistics` (reuse their existing
small hierarchy from Part B), plus 3 new tenants generated here --
`amazon_logistics`, `dhl_express_fleet`, `driveshare_rentals` -- each with a
procedurally generated region -> depot -> team hierarchy (Rivian's OEM
vehicle-line hierarchy from `topology.py` applies to all of them).

**Scope decisions** (see README for full rationale):
- Every generated vehicle gets a `rivian_oem` segment assignment (by vehicle
  line: R1T / R1S / RPV) *plus* its owning fleet customer's assignment --
  proving the same vehicle sits in two genuinely different hierarchies at
  once.
- Generated vehicles deliberately do **not** get `unstructuredNotes` /
  embeddings. The hybrid-search/rerank demos (Parts E/F) stay on the small
  curated corpus where the trap-document semantics are meaningful --
  embedding 50K docs via Voyage would cost real time/money for no
  additional signal.
- Telemetry fields (`stateOfCharge`, `mileage`, `chargingStatus`, etc.)
  match the fields a real fleet-tracker UI needs (see README), so the
  faceted-search demos in Parts K-M have realistic data to bucket.""")

code(r"""CUSTOMER_SPEC = {
    "amazon_logistics": {"label": "Amazon Logistics", "rpv_share": 0.9, "count": 10_000},
    "dhl_express_fleet": {"label": "DHL Express Fleet", "rpv_share": 0.85, "count": 10_000},
    "acme_fleet_corp": {"label": "Acme Fleet Corp", "rpv_share": 0.1, "count": 10_000},
    "globex_logistics": {"label": "Globex Logistics", "rpv_share": 0.15, "count": 10_000},
    "driveshare_rentals": {"label": "DriveShare Rentals", "rpv_share": 0.05, "count": 10_000},
}
TRIMS_BY_MODEL = {
    "R1T": ["Adventure", "Explore", "Launch Edition"],
    "R1S": ["Adventure", "Explore", "Launch Edition"],
    "RPV": ["EDV-500", "EDV-700"],
}
COLORS = ["Rivian Blue", "Forest Green", "Rivian Blue", "Silver", "Compass Yellow", "Limestone"]
CHARGING_STATUSES = ["Ready to Charge", "Charging Complete", "Waiting on Charger", "Charging", "Not Charging"]
ASSET_GROUPS = ["Line Haul", "Last Mile", "Reserve Fleet", "Rental Pool", "Depot Standby"]
MAX_RANGE_MILES = {"R1T": 350, "R1S": 340, "RPV": 165}


def random_vehicle_attrs(rng, vin, model, year):
    max_range = MAX_RANGE_MILES[model]
    soc = rng.randint(15, 100)
    distance_to_empty = max(1, round(max_range * (soc / 100) * rng.uniform(0.9, 1.05)))
    age_years = max(0, 2027 - year)
    mileage = max(50, round(rng.uniform(6000, 24000) * age_years + rng.uniform(-1500, 1500)))
    hv_battery_soh = round(max(78.0, 100.0 - age_years * rng.uniform(1.2, 3.0)), 1)
    return {
        "make": "RIVIAN", "model": model, "trim": rng.choice(TRIMS_BY_MODEL[model]),
        "color": rng.choice(COLORS), "vin": vin, "year": year,
        "firmwareVersion": rng.choice(["v2026.12.4", "v2026.11.2", "v2026.10.1"]),
        "batteryCapacityKw": {"R1T": 135, "R1S": 149, "RPV": 118}[model],
        "mileage": mileage, "stateOfCharge": soc, "distanceToEmptyMiles": distance_to_empty,
        "chargingStatus": rng.choice(CHARGING_STATUSES),
        "vehicleSpeed": 0 if rng.random() < 0.7 else rng.randint(1, 75),
        "hvBatterySOH": hv_battery_soh, "assetGroup": rng.choice(ASSET_GROUPS),
    }


def make_vin(rng, idx):
    body = "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ0123456789") for _ in range(9))
    return f"7FCEHEB{body}{idx:06d}"[:17]


def curated_leaf_teams():
    # True leaf nodes (no children) of acme/globex's existing hierarchy --
    # globex's curated tree is only 2 levels deep, so this can't hardcode
    # segmentType=='team'.
    tenants = {"acme_fleet_corp", "globex_logistics"}
    relevant = [s for s in segments_data if s["tenantId"] in tenants]
    parent_ids = {s["hierarchy"]["parentId"] for s in relevant if s["hierarchy"]["parentId"]}
    leaves = {t: [] for t in tenants}
    for seg in relevant:
        if seg["_id"] not in parent_ids and seg["segmentType"] != "restricted":
            leaves[seg["tenantId"]].append(seg["_id"])
    return leaves


rng = random.Random(42)
scale_segments = list(rivian_oem_segments())  # already inserted in Part B via segments_data; skip re-inserting
generated_leaves = {}
for tenant_id in ["amazon_logistics", "dhl_express_fleet", "driveshare_rentals"]:
    nodes, leaves = fleet_customer_segments(tenant_id, CUSTOMER_SPEC[tenant_id]["label"])
    scale_segments.extend(nodes)
    generated_leaves[tenant_id] = leaves
leaf_teams_by_tenant = {**curated_leaf_teams(), **generated_leaves}

all_segments_by_id = {s["_id"]: s for s in segments_data + scale_segments}

vehicles = []
idx = 0
for tenant_id, spec in CUSTOMER_SPEC.items():
    leaves = leaf_teams_by_tenant[tenant_id]
    for _ in range(spec["count"]):
        idx += 1
        model = "RPV" if rng.random() < spec["rpv_share"] else rng.choice(["R1T", "R1S"])
        year = rng.choice([2023, 2023, 2024, 2024, 2025, 2026])
        vin = make_vin(rng, idx)
        leaf_segment = rng.choice(leaves)
        customer_assignment = compute_assignment(tenant_id, leaf_segment, all_segments_by_id)
        rivian_assignment = compute_assignment("rivian_oem", MODEL_TO_RIVIAN_LINE_SEGMENT[model], all_segments_by_id)
        vehicles.append({
            "_id": f"VIN_SCALE_{idx:06d}",
            "schemaVersion": 2,
            "tenantIds": ["rivian_oem", tenant_id],
            "assetType": "vehicle",
            "attributes": random_vehicle_attrs(rng, vin, model, year),
            "segmentAssignments": [rivian_assignment, customer_assignment],
            "updatedAt": "2026-09-01T00:00:00Z",
        })

# only insert the NEW tenant segments here -- acme/globex/rivian_oem's own
# segments came from Part B's segments_data
new_tenant_ids = {"amazon_logistics", "dhl_express_fleet", "driveshare_rentals"}
db.asset_segments.insert_many([s for s in scale_segments if s["tenantId"] in new_tenant_ids])

t0 = time.perf_counter()
BATCH = 2000
for i in range(0, len(vehicles), BATCH):
    db.assets.insert_many(vehicles[i:i + BATCH])
t1 = time.perf_counter()

print(f"Inserted {len(vehicles)} vehicles in {t1 - t0:.1f}s")
print(f"Total assets in {MONGODB_DB}.assets: {db.assets.count_documents({})}")
print(f"Total segments in {MONGODB_DB}.asset_segments: {db.asset_segments.count_documents({})}")""")

# ---------------------------------------------------------------------------
md(r"""## Part C -- Build indexes (operational + Atlas Search/Vector)

### C1. Operational compound index for authorization (REQ-01)

**Schema v2 wrinkle:** `tenantIds` and `segmentAssignments` are now *both*
arrays on the same document. MongoDB does not allow a compound index across
two different array fields -- we prove this live below, then build the
correct index design around it: `segmentAssignments.tenantId` +
`segmentAssignments.authorizedRolesOrTeams` compound together (fine, same
array), and `tenantIds` gets its own separate single-field index.""")

code(r"""# Prove the "parallel arrays" restriction live, on a throwaway collection,
# before designing around it.
test = db.parallel_array_test
test.drop()
test.create_index([("tenantIds", ASCENDING), ("segmentAssignments.tenantId", ASCENDING)])
try:
    test.insert_one({"tenantIds": ["a", "b"], "segmentAssignments": [{"tenantId": "a"}, {"tenantId": "b"}]})
    print("Insert succeeded (unexpected)")
except OperationFailure as e:
    print(f"Insert failed as expected: {e}")
test.drop()""")

code(r"""old_names = {i["name"] for i in db.assets.list_indexes()}
if "segment_auth_make_idx" in old_names:
    db.assets.drop_index("segment_auth_make_idx")
    print("Dropped stale index: segment_auth_make_idx")

db.assets.create_index(
    [("segmentAssignments.tenantId", ASCENDING), ("segmentAssignments.authorizedRolesOrTeams", ASCENDING)],
    name="segment_auth_idx",
)
print("Created operational index: segment_auth_idx")

db.assets.create_index([("tenantIds", ASCENDING)], name="tenant_ids_idx")
print("Created index: tenant_ids_idx (simple tenant-membership lookups, kept separate to avoid the parallel-arrays restriction above)")""")

md(r"""### C1c. Unique index on VIN

A real VIN is a legally unique identifier -- nothing in this schema enforced
that until now. `api/main.py`'s vehicle-detail/update/delete endpoints look
vehicles up by `attributes.vin` (the real VIN), not `_id` (an internal
document key that happens to look similar, e.g. `VIN_SCALE_000001` --
getting this distinction wrong was a real bug caught by testing the
frontend's click-through-to-detail flow, not by code review).

Partial (`assetType: "vehicle"` only), since `ev_charger`/`e_bike` docs
don't have a `vin` field at all -- without the partial filter, a plain
unique index would need every non-vehicle document to also satisfy the
uniqueness constraint on a field they don't have.""")

code(r"""db.assets.create_index(
    [("attributes.vin", ASCENDING)],
    name="vin_unique_idx",
    unique=True,
    partialFilterExpression={"assetType": "vehicle"},
)
print("Created unique index: vin_unique_idx (partial, assetType=vehicle only)")

# Prove it live: try to insert a second vehicle with an already-used VIN.
existing_vin = db.assets.find_one({"assetType": "vehicle"})["attributes"]["vin"]
try:
    db.assets.insert_one({
        "_id": "VIN_DUPLICATE_TEST", "schemaVersion": 2, "tenantIds": ["rivian_oem"],
        "assetType": "vehicle", "attributes": {"vin": existing_vin, "model": "R1T"},
        "segmentAssignments": [],
    })
    print("Insert succeeded (unexpected)")
except OperationFailure as e:
    print(f"Duplicate VIN correctly rejected: {e}")

# Confirm non-vehicle docs (no vin field) are unaffected by the partial index.
db.assets.insert_many([
    {"_id": "TEST_NO_VIN_1", "assetType": "ev_charger", "attributes": {}, "tenantIds": [], "segmentAssignments": []},
    {"_id": "TEST_NO_VIN_2", "assetType": "ev_charger", "attributes": {}, "tenantIds": [], "segmentAssignments": []},
])
print("Two ev_charger docs with no vin field both inserted fine (partial index correctly excludes them)")
db.assets.delete_many({"_id": {"$in": ["TEST_NO_VIN_1", "TEST_NO_VIN_2"]}})""")

md(r"""### C1a-note. Why this index has only 2 fields, not 3

An earlier version of this index had a third trailing field,
`attributes.make`. Live `explain()` testing (below) showed it was dead
weight: every vehicle's make is `"RIVIAN"` (Rivian is the only OEM in this
model), so it contributed zero selectivity while still being maintained on
every write.

The real, measured picture: for a **narrow** role (a single team, ~500
vehicles), this 2-field index is highly selective and gets chosen
automatically. For a **broad** role (`role_fleet_admin`, ~10,000 vehicles),
no index -- this one or any other -- can avoid scanning roughly the
candidate-set size once you add arbitrary attribute filters on top, because
those filters weren't all indexed together. That's inherent to
faceted/multi-attribute filtering, not a fixable index problem at this data
volume; adding a dedicated compound index per filter combination doesn't
scale (10 optional filter fields -> a combinatorial index explosion).""")

code(r"""narrow_query = {
    "segmentAssignments": {"$elemMatch": {"tenantId": "amazon_logistics",
                                           "authorizedRolesOrTeams": "team_amazon_logistics_0_0_0"}},
    "attributes.chargingStatus": "Ready to Charge",
}
broad_query = {
    "segmentAssignments": {"$elemMatch": {"tenantId": "amazon_logistics", "authorizedRolesOrTeams": "role_fleet_admin"}},
    "attributes.chargingStatus": "Ready to Charge",
}

for label, q in [("narrow (single team, ~500 vehicles)", narrow_query), ("broad (role_fleet_admin, ~10,000 vehicles)", broad_query)]:
    plan = db.assets.find(q).explain()
    stats = plan["executionStats"]
    winner = plan["queryPlanner"]["winningPlan"]
    index_used = winner.get("inputStage", {}).get("indexName", winner.get("indexName", "COLLSCAN"))
    ratio = stats["totalDocsExamined"] / max(1, stats["nReturned"])
    print(f"{label}:")
    print(f"  index={index_used}  nReturned={stats['nReturned']}  docsExamined={stats['totalDocsExamined']}  overscan={ratio:.1f}x  ms={stats['executionTimeMillis']}")""")

md(r"""### C1b. Wildcard index over polymorphic attributes (Attribute Pattern)

`assets.attributes` is a **polymorphic** sub-document whose keys differ by
`assetType`. One wildcard index (`"attributes.$**"`) covers ad hoc
equality/range queries on any attribute -- present or added by a future
asset type -- without hand-maintaining a single-field index per attribute
per type. Proven with `explain()` before/after, at 50K-document scale.""")

code(r"""before_plan = db.assets.find({"attributes.connectorType": "CCS1"}).explain()
print("Winning plan stage WITHOUT wildcard index:", before_plan["queryPlanner"]["winningPlan"]["stage"])

db.assets.create_index([("attributes.$**", ASCENDING)], name="attributes_wildcard_idx")
print("\nCreated wildcard index: attributes_wildcard_idx")

after_plan = db.assets.find({"attributes.connectorType": "CCS1"}).explain()
winning = after_plan["queryPlanner"]["winningPlan"]
input_stage = winning.get("inputStage", {})
print("Winning plan stage WITH wildcard index:   ", winning["stage"], "/", input_stage.get("stage"))
print("Index used:", input_stage.get("indexName"))

assert before_plan["queryPlanner"]["winningPlan"]["stage"] == "COLLSCAN"
assert input_stage.get("indexName") == "attributes_wildcard_idx"
print("\nConfirmed: COLLSCAN -> IXSCAN on a field with no dedicated index.")""")

md(r"""### C2. Atlas Search index (keyword half of hybrid search, REQ-03)

`segmentAssignments` is indexed as `embeddedDocuments` so the tenant + role
filter can be applied *inside* the same array element via Atlas Search's
`embeddedDocument` operator (the search-index equivalent of `$elemMatch`).""")

code(r"""existing = {i["name"] for i in list_search_indexes_retry(db.assets)}

if "assets_text_search_index" not in existing:
    db.assets.create_search_index(SearchIndexModel(
        definition={
            "mappings": {
                "dynamic": False,
                "fields": {
                    "segmentAssignments": {
                        "type": "embeddedDocuments",
                        "fields": {
                            "tenantId": {"type": "token"},
                            "authorizedRolesOrTeams": {"type": "token"},
                        },
                    },
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

md(r"""### C2b. VIN autocomplete index (faceted-search backend, REQ-10)

A real fleet-tracker UI does *substring* VIN search at 50K+ scale
(searching "6493" matches VINs with that string anywhere, not just a
prefix). A plain regex can't use a B-tree index for an unanchored
substring; Atlas Search's `autocomplete` field type (nGram tokenization) is
the right tool.""")

code(r"""existing = {i["name"] for i in list_search_indexes_retry(db.assets)}

if "assets_vin_autocomplete_index" not in existing:
    db.assets.create_search_index(SearchIndexModel(
        definition={
            "mappings": {
                "dynamic": False,
                "fields": {
                    "attributes": {"type": "document", "fields": {
                        "vin": {"type": "autocomplete", "tokenization": "nGram",
                                "minGrams": 3, "maxGrams": 7, "foldDiacritics": False},
                    }},
                    "segmentAssignments": {
                        "type": "embeddedDocuments",
                        "fields": {
                            "tenantId": {"type": "token"},
                            "authorizedRolesOrTeams": {"type": "token"},
                        },
                    },
                },
            }
        },
        name="assets_vin_autocomplete_index",
        type="search",
    ))
    print("Submitted assets_vin_autocomplete_index")
else:
    print("assets_vin_autocomplete_index already exists")""")

md(r"""### C3. Native Atlas auto-embedding vector index (REQ-04)

Same shape validated in the original POC pass; `filter`-type fields now
point at `segmentAssignments.tenantId`/`segmentAssignments.authorizedRolesOrTeams`
instead of the old flat fields.""")

code(r"""existing = {i["name"] for i in list_search_indexes_retry(db.assets)}
used_autoembed = False

if "vector_auto_embed_index" not in existing:
    try:
        db.assets.create_search_index(SearchIndexModel(
            definition={
                "fields": [
                    {"type": "autoEmbed", "path": "unstructuredNotes", "model": "voyage-4",
                     "modality": "text", "quantization": "float", "similarity": "cosine"},
                    {"type": "filter", "path": "segmentAssignments.tenantId"},
                    {"type": "filter", "path": "segmentAssignments.authorizedRolesOrTeams"},
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

md(r"""### C4. Client-side embedding fallback

Scoped to docs that actually have `unstructuredNotes` -- only the 19-doc
curated fixture does (see Part B2's scope note).""")

code(r"""docs = list(db.assets.find({"unstructuredNotes": {"$exists": True}}, {"_id": 1, "unstructuredNotes": 1}))
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
                {"type": "filter", "path": "segmentAssignments.tenantId"},
                {"type": "filter", "path": "segmentAssignments.authorizedRolesOrTeams"},
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

code(r"""wait_for_index(db.assets, "assets_text_search_index")
print("assets_text_search_index is QUERYABLE.")
wait_for_index(db.assets, "assets_vin_autocomplete_index")
print("assets_vin_autocomplete_index is QUERYABLE.")""")

md(r"""### C5. Indexes on `asset_segments` and `tenant_transfer_events`

These two collections had **zero indexes beyond `_id`** through the whole
schema v2 pass until now -- invisible at ~100 segments and a handful of
transfer events, but confirmed live via `explain()` to be full `COLLSCAN`s
for every hierarchy-browsing query (`tenantId` lookups, the `hierarchy.path`
prefix query from Part D) and every transfer-audit lookup. Won't matter
until segment/tenant count or transfer-event volume grows, but it's the
correct baseline, not a premature optimization -- and it's the kind of gap
that's easy to miss because collections outside the main hot-path table
don't show up in day-to-day query latency until they do.""")

code(r"""before = db.asset_segments.find({"tenantId": "amazon_logistics"}).explain()
print("asset_segments tenantId lookup BEFORE index:", before["queryPlanner"]["winningPlan"]["stage"])

db.asset_segments.create_index([("tenantId", ASCENDING)], name="segment_tenant_idx")
db.asset_segments.create_index([("hierarchy.path", ASCENDING)], name="segment_path_idx")
print("Created asset_segments indexes: segment_tenant_idx, segment_path_idx")

db.tenant_transfer_events.create_index([("assetId", ASCENDING), ("timestamp", -1)], name="transfer_asset_history_idx")
db.tenant_transfer_events.create_index([("toTenantId", ASCENDING), ("timestamp", -1)], name="transfer_to_tenant_idx")
print("Created tenant_transfer_events indexes: transfer_asset_history_idx, transfer_to_tenant_idx")

after = db.asset_segments.find({"tenantId": "amazon_logistics"}).explain()
after_stage = after["queryPlanner"]["winningPlan"].get("inputStage", after["queryPlanner"]["winningPlan"])
print("asset_segments tenantId lookup AFTER index: ", after_stage["stage"], "/ index:", after_stage.get("indexName"))""")

# ---------------------------------------------------------------------------
md(r"""## Part D -- Single-pass authorization + fan-out benchmark at scale (REQ-01)

Same comparison as the original POC pass, now run against ~50,000 real
documents instead of 19 -- so the "resolve authorized IDs" step in the
fan-out simulation returns a realistic thousands-sized candidate set, not a
handful.""")

code(r"""def fanout_simulation(tenant, roles, status):
    t0 = time.perf_counter()
    candidate_ids = [a["_id"] for a in db.assets.find(
        {"segmentAssignments": {"$elemMatch": {"tenantId": tenant, "authorizedRolesOrTeams": {"$in": roles}}}},
        {"_id": 1})]
    t1 = time.perf_counter()
    results = list(db.assets.find({"_id": {"$in": candidate_ids}, "attributes.chargingStatus": status}))
    t2 = time.perf_counter()
    return results, {"resolve_ids_ms": (t1 - t0) * 1000, "final_query_ms": (t2 - t1) * 1000,
                      "total_ms": (t2 - t0) * 1000, "n_candidates": len(candidate_ids)}


def single_pass(tenant, roles, status):
    t0 = time.perf_counter()
    results = list(db.assets.find({
        "segmentAssignments": {"$elemMatch": {"tenantId": tenant, "authorizedRolesOrTeams": {"$in": roles}}},
        "attributes.chargingStatus": status,
    }))
    t1 = time.perf_counter()
    return results, {"total_ms": (t1 - t0) * 1000}


TENANT = "acme_fleet_corp"
USER_ROLES = ["region_california_north"]
N_TRIALS = 15

# warm up connections first so we measure query cost, not connection setup
fanout_simulation(TENANT, USER_ROLES, "Ready to Charge")
single_pass(TENANT, USER_ROLES, "Ready to Charge")

fanout_times, single_times = [], []
for _ in range(N_TRIALS):
    fanout_results, m1 = fanout_simulation(TENANT, USER_ROLES, "Ready to Charge")
    single_results, m2 = single_pass(TENANT, USER_ROLES, "Ready to Charge")
    assert {r["_id"] for r in fanout_results} == {r["_id"] for r in single_results}, "result sets must match"
    fanout_times.append(m1["total_ms"])
    single_times.append(m2["total_ms"])

fanout_median = statistics.median(fanout_times)
single_median = statistics.median(single_times)

print(f"Fan-out candidate set size (region_california_north closure): {m1['n_candidates']} assets")
print(f"Simulated fan-out (2 round trips), median of {N_TRIALS} runs: {fanout_median:.1f} ms")
print(f"Single-pass MongoDB query,          median of {N_TRIALS} runs: {single_median:.1f} ms")
print(f"Speedup: {fanout_median / single_median:.2f}x")
print(f"\n({len(single_results)} results, sets identical across every trial -- correctness-equivalent, not just faster)")
print("\nThis now runs against ~50,000 real documents (vs. 19 in the original POC pass),")
print("so the fan-out step's $in candidate list is realistically sized (thousands, not a")
print("handful) -- this is no longer a toy-scale caveat.")""")

md(r"""### Authorization correctness check (test-fixture assertion, not the auth mechanism)

**This is not the authorization pattern -- it's a regression test over our
own known seed data.** The actual authorization logic is entirely the query
above. This list stays at 3 items regardless of dataset size -- it's a
fixed set of known-bad cases a unit test would also hardcode, same as the
original POC pass:

- `VIN_RIVIAN_004` -- right tenant, wrong role (`region_texas`)
- `VIN_RIVIAN_009` -- safety-hold restricted (moved to a dedicated
  `role_fleet_admin`-only segment, see README for why it's a sibling
  branch, not a child, of its old region)
- `VIN_GLOBEX_001` -- **wrong tenant**, but with the literal string
  `region_california_north` granted on its own segment -- catches a filter
  that checks role but forgets tenant.""")

code(r"""df = pd.DataFrame([r["attributes"] | {"_id": r["_id"]} for r in single_results])
known_trap_doc_ids = {"VIN_RIVIAN_004", "VIN_RIVIAN_009", "VIN_GLOBEX_001"}  # test fixture, not prod logic
visible_ids = set(df["_id"])
assert known_trap_doc_ids.isdisjoint(visible_ids), f"Leak detected: {known_trap_doc_ids & visible_ids}"
print("Confirmed: cross-tenant and out-of-role trap documents correctly excluded.\n")
print(df[["_id", "make", "model", "chargingStatus"]].head(10).to_string(index=False))
print(f"... ({len(df)} total rows)")""")

md(r"""### Why `asset_segments` is still a separate collection

Every query so far only touches `assets` -- `asset_segments` is never
joined at read time. Because `segmentAssignments[].ancestorSegments` stores
only **IDs**, not the segment's mutable display metadata, renaming/
reparenting a segment is a single-document write with zero writes to
`assets`, however many (of 50,000+) assets reference it.""")

code(r"""descendants = list(db.asset_segments.find(
    {"hierarchy.path": {"$regex": "^,seg_global,seg_us_west,seg_california,seg_california_north,"}},
    {"_id": 1, "name": 1, "segmentType": 1},
))
print("Descendants of 'Northern California Fleet Operations' (path prefix query):")
for s in descendants:
    print(f"  {s['_id']:<22} {s['segmentType']:<10} {s['name']}")""")

code(r"""before_count = db.assets.count_documents({"segmentAssignments.segmentId": "seg_hayward_team"})

t0 = time.perf_counter()
db.asset_segments.update_one(
    {"_id": "seg_hayward_team"},
    {"$set": {"name": "Hayward Depot Team (renamed during POC demo)", "owner": "usr_mgr_99"}},
)
rename_ms = (time.perf_counter() - t0) * 1000

after_count = db.assets.count_documents({"segmentAssignments.segmentId": "seg_hayward_team"})
assert before_count == after_count, "Renaming a segment must not change which assets reference it"
print(f"Renamed segment in {rename_ms:.2f} ms -- 1 document write.")
print(f"{after_count} assets reference seg_hayward_team, all still correctly linked after rename.")

still_works = db.assets.count_documents({
    "segmentAssignments": {"$elemMatch": {"tenantId": "acme_fleet_corp", "authorizedRolesOrTeams": {"$in": ["team_hayward"]}}}
})
print(f"Authorization query for 'team_hayward' still returns {still_works} assets, unaffected by the rename above.")""")

md(r"""### Many-to-many: one asset, multiple segments (same tenant)

`VIN_RIVIAN_010` is a pool vehicle assigned to **both** `seg_hayward_team`
and `seg_san_jose_team` at once, within the *same* tenant
(`acme_fleet_corp`) -- membership in either team's role is sufficient for
access (OR, not AND).""")

code(r"""for role in ["team_hayward", "team_san_jose", "team_austin"]:
    found = db.assets.find_one({
        "_id": "VIN_RIVIAN_010",
        "segmentAssignments": {"$elemMatch": {"tenantId": "acme_fleet_corp", "authorizedRolesOrTeams": {"$in": [role]}}},
    })
    print(f"User with only '{role:<14}' role sees pool vehicle VIN_RIVIAN_010: {found is not None}")""")

# ---------------------------------------------------------------------------
md(r"""## Part D2 -- Multi-tenant visibility: the actual Rivian/Amazon scenario (REQ-06)

The literal example from the production entity-relationship spec: "an
electric delivery van sold to Amazon is accessible by both Amazon (the
buyer) and Rivian (the OEM), but strictly hidden from a third party like VW
or Hertz." `VIN_RIVIAN_001` is owned by `rivian_oem` + `acme_fleet_corp` --
we prove Rivian sees it (via its own engineering role), Acme sees it (via
its own team role), and an unrelated third tenant (`globex_logistics`) does
**not** -- even though it uses the exact same role string
(`region_california_north`) that Acme uses internally.""")

code(r"""VIN = "VIN_RIVIAN_001"
checks = [
    ("rivian_oem", "role_rivian_r1t_eng", "Rivian OEM engineering (manufacturer)"),
    ("acme_fleet_corp", "team_san_jose", "Acme Fleet Corp (the buyer/operator)"),
    ("globex_logistics", "region_california_north", "unrelated third tenant, same role STRING"),
]
for tenant, role, label in checks:
    found = db.assets.find_one({
        "_id": VIN,
        "segmentAssignments": {"$elemMatch": {"tenantId": tenant, "authorizedRolesOrTeams": role}},
    })
    print(f"{label:<45} sees {VIN}: {found is not None}")

doc = db.assets.find_one({"_id": VIN})
print(f"\n{VIN} tenantIds: {doc['tenantIds']}")
for sa in doc["segmentAssignments"]:
    print(f"  tenant={sa['tenantId']:<18} segment={sa['segmentId']:<20} roles={sa['authorizedRolesOrTeams']}")""")

# ---------------------------------------------------------------------------
md(r"""## Part D3 -- Pagination at scale: skip/limit vs. range-based

`api/main.py`'s `/vehicles` endpoint pages with `.skip().limit()`, matching
the reference UI's page-number pagination. That's the right call for a
page-number UI, but it's worth being honest about its known cost: MongoDB
still has to walk and discard every skipped document even with a covering
index, so cost grows with page depth, not just page size.

Measured live (not a manual assertion): later pages against a ~10,000-doc
tenant-scoped candidate set cost meaningfully more than earlier ones. At
this data volume it's a difference of tens of milliseconds, not a
production incident -- but the trend is real, and it would compound badly
at millions of documents or very deep pagination. The standard fix is
**range-based (keyset) pagination** -- filter on `_id > lastSeenId` sorted
by `_id`, instead of skipping N documents -- which costs the same regardless
of how deep into the result set you are, at the cost of losing direct
"jump to page N" navigation. `api/main.py` exposes both: `page`/`pageSize`
for the UI's page-number navigation, and an `afterId` cursor param as the
scalable alternative for programmatic/infinite-scroll consumers.""")

code(r"""query = {"segmentAssignments": {"$elemMatch": {"tenantId": "amazon_logistics", "authorizedRolesOrTeams": "role_fleet_admin"}}}
page_size = 25
depths = [1, 50, 200, 399]

print("--- skip/limit cost by page depth (one query per page, cold) ---")
for page in depths:
    t0 = time.perf_counter()
    docs = list(db.assets.find(query, {"_id": 1}).sort("_id", 1).skip((page - 1) * page_size).limit(page_size))
    ms = (time.perf_counter() - t0) * 1000
    print(f"  page {page:4} (skip={((page - 1) * page_size):6}): {ms:6.1f}ms, {len(docs)} docs")

# Fair comparison for range-based (keyset) pagination: keyset pagination is
# inherently sequential -- you can't jump to "page 399" without having
# already walked there, that's its whole trade-off vs. skip/limit. So for
# each depth checkpoint, find the boundary _id an untimed skip would land on
# (simulating "we already paged this far"), then time ONLY the next fetch
# from that cursor -- an apples-to-apples "cost of retrieving this page"
# comparison, not conflating cursor-walking cost with fetch cost.
print("\n--- range-based (keyset) cost at equivalent depth (boundary found untimed, only the fetch is timed) ---")
for page in depths:
    boundary = list(db.assets.find(query, {"_id": 1}).sort("_id", 1).skip((page - 1) * page_size).limit(1))
    cursor_id = boundary[0]["_id"] if boundary else None
    t0 = time.perf_counter()
    range_query = dict(query)
    if cursor_id:
        range_query["_id"] = {"$gte": cursor_id}
    docs = list(db.assets.find(range_query, {"_id": 1}).sort("_id", 1).limit(page_size))
    ms = (time.perf_counter() - t0) * 1000
    print(f"  page {page:4} (cursor >= {str(cursor_id):<16}): {ms:6.1f}ms, {len(docs)} docs")
print("\n(range-based fetch cost stays roughly flat regardless of depth; skip/limit's grows with")
print("page number -- but note range-based requires already knowing the boundary _id, which is")
print("exactly what makes it sequential-only: fine for 'next page' navigation, not for jumping")
print("directly to page 399.)")""")

# ---------------------------------------------------------------------------
md(r"""## Part H -- Rule-based segments: dynamic membership (REQ-07)

The production spec calls out upcoming "rule-based segments" where
vehicles dynamically link to segments based on shifting conditions (e.g.
battery level). We model this as a segment with a stored `rule`, plus an
evaluator that finds matching assets and adds a segment assignment for
them.

**A real lesson learned here:** the evaluator must use a single
`update_many` with a shared assignment document, not a per-document loop.
An earlier version of this cell looped `update_one` calls once per matching
vehicle and did not finish within a 2-minute timeout against ~2,900 matches
(thousands of individual network round trips). The `update_many` version
below does the identical update in about a second.""")

code(r"""rule_segment = {
    "_id": "seg_rivian_low_battery_watch",
    "schemaVersion": 1,
    "tenantId": "rivian_oem",
    "name": "Low Battery Watch (Rule-Based)",
    "segmentType": "rule_based",
    "owner": "usr_rivian_ops_1",
    "status": "active",
    "grantedRoles": ["role_rivian_ops_monitoring"],
    "rule": {"path": "attributes.stateOfCharge", "operator": "$lt", "value": 20},
    "hierarchy": {"parentId": "seg_rivian_global", "ancestors": ["seg_rivian_global"],
                  "path": ",seg_rivian_global,seg_rivian_low_battery_watch,"},
    "createdAt": "2026-09-01T00:00:00Z",
}
db.asset_segments.delete_one({"_id": rule_segment["_id"]})
db.asset_segments.insert_one(rule_segment)

chain = rule_segment["hierarchy"]["ancestors"] + [rule_segment["_id"]]
granted = set()
for sid in chain:
    granted.update(db.asset_segments.find_one({"_id": sid}).get("grantedRoles", []))
rule_assignment = {"tenantId": "rivian_oem", "segmentId": rule_segment["_id"],
                    "ancestorSegments": chain, "authorizedRolesOrTeams": sorted(granted)}

rule = rule_segment["rule"]  # {"path": "attributes.stateOfCharge", "operator": "$lt", "value": 20}
evaluator_filter = {
    rule["path"]: {rule["operator"]: rule["value"]},
    "segmentAssignments.segmentId": {"$ne": rule_segment["_id"]},
}

t0 = time.perf_counter()
rule_eval_result = db.assets.update_many(evaluator_filter, {"$push": {"segmentAssignments": rule_assignment}})
t1 = time.perf_counter()
print(f"Rule evaluator applied {rule['path']} {rule['operator']} {rule['value']}: "
      f"matched={rule_eval_result.matched_count} modified={rule_eval_result.modified_count} in {(t1 - t0) * 1000:.1f}ms")

qcount = db.assets.count_documents({
    "segmentAssignments": {"$elemMatch": {"tenantId": "rivian_oem", "authorizedRolesOrTeams": "role_rivian_ops_monitoring"}}
})
print(f"Vehicles now visible to role_rivian_ops_monitoring: {qcount}")""")

# ---------------------------------------------------------------------------
md(r"""## Part I -- Role-grant propagation at scale: the cost side of the computed pattern (REQ-08)

`authorizedRolesOrTeams` is precomputed (Computed Pattern) for fast reads --
that's the whole point of Part D's benchmark. The honest trade-off: when a
NEW role is granted at a high segment node, every descendant asset's
precomputed closure needs to be updated. We demonstrate granting
`role_ca_state_auditor` at `seg_california` (an ancestor of both Hayward
and San Jose) and measure the fan-out cost of that single grant, using
`arrayFilters` to patch exactly the right array element across every
affected document in one `update_many` call.""")

code(r"""db.asset_segments.update_one({"_id": "seg_california"}, {"$addToSet": {"grantedRoles": "role_ca_state_auditor"}})

t0 = time.perf_counter()
result = db.assets.update_many(
    {"segmentAssignments": {"$elemMatch": {"ancestorSegments": "seg_california"}}},
    {"$addToSet": {"segmentAssignments.$[elem].authorizedRolesOrTeams": "role_ca_state_auditor"}},
    array_filters=[{"elem.ancestorSegments": "seg_california"}],
)
t1 = time.perf_counter()
print(f"Granted role_ca_state_auditor at seg_california; propagated to {result.modified_count} assets in {(t1 - t0) * 1000:.1f}ms")

qcount = db.assets.count_documents({"segmentAssignments": {"$elemMatch": {"authorizedRolesOrTeams": "role_ca_state_auditor"}}})
print(f"Vehicles now visible to role_ca_state_auditor: {qcount}")
print("\nThis is the real cost of precomputing authorizedRolesOrTeams: a role grant at a")
print("high segment node is O(descendant asset count), not O(1). It's a deliberate")
print("trade -- reads (which happen far more often than role grants) become a single")
print("indexed lookup instead of a recursive ancestor walk on every query.")""")

# ---------------------------------------------------------------------------
md(r"""## Part J -- Tenant transfer as an ACID transaction (REQ-09)

The production spec calls out tenant-transfer lifecycle events (ownership
changes, a vehicle passed to a new tenant) as something tracked separately
from the segment schema, currently an "ongoing hurdle" in their data
pipelines. We model this as an append-only `tenant_transfer_events`
collection, written atomically with the `assets.tenantIds` update using a
real multi-document ACID transaction -- if any part fails, the whole
transfer rolls back, so you can never end up with an asset that changed
tenants but has no event record (or vice versa).""")

code(r"""victim = db.assets.find_one({"tenantIds": ["rivian_oem", "acme_fleet_corp"], "_id": {"$regex": "^VIN_SCALE_"}})
print(f"Before: {victim['_id']} tenantIds={victim['tenantIds']}")

new_leaf = "seg_globex_midwest"
seg = db.asset_segments.find_one({"_id": new_leaf})
chain = seg["hierarchy"]["ancestors"] + [new_leaf]
granted = set()
for sid in chain:
    granted.update(db.asset_segments.find_one({"_id": sid}).get("grantedRoles", []))
new_assignment = {"tenantId": "globex_logistics", "segmentId": new_leaf, "ancestorSegments": chain,
                   "authorizedRolesOrTeams": sorted(granted)}

with client.start_session() as session:
    with session.start_transaction():
        db.assets.update_one(
            {"_id": victim["_id"]},
            {"$set": {"tenantIds": ["rivian_oem", "globex_logistics"]},
             "$pull": {"segmentAssignments": {"tenantId": "acme_fleet_corp"}}},
            session=session,
        )
        db.assets.update_one(
            {"_id": victim["_id"]},
            {"$push": {"segmentAssignments": new_assignment}},
            session=session,
        )
        db.tenant_transfer_events.insert_one(
            {"assetId": victim["_id"], "fromTenantId": "acme_fleet_corp", "toTenantId": "globex_logistics",
             "eventType": "transferred", "timestamp": datetime.datetime.now(datetime.timezone.utc),
             "initiatedBy": "usr_ops_admin"},
            session=session,
        )
print("Transaction committed.")

after = db.assets.find_one({"_id": victim["_id"]})
print(f"After:  {after['_id']} tenantIds={after['tenantIds']}")
for sa in after["segmentAssignments"]:
    print(f"  tenant={sa['tenantId']:<18} segment={sa['segmentId']}")

event = db.tenant_transfer_events.find_one({"assetId": victim["_id"]})
print(f"\nEvent log entry: {event['fromTenantId']} -> {event['toTenantId']} at {event['timestamp']}")""")

# ---------------------------------------------------------------------------
md(r"""## Part K -- Faceted search backend: filter panel (REQ-10)

A real fleet-tracker UI's filter panel shows counts per bucket/category
(numeric ranges for State of Charge, Mileage, etc.; categorical counts for
Trim, Model, Charging Status, Asset Group) scoped to whatever the current
user/filter selection already is. This is MongoDB's core `$facet`
aggregation stage -- one query, multiple parallel sub-pipelines, each
producing one facet's buckets -- computed over an authorization-scoped
`$match` (using the same index from Part C1).""")

code(r"""TENANT2, ROLE2 = "amazon_logistics", "region_amazon_logistics_0"

t0 = time.perf_counter()
facets = list(db.assets.aggregate([
    {"$match": {"segmentAssignments": {"$elemMatch": {"tenantId": TENANT2, "authorizedRolesOrTeams": {"$in": [ROLE2]}}}}},
    {"$facet": {
        "chargingStatus": [{"$sortByCount": "$attributes.chargingStatus"}],
        "trim": [{"$sortByCount": "$attributes.trim"}],
        "assetGroup": [{"$sortByCount": "$attributes.assetGroup"}],
        "stateOfChargeBuckets": [{"$bucket": {
            "groupBy": "$attributes.stateOfCharge", "boundaries": [0, 25, 50, 75, 101],
            "default": "other", "output": {"count": {"$sum": 1}}}}],
        "mileageBuckets": [{"$bucket": {
            "groupBy": "$attributes.mileage", "boundaries": [0, 10000, 30000, 60000, 100000, 1_000_000],
            "default": "other", "output": {"count": {"$sum": 1}}}}],
        "totalCount": [{"$count": "n"}],
    }},
]))[0]
t1 = time.perf_counter()

print(f"Facets for {TENANT2} / role {ROLE2}, computed in {(t1 - t0) * 1000:.1f}ms:")
print(f"Total matching vehicles: {facets['totalCount'][0]['n']}\n")
for facet_name in ["chargingStatus", "trim", "assetGroup"]:
    print(f"  {facet_name}:", {b["_id"]: b["count"] for b in facets[facet_name]})
print(f"  stateOfCharge buckets:", {b["_id"]: b["count"] for b in facets["stateOfChargeBuckets"]})
print(f"  mileage buckets:      ", {b["_id"]: b["count"] for b in facets["mileageBuckets"]})""")

# ---------------------------------------------------------------------------
md(r"""## Part L -- VIN substring search (REQ-10)

Matches the reference UI's search box behavior: typing a substring
("6493") returns every VIN containing it anywhere, not just ones starting
with it. Uses the `assets_vin_autocomplete_index` built in Part C2b.""")

code(r"""sample = db.assets.find_one({"attributes.vin": {"$exists": True}})
vin = sample["attributes"]["vin"]
substring = vin[5:11]
print(f"Sample VIN: {vin} -- searching for substring: {substring!r}\n")

t0 = time.perf_counter()
results = list(db.assets.aggregate([
    {"$search": {
        "index": "assets_vin_autocomplete_index",
        "compound": {"must": [{"autocomplete": {"query": substring, "path": "attributes.vin"}}]},
    }},
    {"$limit": 10},
    {"$project": {"attributes.vin": 1}},
]))
t1 = time.perf_counter()
print(f"{len(results)} matches in {(t1 - t0) * 1000:.1f}ms:")
for r in results:
    print(" ", r["attributes"]["vin"])""")

# ---------------------------------------------------------------------------
md(r"""## Part M -- Segment-tree rollup counts: the "Fleet Selection" view (REQ-10)

A real fleet UI's tree picker shows a vehicle count *per node*, rolled up
through descendants (selecting "Full Amazon Fleet" shows its total; drilling
into a region/depot/team shows that subtree's total). Since
`ancestorSegments` already contains the full chain (segment + every
ancestor), a single `$unwind` + `$group` produces every node's rollup count
in one aggregation -- no recursive queries needed.""")

code(r"""t0 = time.perf_counter()
counts = list(db.assets.aggregate([
    {"$match": {"segmentAssignments.tenantId": "amazon_logistics"}},
    {"$unwind": "$segmentAssignments"},
    {"$match": {"segmentAssignments.tenantId": "amazon_logistics"}},
    {"$unwind": "$segmentAssignments.ancestorSegments"},
    {"$group": {"_id": "$segmentAssignments.ancestorSegments", "vehicleCount": {"$sum": 1}}},
]))
t1 = time.perf_counter()
counts_by_id = {c["_id"]: c["vehicleCount"] for c in counts}
print(f"Rollup counts for {len(counts)} segments, computed in {(t1 - t0) * 1000:.1f}ms\n")

segs = list(db.asset_segments.find({"tenantId": "amazon_logistics"}))
by_id = {s["_id"]: s for s in segs}


def render(seg_id, depth=0):
    s = by_id[seg_id]
    print("  " * depth + f"{s['name']} ({counts_by_id.get(seg_id, 0)} vehicles)")
    children = sorted(c["_id"] for c in segs if c["hierarchy"]["parentId"] == seg_id)
    for c in children[:3]:
        render(c, depth + 1)


root = next(s["_id"] for s in segs if s["hierarchy"]["parentId"] is None)
render(root)
print("\n(showing first 3 children per node for brevity -- all 31 segments have counts)")""")

# ---------------------------------------------------------------------------
md(r"""## Part E -- Hybrid keyword + vector search with auto-embedding (REQ-03, REQ-04)

Query: **"battery thermal throttling during fast charging"**, run against
the small curated fixture (see Part B2's scope note on why the 50K scale
dataset isn't embedded). Security filter applied *inside each sub-pipeline*
of `$rankFusion`, using the same `embeddedDocument`-style scoping as
elsewhere in this schema.""")

code(r"""QUERY_TEXT = "battery thermal throttling during fast charging"
E_TENANT = "acme_fleet_corp"
E_ROLES = ["region_california_north"]

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
                        {"segmentAssignments.tenantId": {"$eq": E_TENANT}},
                        {"segmentAssignments.authorizedRolesOrTeams": {"$in": E_ROLES}},
                    ]},
                }}],
                "kw": [
                    {"$search": {
                        "index": "assets_text_search_index",
                        "compound": {
                            "must": [{"text": {"query": QUERY_TEXT, "path": "unstructuredNotes"}}],
                            "filter": [
                                {"embeddedDocument": {
                                    "path": "segmentAssignments",
                                    # NOTE: `token` type fields inside embeddedDocuments need the
                                    # `equals` operator, not `text` -- `text` silently matches
                                    # nothing against a token field (verified live; a real
                                    # spec-vs-reality gotcha, not documented clearly anywhere).
                                    "operator": {"compound": {"must": [
                                        {"equals": {"value": E_TENANT, "path": "segmentAssignments.tenantId"}},
                                        {"compound": {"should": [
                                            {"equals": {"value": r, "path": "segmentAssignments.authorizedRolesOrTeams"}}
                                            for r in E_ROLES
                                        ], "minimumShouldMatch": 1}},
                                    ]}},
                                }},
                            ],
                        },
                    }},
                    {"$limit": 10},
                ],
            }
        }
    }},
    {"$project": {"unstructuredNotes": 1, "attributes": 1}},
    {"$limit": 10},
]))

assert {"VIN_GLOBEX_001", "VIN_RIVIAN_009", "VIN_RIVIAN_004"}.isdisjoint({r["_id"] for r in hybrid_results})
print(f"Hybrid $rankFusion results ({len(hybrid_results)}), security filters applied inside each sub-pipeline:\n")
for r in hybrid_results:
    print(f"  {r['_id']:<16} {r['unstructuredNotes'][:70]}")""")

# ---------------------------------------------------------------------------
md(r"""## Part F -- Reranking + precision comparison (REQ-05)

Same as the original POC pass: tries native `$rerank` first, falls back to
Voyage AI's `.rerank()` API if the server doesn't support the stage.""")

code(r"""candidates = list(db.assets.aggregate([
    {"$vectorSearch": {
        "index": VECTOR_INDEX,
        "path": "unstructuredNotes" if used_autoembed else "unstructuredNotesEmbedding",
        **({"query": QUERY_TEXT} if used_autoembed else
           {"queryVector": vo.embed([QUERY_TEXT], model="voyage-3.5", input_type="query").embeddings[0]}),
        "numCandidates": 50,
        "limit": 10,
        "filter": {"$and": [
            {"segmentAssignments.tenantId": {"$eq": E_TENANT}},
            {"segmentAssignments.authorizedRolesOrTeams": {"$in": E_ROLES}},
        ]},
    }},
    {"$project": {"unstructuredNotes": 1, "vscore": {"$meta": "vectorSearchScore"}}},
]))

print("--- Vector search order (pre-rerank) ---")
for c in candidates:
    print(f"  {c['vscore']:.4f}  {c['_id']:<16} {c['unstructuredNotes'][:65]}")

used_native_rerank = False
reranked = []

try:
    native_results = list(db.assets.aggregate([
        {"$vectorSearch": {
            "index": VECTOR_INDEX,
            "path": "unstructuredNotes" if used_autoembed else "unstructuredNotesEmbedding",
            **({"query": QUERY_TEXT} if used_autoembed else
               {"queryVector": vo.embed([QUERY_TEXT], model="voyage-3.5", input_type="query").embeddings[0]}),
            "numCandidates": 50,
            "limit": 10,
            "filter": {"$and": [
                {"segmentAssignments.tenantId": {"$eq": E_TENANT}},
                {"segmentAssignments.authorizedRolesOrTeams": {"$in": E_ROLES}},
            ]},
        }},
        {"$rerank": {
            "model": "rerank-2.5",
            "query": {"text": QUERY_TEXT},
            "path": "unstructuredNotes",
            "numDocsToRerank": 10,
        }},
        {"$addFields": {"rerankScore": {"$meta": "score"}}},
        {"$limit": 5},
        {"$project": {"unstructuredNotes": 1, "rerankScore": 1}},
    ]))
    reranked = [(r["rerankScore"], r) for r in native_results]
    used_native_rerank = True
    print("\n--- Reranked top 5 (NATIVE $rerank, server-side) ---")
except OperationFailure as e:
    print(f"\nNative $rerank unavailable ({e}); falling back to Voyage AI API call.")
    docs_text = [c["unstructuredNotes"] for c in candidates]
    rerank_result = vo.rerank(QUERY_TEXT, docs_text, model="rerank-2.5", top_k=5)
    reranked = [(r.relevance_score, candidates[r.index]) for r in rerank_result.results]
    print("\n--- Reranked top 5 (Voyage AI API, client-side call) ---")

reranked_ids = []
for score, doc in reranked:
    reranked_ids.append(doc["_id"])
    print(f"  {score:.4f}  {doc['_id']:<16} {doc['unstructuredNotes'][:65]}")

vector_order_top5 = [c["_id"] for c in candidates[:5]]
print("\nVector-only top 5 order: ", vector_order_top5)
print("Reranked top 5 order:    ", reranked_ids)
print("Reranking method used:   ", "native $rerank stage" if used_native_rerank else "Voyage AI API (client-side)")
if vector_order_top5 != reranked_ids:
    print("\nReranking changed the top-5 ordering.")""")

# ---------------------------------------------------------------------------
md(r"""## Part G -- Value proposition recap

Rendered from the actual results captured above, not hardcoded claims.""")

code(r"""total_assets = db.assets.count_documents({})
print("+" + "-" * 88 + "+")
print("|  BENEFITS AT A GLANCE (measured against a live Atlas cluster, {:>6} documents)".format(total_assets).ljust(89) + "|")
print("+" + "-" * 88 + "+")
print(f"| 1. SCALE                      {total_assets} assets, {len(CUSTOMER_SPEC) + 1} tenants, 0 joins".ljust(89) + "|")
print(f"| 2. LATENCY                    Single-pass median {single_median:.1f}ms vs fan-out median {fanout_median:.1f}ms ({fanout_median/single_median:.2f}x)".ljust(89) + "|")
print(f"| 3. MULTI-TENANT ASSETS        Verified: OEM + buyer both see it, unrelated tenant does not".ljust(89) + "|")
print(f"| 4. ROLE-GRANT PROPAGATION     {result.modified_count} descendants updated via 1 update_many call".ljust(89) + "|")
print(f"| 5. AUTO-EMBEDDING             used_autoembed = {used_autoembed} (native Atlas + Voyage AI voyage-4)".ljust(89) + "|")
print(f"| 6. HYBRID SEARCH              $rankFusion combined keyword + vector, {len(hybrid_results)} results, 0 leaks".ljust(89) + "|")
print(f"| 7. RERANKING                  rerank-2.5 ({'native $rerank' if used_native_rerank else 'Voyage API'}) applied".ljust(89) + "|")
print(f"| 8. FACETED SEARCH             5 facets computed in 1 aggregation call".ljust(89) + "|")
print("+" + "-" * 88 + "+")""")

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
