#!/usr/bin/env python3
"""Generate a production-scale synthetic fleet (~50,000 vehicles) and bulk
insert it into the same collections as the curated fixture (data/*.json).

This is deliberately NOT committed as static JSON -- 50K documents is tens of
MB, and there's no reason to version-control randomly generated data. Run
this script against a live cluster; it caches its own output locally
(gitignored) so re-runs are fast and reproducible without hitting Atlas
again unless --reload is passed.

Scope decisions (see README for full rationale):
- Only vehicles are generated at scale (the user's ask). ev_chargers/e_bikes
  stay on the small curated fixture only.
- Generated vehicles do NOT get `unstructuredNotes` / embeddings -- the
  hybrid-search/rerank demos (REQ-03/04/05) stay on the small curated
  corpus where the trap-document semantics are meaningful. Embedding 50K
  docs via Voyage would cost real time/money for no additional signal.
- Rivian OEM is present in every vehicle's tenantIds (it manufactured all of
  them); each vehicle also gets exactly one fleet-customer tenant.

Usage:
    python scripts/generate_fleet_data.py [--count 50000] [--reload]
"""
import argparse
import json
import os
import random
from pathlib import Path

import certifi
from dotenv import load_dotenv
from pymongo import MongoClient

from topology import (
    MODEL_TO_RIVIAN_LINE_SEGMENT,
    TENANTS,
    compute_assignment,
    fleet_customer_segments,
    rivian_oem_segments,
)

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MONGODB_URI = os.environ["MONGODB_URI"]
MONGODB_DB = os.environ.get("MONGODB_DB", "amp_poc_db")

CACHE_DIR = ROOT / "data" / "generated"
ASSETS_CACHE = CACHE_DIR / "assets_50k.jsonl"
SEGMENTS_CACHE = CACHE_DIR / "segments_scale.json"

# fleet-customer tenant -> (label, vehicle line mix, hierarchy shape)
CUSTOMER_SPEC = {
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


def random_vehicle_attrs(rng: random.Random, vin: str, model: str, year: int) -> dict:
    max_range = MAX_RANGE_MILES[model]
    soc = rng.randint(15, 100)
    distance_to_empty = max(1, round(max_range * (soc / 100) * rng.uniform(0.9, 1.05)))
    age_years = max(0, 2027 - year)
    mileage = max(50, round(rng.uniform(6000, 24000) * age_years + rng.uniform(-1500, 1500)))
    hv_battery_soh = round(max(78.0, 100.0 - age_years * rng.uniform(1.2, 3.0)), 1)
    return {
        "make": "RIVIAN",
        "model": model,
        "trim": rng.choice(TRIMS_BY_MODEL[model]),
        "color": rng.choice(COLORS),
        "vin": vin,
        "year": year,
        "firmwareVersion": rng.choice(["v2026.12.4", "v2026.11.2", "v2026.10.1"]),
        "batteryCapacityKw": {"R1T": 135, "R1S": 149, "RPV": 118}[model],
        "mileage": mileage,
        "stateOfCharge": soc,
        "distanceToEmptyMiles": distance_to_empty,
        "chargingStatus": rng.choice(CHARGING_STATUSES),
        "vehicleSpeed": 0 if rng.random() < 0.7 else rng.randint(1, 75),
        "hvBatterySOH": hv_battery_soh,
        "assetGroup": rng.choice(ASSET_GROUPS),
    }


def make_vin(rng: random.Random, idx: int) -> str:
    # Loosely mimics real 17-char VIN shape; not a real check-digit VIN, just
    # unique and plausible-looking for search/autocomplete demos.
    body = "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ0123456789") for _ in range(9))
    return f"7FCEHEB{body}{idx:06d}"[:17]


def build_scale_segments() -> tuple[list[dict], dict]:
    """Rivian OEM hierarchy + one procedurally-generated hierarchy per NEW
    fleet-customer tenant (amazon/dhl/driveshare). acme_fleet_corp and
    globex_logistics keep their existing curated hierarchy from
    data/segments_seed.json (loaded separately) -- we don't regenerate those,
    just reuse their leaf team IDs for vehicle distribution.
    """
    segments = list(rivian_oem_segments())
    leaf_teams_by_tenant: dict[str, list[str]] = {}
    for tenant_id in ["amazon_logistics", "dhl_express_fleet", "driveshare_rentals"]:
        label = CUSTOMER_SPEC[tenant_id]["label"]
        nodes, leaves = fleet_customer_segments(tenant_id, label)
        segments.extend(nodes)
        leaf_teams_by_tenant[tenant_id] = leaves
    return segments, leaf_teams_by_tenant


