#!/usr/bin/env python3
"""Minimal FastAPI service exposing the MongoDB queries/aggregations proven
in the notebook as real HTTP JSON endpoints -- the shapes a fleet-tracker
frontend (see README screenshots reference) would actually consume.

This is deliberately small: no auth system, no ORM, no request validation
beyond FastAPI's built-in typing. `tenant`/`role` are accepted as plain
query params standing in for what a real deployment would pull from an
authenticated session/JWT -- the point here is to prove the MongoDB query
patterns are wireable behind a real API, not to build Rivian's actual
product.

Run:
    pip install fastapi uvicorn
    uvicorn api.main:app --reload --port 8000

Then e.g.:
    curl "http://localhost:8000/vehicles?tenant=amazon_logistics&role=region_amazon_logistics_0&page=1&pageSize=10"
    curl "http://localhost:8000/facets?tenant=amazon_logistics&role=region_amazon_logistics_0"
    curl "http://localhost:8000/segments/tree?tenant=amazon_logistics"
    curl "http://localhost:8000/vehicles/search?vin=6493&tenant=amazon_logistics&role=region_amazon_logistics_0"
"""
import os
import random
import re
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import certifi
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from topology import MODEL_TO_RIVIAN_LINE_SEGMENT, SCHEMA_VERSION, compute_assignment  # noqa: E402

load_dotenv(ROOT / ".env")

MONGODB_URI = os.environ["MONGODB_URI"]
MONGODB_DB = os.environ.get("MONGODB_DB", "amp_poc_db")

client = MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
db = client[MONGODB_DB]

app = FastAPI(title="AMP Fleet API (POC)", version="0.1.0")

# Demo-only: the reference frontend (frontend/index.html) calls this API
# directly from the browser. A real deployment would restrict this to its
# actual frontend origin (and wouldn't need it at all if same-origin, which
# is also supported below via StaticFiles).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def auth_filter(tenant: str, role: str, segment: Optional[str] = None) -> dict:
    """The single-pass authorization filter proven in notebook Part D --
    every endpoint below scopes through this, never returning data the
    caller's tenant+role isn't entitled to. `segment`, if given, further
    narrows to a specific node's subtree (used by the Fleet Selection tree
    in the frontend) via the same ancestorSegments membership check used
    for the rollup-count aggregation in notebook Part M.

    Scoped to `assetType: "vehicle"` -- this API surface is specifically
    the "Vehicle Tracker" (matching the reference UI), and `assets` is
    polymorphic (also holds ev_charger/e_bike docs, per REQ-02). Without
    this, /vehicles would return chargers and e-bikes mixed into a table
    whose columns (Model/Trim/Year) don't apply to them -- a real bug this
    project caught by testing detail-view lookups, not by inspection."""
    elem_match = {"tenantId": tenant, "authorizedRolesOrTeams": role}
    if segment:
        elem_match["ancestorSegments"] = segment
    return {"assetType": "vehicle", "segmentAssignments": {"$elemMatch": elem_match}}


@app.get("/tenants")
def list_tenants():
    """Tenant picker -- stands in for "which org is this session logged
    into" in a real deployment."""
    return {"tenants": list(db.tenants.find({}, {"_id": 1, "name": 1, "type": 1}))}


@app.get("/tenants/{tenant}/roles")
def list_roles(tenant: str):
    """Distinct roles granted anywhere in this tenant's segment tree --
    stands in for "which roles could a logged-in user have" in a real
    deployment (there's no user/session model in this POC)."""
    roles = db.asset_segments.distinct("grantedRoles", {"tenantId": tenant})
    if not roles:
        raise HTTPException(status_code=404, detail=f"No roles found for tenant '{tenant}'")
    return {"tenant": tenant, "roles": sorted(roles)}


@app.get("/health")
def health():
    return {"status": "ok", "assets": db.assets.estimated_document_count()}


