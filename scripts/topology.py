"""Shared tenant/segment topology + authorization-closure logic.

Used by:
- data/segments_seed.json / data/assets_seed.json (curated 19-doc fixture,
  migrated to this schema by hand -- this module defines the *new* tenants
  and the Rivian OEM hierarchy those fixture docs reference)
- scripts/generate_fleet_data.py (50K-vehicle synthetic scale dataset)
- scripts/seed.py, scripts/create_indexes.py
- notebooks/amp_mongodb_poc.ipynb (inlined into a code cell so the notebook
  is self-contained -- see build_notebook.py)

Schema v2 (vs. the original POC):
- assets.tenantId (scalar)   -> assets.tenantIds (array). A vehicle can be
  owned by multiple tenants at once (Rivian as OEM + the fleet customer that
  bought it).
- assets.segmentAssignments[] is now TENANT-SCOPED: each entry is
  {tenantId, segmentId, ancestorSegments, authorizedRolesOrTeams}. Segments
  themselves stay 1-tenant-owned (a segment can't be shared across tenants),
  so a multi-tenant asset needs one assignment per tenant context -- Rivian
  places a vehicle in its own internal fleet-health hierarchy, independent
  of where the owning fleet customer places it in *their* org tree.
- authorizedRolesOrTeams is no longer hand-set per asset. It's COMPUTED: the
  union of `grantedRoles` (a new field on asset_segments) across a segment
  assignment's segmentId + all its ancestorSegments. This mirrors the real
  rule ("access to a parent node recursively grants visibility to all child
  nodes and their connected vehicles") instead of just asserting it, and
  it's the mechanism that collapses the ACL-service round trip described in
  the production architecture into a single indexed query.
"""
from __future__ import annotations

TENANTS = [
    {"_id": "rivian_oem", "name": "Rivian Automotive (OEM)", "type": "oem"},
    {"_id": "acme_fleet_corp", "name": "Acme Fleet Corp", "type": "fleet_customer"},
    {"_id": "globex_logistics", "name": "Globex Logistics", "type": "fleet_customer"},
    {"_id": "amazon_logistics", "name": "Amazon Logistics", "type": "fleet_customer"},
    {"_id": "dhl_express_fleet", "name": "DHL Express Fleet", "type": "fleet_customer"},
    {"_id": "driveshare_rentals", "name": "DriveShare Rentals", "type": "fleet_customer"},
]

# Maps a vehicle's attributes.model to the Rivian OEM vehicle-line segment it
# belongs to in Rivian's *own* internal hierarchy (independent of whichever
# fleet customer owns/operates it). RPV = Rivian Package Van (commercial
# delivery line -- what Amazon/DHL run), matching Rivian's real product
# naming (trim variants like EDV-700/EDV-500 sit under the RPV model line).
MODEL_TO_RIVIAN_LINE_SEGMENT = {
    "R1T": "seg_rivian_line_r1t",
    "R1S": "seg_rivian_line_r1s",
    "RPV": "seg_rivian_line_rpv",
}


def materialize_segment_tree(nodes: list[dict]) -> list[dict]:
    """Turn a flat list of {_id, tenantId, name, segmentType, parentId,
    grantedRoles, owner, status, createdAt, [segmentType=="rule_based" extras
    like `rule`]} into full segment documents, computing `ancestors` and
    `path` from `parentId` automatically instead of hand-typing them (less
    error-prone at scale, and the book's own Tree Pattern guidance is to
    keep ancestors/path derived from a single parent pointer, not
    hand-maintained in three places).
    """
    by_id = {n["_id"]: n for n in nodes}
    out = []
    for n in nodes:
        ancestors: list[str] = []
        cur = n.get("parentId")
        while cur:
            ancestors.insert(0, cur)
            cur = by_id[cur].get("parentId")
        path = "," + ",".join(ancestors + [n["_id"]]) + ","
        doc = {k: v for k, v in n.items() if k != "parentId"}
        doc.setdefault("grantedRoles", [])
        doc["hierarchy"] = {"parentId": n.get("parentId"), "ancestors": ancestors, "path": path}
        out.append(doc)
    return out