def load_curated_leaf_teams() -> dict[str, list[str]]:
    """acme_fleet_corp / globex_logistics already have a curated hierarchy in
    data/segments_seed.json. Compute their true leaf nodes (segments with no
    children) so the generator can distribute vehicles across them --
    globex's curated hierarchy is only 2 levels deep (no team-level nodes),
    so a hardcoded segmentType=="team" filter would wrongly return zero
    leaves for it."""
    with open(ROOT / "data" / "segments_seed.json") as f:
        existing = json.load(f)
    tenants = {"acme_fleet_corp", "globex_logistics"}
    relevant = [s for s in existing if s["tenantId"] in tenants]
    parent_ids = {s["hierarchy"]["parentId"] for s in relevant if s["hierarchy"]["parentId"]}
    leaves: dict[str, list[str]] = {t: [] for t in tenants}
    for seg in relevant:
        if seg["_id"] not in parent_ids and seg["segmentType"] != "restricted":
            leaves[seg["tenantId"]].append(seg["_id"])
    return leaves


def generate(count: int, seed: int = 42) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    scale_segments, generated_leaves = build_scale_segments()
    curated_leaves = load_curated_leaf_teams()

    with open(ROOT / "data" / "segments_seed.json") as f:
        curated_segments = json.load(f)

    all_segments = curated_segments + scale_segments
    segments_by_id = {s["_id"]: s for s in all_segments}

    leaf_teams_by_tenant = {**curated_leaves, **generated_leaves}

    assets = []
    idx = 0
    for tenant_id, spec in CUSTOMER_SPEC.items():
        leaves = leaf_teams_by_tenant[tenant_id]
        for _ in range(spec["count"]):
            idx += 1
            model = "RPV" if rng.random() < spec["rpv_share"] else rng.choice(["R1T", "R1S"])
            year = rng.choice([2023, 2023, 2024, 2024, 2025, 2026])
            vin = make_vin(rng, idx)
            asset_id = f"VIN_SCALE_{idx:06d}"
            leaf_segment = rng.choice(leaves)

            customer_assignment = compute_assignment(tenant_id, leaf_segment, segments_by_id)
            rivian_line_segment = MODEL_TO_RIVIAN_LINE_SEGMENT[model]
            rivian_assignment = compute_assignment("rivian_oem", rivian_line_segment, segments_by_id)

            asset = {
                "_id": asset_id,
                "schemaVersion": 2,
                "tenantIds": ["rivian_oem", tenant_id],
                "assetType": "vehicle",
                "attributes": random_vehicle_attrs(rng, vin, model, year),
                "segmentAssignments": [rivian_assignment, customer_assignment],
                "updatedAt": "2026-09-01T00:00:00Z",
            }
            assets.append(asset)

    return scale_segments, assets


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=50_000, help="total vehicles across all fleet customers")
    parser.add_argument("--reload", action="store_true", help="regenerate even if cache files exist")
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if args.reload or not ASSETS_CACHE.exists() or not SEGMENTS_CACHE.exists():
        # scale `count` proportionally against the default 50K/5-tenant split
        scale = args.count / 50_000
        for spec in CUSTOMER_SPEC.values():
            spec["count"] = round(spec["count"] * scale)
        scale_segments, assets = generate(args.count)
        with open(SEGMENTS_CACHE, "w") as f:
            json.dump(scale_segments, f)
        with open(ASSETS_CACHE, "w") as f:
            for a in assets:
                f.write(json.dumps(a) + "\n")
        print(f"Generated {len(assets)} vehicles + {len(scale_segments)} segments, cached to {CACHE_DIR}")
    else:
        with open(SEGMENTS_CACHE) as f:
            scale_segments = json.load(f)
        assets = []
        with open(ASSETS_CACHE) as f:
            for line in f:
                assets.append(json.loads(line))
        print(f"Loaded {len(assets)} vehicles + {len(scale_segments)} segments from cache ({CACHE_DIR})")

    client = MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
    db = client[MONGODB_DB]

    db.tenants.delete_many({})
    db.tenants.insert_many(TENANTS)
    print(f"Inserted {len(TENANTS)} tenants")

    # Only insert the NEW segments (rivian_oem + amazon/dhl/driveshare) here --
    # acme_fleet_corp/globex_logistics come from the curated fixture seed step.
    db.asset_segments.delete_many({"tenantId": {"$in": ["rivian_oem", "amazon_logistics",
                                                          "dhl_express_fleet", "driveshare_rentals"]}})
    db.asset_segments.insert_many(scale_segments)
    print(f"Inserted {len(scale_segments)} scale segments")

    db.assets.delete_many({"_id": {"$regex": "^VIN_SCALE_"}})
    batch = 2000
    for i in range(0, len(assets), batch):
        db.assets.insert_many(assets[i:i + batch])
        print(f"  inserted {min(i + batch, len(assets))}/{len(assets)}")

    print(f"\nTotal assets in {MONGODB_DB}.assets: {db.assets.count_documents({})}")
    print(f"Total segments in {MONGODB_DB}.asset_segments: {db.asset_segments.count_documents({})}")


if __name__ == "__main__":
    main()