@app.get("/vehicles")
def list_vehicles(
    tenant: str,
    role: str,
    segment: Optional[str] = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(25, ge=1, le=200),
    afterId: Optional[str] = None,
    # categorical filters -- one per CATEGORICAL_FACETS entry
    chargingStatus: Optional[str] = None,
    trim: Optional[str] = None,
    model: Optional[str] = None,
    assetGroup: Optional[str] = None,
    year: Optional[int] = None,
    make: Optional[str] = None,
    # numeric range filters -- one min/max pair per NUMERIC_FACETS entry
    minSOC: Optional[int] = None,
    maxSOC: Optional[int] = None,
    minMileage: Optional[int] = None,
    maxMileage: Optional[int] = None,
    minDistanceToEmpty: Optional[int] = None,
    maxDistanceToEmpty: Optional[int] = None,
    minSpeed: Optional[int] = None,
    maxSpeed: Optional[int] = None,
    minSOH: Optional[float] = None,
    maxSOH: Optional[float] = None,
):
    """Paginated vehicle list -- the "Vehicle Tracker" table. Matches the
    reference UI's "Showing X / Y vehicles" pattern via totalCount. Filter
    params mirror every facet in /facets (see CATEGORICAL_FACETS /
    NUMERIC_FACETS below).

    Two pagination modes:
    - `page`/`pageSize` (default): skip/limit, for the UI's page-number
      navigation. Cost grows with page depth -- fine at this data volume
      (measured live in notebook Part D3), a real concern at much larger
      scale or very deep pagination.
    - `afterId`: range/keyset pagination (`_id > afterId`, sorted by `_id`).
      Cost stays flat regardless of depth, at the cost of no direct
      "jump to page N" -- the right choice for programmatic/infinite-scroll
      consumers, not exposed in this reference UI but proven out in the
      notebook alongside the skip/limit comparison.
    """
    query = auth_filter(tenant, role, segment)
    categorical = {"chargingStatus": chargingStatus, "trim": trim, "model": model,
                   "assetGroup": assetGroup, "year": year, "make": make}
    for field, value in categorical.items():
        if value is not None:
            query[f"attributes.{field}"] = value

    ranges = {
        "stateOfCharge": (minSOC, maxSOC),
        "mileage": (minMileage, maxMileage),
        "distanceToEmptyMiles": (minDistanceToEmpty, maxDistanceToEmpty),
        "vehicleSpeed": (minSpeed, maxSpeed),
        "hvBatterySOH": (minSOH, maxSOH),
    }
    for field, (lo, hi) in ranges.items():
        if lo is not None or hi is not None:
            range_clause = {}
            if lo is not None:
                range_clause["$gte"] = lo
            if hi is not None:
                range_clause["$lte"] = hi
            query[f"attributes.{field}"] = range_clause

    total = db.assets.count_documents(query)

    if afterId is not None:
        cursor_query = dict(query)
        cursor_query["_id"] = {"$gt": afterId}
        docs = list(db.assets.find(cursor_query, {"_id": 1, "attributes": 1}).sort("_id", 1).limit(pageSize))
        # attributes.vin is the real vehicle VIN shown/used by the frontend and
        # detail/update/delete lookups below; _id is a separate internal doc key.
        vehicles = [dict(d["attributes"]) for d in docs]
        return {"pageSize": pageSize, "totalCount": total, "vehicles": vehicles,
                "nextAfterId": docs[-1]["_id"] if docs else None}

    docs = list(
        db.assets.find(query, {"_id": 1, "attributes": 1})
        .sort("_id", 1)
        .skip((page - 1) * pageSize)
        .limit(pageSize)
    )
    vehicles = [dict(d["attributes"]) for d in docs]
    return {"page": page, "pageSize": pageSize, "totalCount": total, "vehicles": vehicles}


VIN_AUTOCOMPLETE_MAX_GRAMS = 7  # must match assets_vin_autocomplete_index's maxGrams


@app.get("/vehicles/search")
def search_vehicles(vin: str, tenant: str, role: str, limit: int = Query(10, ge=1, le=50)):
    """VIN substring search -- matches the reference UI's "type any
    substring, get matches" behavior.

    **Real bug found by testing, not review**: Atlas Search's `autocomplete`
    operator only guarantees correctness for query strings up to its
    `maxGrams` setting (7 here). Pasting a *full* 17-character VIN returned
    dozens of unrelated vehicles, all scored identically -- because the
    operator fragments a longer query into its own 3-7 character grams
    internally and matches documents sharing *any* one of them, not the
    literal full string. Every synthetic VIN in this dataset also happens
    to share a literal 7-character prefix (realistic -- real VINs share a
    manufacturer WMI code across a whole fleet too), which made this
    especially visible: searching a full VIN matched almost the entire
    tenant's fleet.

    Fix: `autocomplete` is used only for genuinely short, typeahead-style
    queries (<= maxGrams) where it's fast and already verified correct.
    For anything longer, fall back to scoping by the same auth-filter
    index used everywhere else in this API, then an exact case-insensitive
    substring match within that already-narrowed candidate set --
    guaranteed correct, and fast enough at this data volume (~50-120ms
    against a ~10,000-doc tenant) without needing search-index tuning."""
    if len(vin) <= VIN_AUTOCOMPLETE_MAX_GRAMS:
        results = list(db.assets.aggregate([
            {"$search": {
                "index": "assets_vin_autocomplete_index",
                "compound": {
                    "must": [{"autocomplete": {"query": vin, "path": "attributes.vin"}}],
                    # `token` type fields inside embeddedDocuments need `equals`, not `text`
                    # (verified live -- `text` silently matches nothing against a token field).
                    "filter": [{"embeddedDocument": {
                        "path": "segmentAssignments",
                        "operator": {"compound": {"must": [
                            {"equals": {"value": tenant, "path": "segmentAssignments.tenantId"}},
                            {"equals": {"value": role, "path": "segmentAssignments.authorizedRolesOrTeams"}},
                        ]}},
                    }}],
                },
            }},
            {"$limit": limit},
            {"$project": {"attributes": 1}},
        ]))
        return {"vehicles": [dict(r["attributes"]) for r in results], "method": "autocomplete"}

    escaped = re.escape(vin)
    results = list(
        db.assets.find(
            {**auth_filter(tenant, role), "attributes.vin": {"$regex": escaped, "$options": "i"}},
            {"attributes": 1},
        ).limit(limit)
    )
    return {"vehicles": [dict(r["attributes"]) for r in results], "method": "regex_after_auth_filter"}


