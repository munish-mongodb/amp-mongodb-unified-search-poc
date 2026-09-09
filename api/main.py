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
from pathlib import Path
from typing import Optional

import certifi
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MONGODB_URI = os.environ["MONGODB_URI"]
MONGODB_DB = os.environ.get("MONGODB_DB", "amp_poc_db")

client = MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
db = client[MONGODB_DB]

app = FastAPI(title="AMP Fleet API (POC)", version="0.1.0")


def auth_filter(tenant: str, role: str) -> dict:
    """The single-pass authorization filter proven in notebook Part D --
    every endpoint below scopes through this, never returning data the
    caller's tenant+role isn't entitled to."""
    return {"segmentAssignments": {"$elemMatch": {"tenantId": tenant, "authorizedRolesOrTeams": role}}}


@app.get("/health")
def health():
    return {"status": "ok", "assets": db.assets.estimated_document_count()}


@app.get("/vehicles")
def list_vehicles(
    tenant: str,
    role: str,
    page: int = Query(1, ge=1),
    pageSize: int = Query(25, ge=1, le=200),
    chargingStatus: Optional[str] = None,
    trim: Optional[str] = None,
    model: Optional[str] = None,
    assetGroup: Optional[str] = None,
    minSOC: Optional[int] = None,
    maxSOC: Optional[int] = None,
):
    """Paginated vehicle list -- the "Vehicle Tracker" table. Matches the
    reference UI's "Showing X / Y vehicles" pattern via totalCount."""
    query = auth_filter(tenant, role)
    if chargingStatus:
        query["attributes.chargingStatus"] = chargingStatus
    if trim:
        query["attributes.trim"] = trim
    if model:
        query["attributes.model"] = model
    if assetGroup:
        query["attributes.assetGroup"] = assetGroup
    if minSOC is not None or maxSOC is not None:
        soc_range = {}
        if minSOC is not None:
            soc_range["$gte"] = minSOC
        if maxSOC is not None:
            soc_range["$lte"] = maxSOC
        query["attributes.stateOfCharge"] = soc_range

    total = db.assets.count_documents(query)
    docs = list(
        db.assets.find(query, {"_id": 1, "attributes": 1})
        .skip((page - 1) * pageSize)
        .limit(pageSize)
    )
    vehicles = [{"vin": d["_id"], **d["attributes"]} for d in docs]
    return {"page": page, "pageSize": pageSize, "totalCount": total, "vehicles": vehicles}


@app.get("/vehicles/search")
def search_vehicles(vin: str, tenant: str, role: str, limit: int = Query(10, ge=1, le=50)):
    """VIN substring search via the Atlas Search autocomplete index --
    matches the reference UI's "type any substring, get matches" behavior."""
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
    return {"vehicles": [{"vin": r["_id"], **r["attributes"]} for r in results]}


@app.get("/facets")
def get_facets(tenant: str, role: str):
    """Filter-panel facet counts -- numeric range buckets + categorical
    counts, scoped to the caller's authorization, computed in a single
    $facet aggregation (notebook Part K)."""
    query = auth_filter(tenant, role)
    result = list(db.assets.aggregate([
        {"$match": query},
        {"$facet": {
            "chargingStatus": [{"$sortByCount": "$attributes.chargingStatus"}],
            "trim": [{"$sortByCount": "$attributes.trim"}],
            "model": [{"$sortByCount": "$attributes.model"}],
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

    def as_dict(bucket_list):
        return {str(b["_id"]): b["count"] for b in bucket_list}

    return {
        "totalCount": result["totalCount"][0]["n"] if result["totalCount"] else 0,
        "chargingStatus": as_dict(result["chargingStatus"]),
        "trim": as_dict(result["trim"]),
        "model": as_dict(result["model"]),
        "assetGroup": as_dict(result["assetGroup"]),
        "stateOfChargeBuckets": as_dict(result["stateOfChargeBuckets"]),
        "mileageBuckets": as_dict(result["mileageBuckets"]),
    }


@app.get("/segments/tree")
def segment_tree(tenant: str):
    """The "Fleet Selection" tree -- every segment for a tenant, with a
    rolled-up vehicle count per node (notebook Part M)."""
    segs = list(db.asset_segments.find({"tenantId": tenant}))
    if not segs:
        raise HTTPException(status_code=404, detail=f"No segments found for tenant '{tenant}'")

    counts = list(db.assets.aggregate([
        {"$match": {"segmentAssignments.tenantId": tenant}},
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