def rivian_oem_segments() -> list[dict]:
    """Rivian's own internal hierarchy, organized by vehicle line -- distinct
    from any fleet customer's operational (region/depot/team) hierarchy.
    Every vehicle Rivian manufactures gets a segmentAssignment here, in
    addition to whatever the owning fleet customer assigns it to."""
    nodes = [
        {"_id": "seg_rivian_global", "tenantId": "rivian_oem", "name": "Rivian Global Fleet",
         "segmentType": "global", "owner": "usr_rivian_exec_1", "status": "active",
         "parentId": None, "grantedRoles": ["role_fleet_admin"],
         "createdAt": "2024-06-01T08:00:00Z"},
        {"_id": "seg_rivian_line_r1t", "tenantId": "rivian_oem", "name": "R1T Vehicle Line",
         "segmentType": "vehicle_line", "owner": "usr_rivian_eng_1", "status": "active",
         "parentId": "seg_rivian_global", "grantedRoles": ["role_rivian_r1t_eng"],
         "createdAt": "2024-06-02T08:00:00Z"},
        {"_id": "seg_rivian_line_r1s", "tenantId": "rivian_oem", "name": "R1S Vehicle Line",
         "segmentType": "vehicle_line", "owner": "usr_rivian_eng_2", "status": "active",
         "parentId": "seg_rivian_global", "grantedRoles": ["role_rivian_r1s_eng"],
         "createdAt": "2024-06-02T08:05:00Z"},
        {"_id": "seg_rivian_line_rpv", "tenantId": "rivian_oem", "name": "RPV Commercial Van Line",
         "segmentType": "vehicle_line", "owner": "usr_rivian_eng_3", "status": "active",
         "parentId": "seg_rivian_global", "grantedRoles": ["role_rivian_rpv_eng"],
         "createdAt": "2024-06-02T08:10:00Z"},
    ]
    return materialize_segment_tree(nodes)


def fleet_customer_segments(tenant_id: str, label: str, n_regions=3, depots_per_region=3,
                             teams_per_depot=2) -> tuple[list[dict], list[str]]:
    """Procedurally build a global -> region -> depot -> team hierarchy for a
    fleet-customer tenant. Returns (segment_nodes, leaf_team_ids) so the
    caller can distribute vehicles across the leaves.
    """
    prefix = tenant_id
    nodes = [
        {"_id": f"seg_{prefix}_global", "tenantId": tenant_id, "name": f"{label} Global",
         "segmentType": "global", "owner": f"usr_{prefix}_exec_0", "status": "active",
         "parentId": None, "grantedRoles": ["role_fleet_admin"],
         "createdAt": "2025-01-01T08:00:00Z"},
    ]
    leaf_ids: list[str] = []
    for r in range(n_regions):
        region_id = f"seg_{prefix}_region{r}"
        nodes.append({
            "_id": region_id, "tenantId": tenant_id, "name": f"{label} Region {r + 1}",
            "segmentType": "region", "owner": f"usr_{prefix}_exec_{r + 1}", "status": "active",
            "parentId": f"seg_{prefix}_global", "grantedRoles": [f"region_{prefix}_{r}"],
            "createdAt": "2025-01-02T08:00:00Z",
        })
        for d in range(depots_per_region):
            depot_id = f"seg_{prefix}_region{r}_depot{d}"
            nodes.append({
                "_id": depot_id, "tenantId": tenant_id, "name": f"{label} Depot {r + 1}-{d + 1}",
                "segmentType": "depot", "owner": f"usr_{prefix}_mgr_{r}_{d}", "status": "active",
                "parentId": region_id, "grantedRoles": [f"depot_{prefix}_{r}_{d}"],
                "createdAt": "2025-01-03T08:00:00Z",
            })
            for t in range(teams_per_depot):
                team_id = f"seg_{prefix}_region{r}_depot{d}_team{t}"
                nodes.append({
                    "_id": team_id, "tenantId": tenant_id, "name": f"{label} Team {r + 1}-{d + 1}-{t + 1}",
                    "segmentType": "team", "owner": f"usr_{prefix}_lead_{r}_{d}_{t}", "status": "active",
                    "parentId": depot_id, "grantedRoles": [f"team_{prefix}_{r}_{d}_{t}"],
                    "createdAt": "2025-01-04T08:00:00Z",
                })
                leaf_ids.append(team_id)
    return materialize_segment_tree(nodes), leaf_ids


def compute_assignment(tenant_id: str, segment_id: str, segments_by_id: dict) -> dict:
    """Compute a tenant-scoped segmentAssignment entry: ancestorSegments plus
    authorizedRolesOrTeams derived as the union of `grantedRoles` across the
    segment itself and every ancestor -- the actual mechanism behind
    "access to a parent node recursively grants visibility to all child
    nodes and their connected vehicles."
    """
    seg = segments_by_id[segment_id]
    chain = seg["hierarchy"]["ancestors"] + [segment_id]
    granted: set[str] = set()
    for sid in chain:
        granted.update(segments_by_id[sid].get("grantedRoles", []))
    return {
        "tenantId": tenant_id,
        "segmentId": segment_id,
        "ancestorSegments": chain,
        "authorizedRolesOrTeams": sorted(granted),
    }