# Fields an authenticated caller is allowed to edit via PATCH. Deliberately
# excludes _id/vin, model, tenantIds, and segmentAssignments -- those change
# the document's identity or authorization shape and need dedicated
# operations (tenant transfer = notebook Part J's transaction pattern;
# re-segmenting = its own move operation), not a generic field-level PATCH.
EDITABLE_FIELDS = {"trim", "color", "chargingStatus", "assetGroup", "stateOfCharge",
                   "distanceToEmptyMiles", "mileage", "vehicleSpeed", "hvBatterySOH"}


class VehicleUpdate(BaseModel):
    trim: Optional[str] = None
    color: Optional[str] = None
    chargingStatus: Optional[str] = None
    assetGroup: Optional[str] = None
    stateOfCharge: Optional[int] = None
    distanceToEmptyMiles: Optional[int] = None
    mileage: Optional[int] = None
    vehicleSpeed: Optional[int] = None
    hvBatterySOH: Optional[float] = None


class VehicleCreate(BaseModel):
    tenant: str
    role: str  # must be a role actually granted somewhere in tenant's segment tree
    model: str  # "R1T" | "R1S" | "RPV"
    segmentId: str  # a leaf segment in the tenant's own hierarchy (see /segments/tree)
    trim: Optional[str] = None
    color: str = "Rivian Blue"
    assetGroup: str = "Depot Standby"


def _get_authorized_vehicle_or_404(vin: str, tenant: str, role: str) -> dict:
    """Shared auth check for the single-vehicle endpoints: 404 (not just
    403) for both "doesn't exist" and "exists but you can't see it" --
    consistent with not revealing cross-tenant existence, same principle
    REQ-01's authorization query already enforces for list/search.

    Looks up by `attributes.vin` (the real vehicle VIN, what's displayed
    and clicked in the frontend), NOT the document's `_id` -- those are
    different values in this schema (`_id` is an internal doc key, e.g.
    `VIN_SCALE_000001`; `attributes.vin` is the vehicle's actual VIN, e.g.
    `7FCEHEB...`). Getting this wrong was a real bug caught by testing the
    detail-view click-through, not by code review: GET/PATCH/DELETE all
    404'd on every vehicle until this was fixed."""
    query = {"attributes.vin": vin, **auth_filter(tenant, role)}
    doc = db.assets.find_one(query)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Vehicle '{vin}' not found or not authorized for this tenant/role")
    return doc


@app.get("/vehicles/{vin}")
def get_vehicle(vin: str, tenant: str, role: str):
    """Full detail view for a single vehicle -- what clicking a VIN in the
    reference UI opens. Includes every segment assignment (so a multi-tenant
    vehicle's placement in *each* tenant's hierarchy is visible, not just the
    caller's), plus its tenant-transfer history."""
    doc = _get_authorized_vehicle_or_404(vin, tenant, role)
    history = list(db.tenant_transfer_events.find({"assetId": doc["_id"]}, {"_id": 0}).sort("timestamp", -1))
    return {
        "vin": vin,
        "assetId": doc["_id"],
        "schemaVersion": doc.get("schemaVersion"),
        "tenantIds": doc["tenantIds"],
        "attributes": doc["attributes"],
        "segmentAssignments": doc["segmentAssignments"],
        "transferHistory": history,
    }


