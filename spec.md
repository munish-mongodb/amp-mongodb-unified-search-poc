# SPEC-001: Technical Specification & Demonstration Plan
## Unified Multi-Tenant Operational, Vector, & Hybrid Search Engine on MongoDB

| Metadata | Details |
| :--- | :--- |
| **Document ID** | SPEC-001-AMP-MONGO |
| **Status** | PROPOSED / POC READY |
| **Target Audience** | Enterprise Architecture, Data Engineering, & Product Security Teams |
| **Repository URL** | `https://github.com/org/amp-mongodb-unified-search-poc` |
| **Primary Goal** | Prove MongoDB handles OEM asset management, hierarchical authorization, and hybrid vector search in a single data layer. |

---

## 1. Executive Summary & Problem Context

The customer operates an Enterprise Asset Management Platform (AMP) for physical OEM assets (vehicles, EV chargers, e-bikes). The current production architecture suffers from a **split-brain data model**:

* **MongoDB** stores polymorphic physical asset records (VIN, firmware, diagnostic properties).
* **PostgreSQL** stores operational metadata, hierarchical asset segments (regions, teams), and access control lists (ACLs).

### The Primary Engineering Challenge
Searching for an asset requires enforcing user segment permissions. Because access rules live in PostgreSQL and asset data lives in MongoDB, every operational or global search request triggers a high-latency **cross-database fan-out**:

```
[User Search Query] 
       │
       ▼
1. Query PostgreSQL ────► Resolves authorized Segment IDs & Asset IDs (~10,000 IDs)
       │
       ▼
2. App Layer Assembly ──► Constructs massive $in list query
       │
       ▼
3. Query MongoDB ───────► Executes db.assets.find({ _id: { $in: [...] }, color: "blue" })
       │
       ▼
4. Results Merging ─────► High latency, query serialization overhead, complex MCP layer
```

### The Solution: Unified MongoDB Data & Search Engine
By migrating asset segment hierarchies and entitlement rules into MongoDB alongside polymorphic asset documents, we eliminate cross-database fan-outs. MongoDB evaluates **Metadata Filters + Hierarchical Entitlements + Full-Text + Vector Search in a single query pass**.

---

## 2. Technical Requirements & Objectives

| Objective ID | Feature Area | Description | Success Metric |
| :--- | :--- | :--- | :--- |
| **REQ-01** | **Single-Pass Authorization** | Evaluate user segment authorization and metadata filters within MongoDB. | 0 cross-database queries; < 15ms p95 latency. |
| **REQ-02** | **Flexible Schema** | Support polymorphic fields across different asset classes (Vehicles, Chargers, E-Bikes). | No `ALTER TABLE` schema migrations required for new properties. |
| **REQ-03** | **Atlas Search / Vector** | Combine keyword text search with semantic vector embeddings. | Relevant natural-language matches on unstructured issue logs. |
| **REQ-04** | **Auto Embedding** | Leverage native MongoDB Atlas `autoEmbed` via Voyage AI models. | Server-side vector generation; 0 client-side embedding boilerplate. |
| **REQ-05** | **In-Engine Reranking** | Use Voyage AI / Cohere reranking integrated directly within the MongoDB pipeline. | Top-5 retrieval precision improvement by 20%+. |

---

## 3. MongoDB Data Architecture & Schema Design

### 3.1 Collection `asset_segments` (Hierarchy & Metadata)
Maintains the organizational structure using the **Materialized Path** and **Ancestors Array Pattern**.

```json
{
  "_id": "seg_california_north",
  "tenantId": "acme_fleet_corp",
  "name": "Northern California Fleet Operations",
  "segmentType": "region",
  "owner": "usr_exec_99",
  "status": "active",
  "hierarchy": {
    "parentId": "seg_california",
    "ancestors": ["seg_global", "seg_us_west", "seg_california"],
    "path": ",seg_global,seg_us_west,seg_california,seg_california_north,"
  },
  "createdAt": "2026-01-15T08:00:00Z"
}
```

### 3.2 Collection `assets` (Polymorphic Assets + Denormalized Security Context)
Embeds segment assignment and pre-computed ancestor arrays to enable fast single-query authorization.

```json
{
  "_id": "asset_vin_1FTFW1E81MF123456",
  "tenantId": "acme_fleet_corp",
  "assetType": "vehicle",
  "attributes": {
    "make": "Rivian",
    "model": "R1T",
    "color": "Rivian Blue",
    "vin": "1FTFW1E81MF123456",
    "firmwareVersion": "v2026.12.4",
    "batteryCapacityKw": 135
  },
  "unstructuredNotes": "Vehicle reported intermittent DC fast charging throttling under high ambient temperatures in Hayward depot.",
  "segmentAssignments": [
    {
      "segmentId": "seg_hayward_team",
      "ancestorSegments": ["seg_global", "seg_us_west", "seg_california", "seg_california_north", "seg_hayward_team"]
    }
  ],
  "authorizedRolesOrTeams": ["team_hayward", "region_california_north", "role_fleet_admin"],
  "updatedAt": "2026-08-31T14:30:00Z"
}
```

---

## 4. Indexing & Unified Search Pipeline Specifications

### 4.1 Compound Operational Indexes
```javascript
// Index for fast tenant + ACL multi-key matching
db.assets.createIndex({ "tenantId": 1, "authorizedRolesOrTeams": 1, "attributes.make": 1 });
```

### 4.2 Atlas Search & Automated Vector Embedding Index
Using MongoDB Atlas `autoEmbed` with integrated Voyage AI models (`voyage-3` / `voyage-multimodal-3`):