@app.patch("/vehicles/{vin}")
def update_vehicle(vin: str, tenant: str, role: str, update: VehicleUpdate):
    """Update a subset of attribute fields (Update, in CRUD terms). Requires
    the same tenant+role authorization as reading it -- there's no separate
    "read-only vs. read-write" role distinction in this POC's simplified
    auth model."""
    doc = _get_authorized_vehicle_or_404(vin, tenant, role)
    changes = {f"attributes.{k}": v for k, v in update.model_dump(exclude_none=True).items()}
    if not changes:
        raise HTTPException(status_code=400, detail="No editable fields provided")
    db.assets.update_one({"_id": doc["_id"]}, {"$set": changes})
    return get_vehicle(vin, tenant, role)


@app.delete("/vehicles/{vin}")
def delete_vehicle(vin: str, tenant: str, role: str):
    """Delete a vehicle (Delete, in CRUD terms). This is a real hard delete
    for POC simplicity -- a production fleet system would more likely
    soft-delete (a `status: "decommissioned"` field, filtered out of normal
    queries but retained for audit/compliance) rather than actually remove
    the document, especially given `tenant_transfer_events` already
    establishes the pattern of keeping an audit trail for lifecycle
    changes. Noted here rather than implemented, to keep this endpoint's
    behavior unambiguous for the demo."""
    doc = _get_authorized_vehicle_or_404(vin, tenant, role)
    db.assets.delete_one({"_id": doc["_id"]})
    return {"deleted": vin}


@app.post("/vehicles", status_code=201)
def create_vehicle(body: VehicleCreate):
    """Create a new vehicle (Create, in CRUD terms). Builds the same
    schema shape as scripts/generate_fleet_data.py: a rivian_oem
    segmentAssignment (by vehicle line) plus the owning tenant's
    segmentAssignment (computed via topology.compute_assignment, so
    authorizedRolesOrTeams is derived the same way everywhere in this
    project, not hand-set here)."""
    if body.model not in MODEL_TO_RIVIAN_LINE_SEGMENT:
        raise HTTPException(status_code=400, detail=f"model must be one of {list(MODEL_TO_RIVIAN_LINE_SEGMENT)}")

    tenant_roles = db.asset_segments.distinct("grantedRoles", {"tenantId": body.tenant})
    if body.role not in tenant_roles:
        raise HTTPException(status_code=403, detail=f"role '{body.role}' is not granted anywhere in tenant '{body.tenant}'")

    segment = db.asset_segments.find_one({"_id": body.segmentId, "tenantId": body.tenant})
    if not segment:
        raise HTTPException(status_code=404, detail=f"segment '{body.segmentId}' not found for tenant '{body.tenant}'")

    segments_by_id = {s["_id"]: s for s in db.asset_segments.find({})}
    tenant_assignment = compute_assignment(body.tenant, body.segmentId, segments_by_id)
    rivian_assignment = compute_assignment("rivian_oem", MODEL_TO_RIVIAN_LINE_SEGMENT[body.model], segments_by_id)

    # verify the creating role can actually see the segment it's assigning into
    if body.role not in tenant_assignment["authorizedRolesOrTeams"]:
        raise HTTPException(status_code=403,
                             detail=f"role '{body.role}' is not authorized for segment '{body.segmentId}'")

    # _id is an internal document key (matches the "VIN_API_..." / "VIN_SCALE_..."
    # convention used elsewhere in this project); attributes.vin is the
    # realistic-looking VIN a real system would actually assign and that
    # every other lookup in this API (GET/PATCH/DELETE, search) uses.
    asset_id = "VIN_API_" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
    vin = "7FCEHEB" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
    trim_defaults = {"R1T": "Adventure", "R1S": "Adventure", "RPV": "EDV-500"}
    battery_defaults = {"R1T": 135, "R1S": 149, "RPV": 118}
    doc = {
        "_id": asset_id,
        "schemaVersion": SCHEMA_VERSION,
        "tenantIds": ["rivian_oem", body.tenant],
        "assetType": "vehicle",
        "attributes": {
            "make": "RIVIAN",
            "model": body.model,
            "trim": body.trim or trim_defaults[body.model],
            "color": body.color,
            "vin": vin,
            "year": datetime.now(timezone.utc).year,
            "firmwareVersion": "v2026.12.4",
            "batteryCapacityKw": battery_defaults[body.model],
            "mileage": 0,
            "stateOfCharge": 100,
            "distanceToEmptyMiles": {"R1T": 350, "R1S": 340, "RPV": 165}[body.model],
            "chargingStatus": "Charging Complete",
            "vehicleSpeed": 0,
            "hvBatterySOH": 100.0,
            "assetGroup": body.assetGroup,
        },
        "segmentAssignments": [rivian_assignment, tenant_assignment],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    db.assets.insert_one(doc)
    return get_vehicle(vin, body.tenant, body.role)


# Categorical facets: field name -> attributes.<path>
CATEGORICAL_FACETS = {
    "chargingStatus": "chargingStatus",
    "trim": "trim",
    "model": "model",
    "assetGroup": "assetGroup",
    "year": "year",
    "make": "make",
}
# Numeric-range facets: field name -> (attributes.<path>, bucket boundaries)
NUMERIC_FACETS = {
    "stateOfChargeBuckets": ("stateOfCharge", [0, 25, 50, 75, 101]),
    "mileageBuckets": ("mileage", [0, 10000, 30000, 60000, 100000, 1_000_000]),
    "distanceToEmptyBuckets": ("distanceToEmptyMiles", [0, 50, 100, 150, 200, 1000]),
    "vehicleSpeedBuckets": ("vehicleSpeed", [0, 1, 26, 51, 76, 1000]),
    "hvBatterySOHBuckets": ("hvBatterySOH", [78, 85, 90, 95, 100.1]),
}


@app.get("/facets")
def get_facets(tenant: str, role: str, segment: Optional[str] = None):
    """Filter-panel facet counts -- numeric range buckets + categorical
    counts, scoped to the caller's authorization, computed in a single
    $facet aggregation (notebook Part K). Covers every filter category in
    the reference UI's filter panel (Name is the one exception -- it's a
    display label derived from VIN, not a distinct field; VIN search
    already covers that use case)."""
    query = auth_filter(tenant, role, segment)
    facet_stage = {}
    for name, path in CATEGORICAL_FACETS.items():
        facet_stage[name] = [{"$sortByCount": f"$attributes.{path}"}]
    for name, (path, boundaries) in NUMERIC_FACETS.items():
        facet_stage[name] = [{"$bucket": {
            "groupBy": f"$attributes.{path}", "boundaries": boundaries,
            "default": "other", "output": {"count": {"$sum": 1}}}}]
    facet_stage["totalCount"] = [{"$count": "n"}]

    result = list(db.assets.aggregate([{"$match": query}, {"$facet": facet_stage}]))[0]

    def as_dict(bucket_list):
        return {str(b["_id"]): b["count"] for b in bucket_list}

    out = {"totalCount": result["totalCount"][0]["n"] if result["totalCount"] else 0}
    for name in list(CATEGORICAL_FACETS) + list(NUMERIC_FACETS):
        out[name] = as_dict(result[name])
    return out


@app.get("/segments/tree")
def segment_tree(tenant: str):
    """The "Fleet Selection" tree -- every segment for a tenant, with a
    rolled-up vehicle count per node (notebook Part M)."""
    segs = list(db.asset_segments.find({"tenantId": tenant}))
    if not segs:
        raise HTTPException(status_code=404, detail=f"No segments found for tenant '{tenant}'")

    counts = list(db.assets.aggregate([
        # assetType filter matters here too -- ev_charger/e_bike docs also
        # carry segmentAssignments for acme_fleet_corp/globex_logistics, and
        # this endpoint's counts are explicitly labeled "vehicleCount".
        {"$match": {"assetType": "vehicle", "segmentAssignments.tenantId": tenant}},
        {"$unwind": "$segmentAssignments"},
        {"$match": {"segmentAssignments.tenantId": tenant}},
        {"$unwind": "$segmentAssignments.ancestorSegments"},
        {"$group": {"_id": "$segmentAssignments.ancestorSegments", "vehicleCount": {"$sum": 1}}},
    ]))
    counts_by_id = {c["_id"]: c["vehicleCount"] for c in counts}

    by_parent: dict = {}
    for s in segs:
        by_parent.setdefault(s["hierarchy"]["parentId"], []).append(s)

    def build(seg_id):
        s = next(x for x in segs if x["_id"] == seg_id)
        return {
            "id": s["_id"],
            "name": s["name"],
            "segmentType": s["segmentType"],
            "vehicleCount": counts_by_id.get(seg_id, 0),
            "children": [build(c["_id"]) for c in by_parent.get(seg_id, [])],
        }

    roots = by_parent.get(None, [])
    return {"tenant": tenant, "tree": [build(r["_id"]) for r in roots]}


# Serve the reference frontend (frontend/index.html) at "/" -- mounted last
# so it doesn't shadow the API routes above. Running same-origin like this
# means the frontend doesn't even need the CORS middleware in practice; it's
# kept above for anyone who wants to serve the frontend separately instead.
FRONTEND_DIR = ROOT / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