```json
{
  "mappings": {
    "dynamic": true,
    "fields": {
      "tenantId": { "type": "token" },
      "authorizedRolesOrTeams": { "type": "token" },
      "unstructuredNotes": {
        "type": "autoEmbed",
        "model": "voyage-3",
        "dimensions": 1024,
        "similarity": "cosine"
      }
    }
  }
}
```

### 4.3 Unified Hybrid Search + Reranking Query Pipeline
Below is the MongoDB aggregation pipeline demonstrating **Hybrid Search (Keyword + Automated Vector) with Security Filtering and In-Engine Reranking**:

```javascript
db.assets.aggregate([
  // Step 1: Hybrid Vector + Keyword Search with Pre-Filtering
  {
    "$vectorSearch": {
      "index": "vector_auto_embed_index",
      "path": "unstructuredNotes",
      "queryText": "battery thermal throttling during fast charging",
      "numCandidates": 100,
      "limit": 20,
      "filter": {
        "$and": [
          { "tenantId": { "$eq": "acme_fleet_corp" } },
          { "authorizedRolesOrTeams": { "$in": ["region_california_north"] } }
        ]
      }
    }
  },
  // Step 2: Combine with Metadata Attribute Filters
  {
    "$match": {
      "attributes.make": "Rivian",
      "attributes.color": "Rivian Blue"
    }
  },
  // Step 3: In-Engine Native Reranking (Voyage AI Reranker)
  {
    "$rerank": {
      "model": "voyage-rerank-2",
      "queryText": "battery thermal throttling during fast charging",
      "field": "unstructuredNotes",
      "topK": 5
    }
  },
  // Step 4: Projection
  {
    "$project": {
      "attributes.vin": 1,
      "attributes.make": 1,
      "attributes.model": 1,
      "attributes.color": 1,
      "unstructuredNotes": 1,
      "score": { "$meta": "searchScore" },
      "rerankScore": { "$meta": "rerankScore" }
    }
  }
]);
```

---

## 5. Google Colab Public Demo Guide (`amp_mongodb_poc.ipynb`)

The public repository contains a fully executable Python notebook designed to run in Google Colab.

### 5.1 Environment Prerequisites
Install required libraries inside Google Colab:

```python
!pip install pymongo[srv] pandas sentence-transformers tabulate voyageai
```

### 5.2 Executable Colab Script Outline

```python
import os
import pymongo
from pymongo import MongoClient
import pandas as pd
from datetime import datetime

# 1. Connect to MongoDB Atlas
# Use standard connection string or Mock In-Memory Server for local testing
MONGODB_URI = "mongodb+srv://<username>:<password>@cluster0.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(MONGODB_URI)
db = client["amp_poc_db"]

# 2. Seed Polymorphic OEM Assets
assets_data = [
    {
        "_id": "VIN_RIVIAN_001",
        "tenantId": "acme_fleet_corp",
        "assetType": "vehicle",
        "attributes": {"make": "Rivian", "model": "R1T", "color": "Rivian Blue", "vin": "1FTFW1E81MF123456"},
        "unstructuredNotes": "DC fast charger cut out at 80% state of charge in San Jose station. High heat warning.",
        "authorizedRolesOrTeams": ["team_san_jose", "region_california_north"]
    },
    {
        "_id": "VIN_RIVIAN_002",
        "tenantId": "acme_fleet_corp",
        "assetType": "vehicle",
        "attributes": {"make": "Rivian", "model": "R1S", "color": "Forest Green", "vin": "1FTFW1E81MF987654"},
        "unstructuredNotes": "Air suspension fault detected during highway cruising near Hayward.",
        "authorizedRolesOrTeams": ["team_hayward", "region_california_north"]
    },
    {
        "_id": "CHARGER_EV_101",
        "tenantId": "acme_fleet_corp",
        "assetType": "ev_charger",
        "attributes": {"maxKw": 350, "connectorType": "CCS1", "firmware": "v4.2.1"},
        "unstructuredNotes": "Cable cooling fan failure reported by telemetry.",
        "authorizedRolesOrTeams": ["team_san_jose", "region_california_north"]
    }
]

db.assets.drop()
db.assets.insert_many(assets_data)
print(" Successfully seeded MongoDB assets with denormalized security entitlements!")

# 3. Benchmark Query: Simulated Fan-out vs. MongoDB Single Pass
def execute_single_pass_search(user_roles, make_filter, text_query):
    query = {
        "tenantId": "acme_fleet_corp",
        "authorizedRolesOrTeams": {"$in": user_roles},
        "attributes.make": make_filter
    }
    results = list(db.assets.find(query))
    return pd.DataFrame([r['attributes'] for r in results])

# Execute Search for Regional Manager with 'region_california_north' access
df_results = execute_single_pass_search(["region_california_north"], "Rivian", "battery heat")
print("
--- Search Results (Evaluated in Single MongoDB Query Pass) ---")
print(df_results.to_string(index=False))
```

---

## 6. Value Proposition Summary for Customer Stakeholders

```
+-----------------------------------------------------------------------------------+
|                        BENEFITS AT A GLANCE                                       |
+-----------------------------------------------------------------------------------+
| 1. ARCHITECTURAL SIMPLICITY  | Eliminates complex MCP fan-out logic between DBs.  |
| 2. SUB-SECOND LATENCY       | Pre-filtered vector & keyword search in 1 pass.   |
| 3. ZERO EXTRA AGENTS        | Auto-embedding manages Voyage AI vectors server-side.|
| 4. ENHANCED ACCURACY        | Native Voyage AI reranking boosts top-k relevance.|
+-----------------------------------------------------------------------------------+
```

---
*Created and validated for public GitHub distribution and executive PoC demonstration.*
