#!/usr/bin/env python3
"""Create operational + Atlas Search/Vector indexes on amp_poc_db.assets.

Schema v2 note: `tenantIds` and `segmentAssignments` are now BOTH arrays on
the same document. MongoDB does not allow a compound index across two
different array fields ("cannot index parallel arrays", verified live --
see README). So authorization filtering indexes/queries scope by
`segmentAssignments.tenantId` (inside the single array), and `tenantIds`
gets its own separate single-field index for simple tenant-membership
lookups that don't care about segment placement.

Usage:
    python scripts/create_indexes.py
"""
import os
import time
from pathlib import Path

import certifi
from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MONGODB_URI = os.environ["MONGODB_URI"]
MONGODB_DB = os.environ.get("MONGODB_DB", "amp_poc_db")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY")

AUTOEMBED_INDEX_NAME = "vector_auto_embed_index"
FALLBACK_INDEX_NAME = "vector_manual_embed_index"
TEXT_SEARCH_INDEX_NAME = "assets_text_search_index"
VIN_AUTOCOMPLETE_INDEX_NAME = "assets_vin_autocomplete_index"


def list_search_indexes_retry(coll, name=None, retries=5, delay=5):
    # Atlas's search index management control plane occasionally returns a
    # transient "Error connecting to Search Index Management service" error
    # under heavy index create/drop churn. Retry a few times before giving up.
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
    raise TimeoutError(f"Index {name} not queryable after {timeout}s")


def main() -> None:
    client = MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
    db = client[MONGODB_DB]
    coll = db.assets

    # 1. Operational compound index for authorization (REQ-01), scoped to the
    # segmentAssignments array only (see module docstring re: parallel arrays).
    coll.create_index(
        [("segmentAssignments.tenantId", ASCENDING),
         ("segmentAssignments.authorizedRolesOrTeams", ASCENDING),
         ("attributes.make", ASCENDING)],
        name="segment_auth_make_idx",
    )
    print("Created operational index: segment_auth_make_idx")

    # 1a. Separate single-field index on tenantIds for simple tenant-membership
    # lookups (e.g. "does tenant X have any relationship to this asset at
    # all") that don't need segment context. Kept out of the compound index
    # above specifically to avoid the parallel-arrays restriction.
    coll.create_index([("tenantIds", ASCENDING)], name="tenant_ids_idx")
    print("Created index: tenant_ids_idx")

    # 1b. Wildcard index on the polymorphic attributes sub-document (Attribute
    # Pattern). Covers ad hoc equality/range filters on any attribute --
    # present or added by a future asset type -- without hand-maintaining a
    # single-field index per attribute per type.
    coll.create_index([("attributes.$**", ASCENDING)], name="attributes_wildcard_idx")
    print("Created wildcard index: attributes_wildcard_idx")

    existing = {i["name"] for i in list_search_indexes_retry(coll)}

    # 2. Plain Atlas Search index for keyword/full-text (REQ-03 keyword half).
    # `segmentAssignments` is indexed as `embeddedDocuments` so the tenant +
    # role filter can be applied *inside* the same array element via Atlas
    # Search's `embeddedDocument` operator (the search-index equivalent of
    # $elemMatch) -- required for REQ-01 authorization to compose correctly
    # with keyword/vector search, not just plain find().
    if TEXT_SEARCH_INDEX_NAME not in existing:
        text_model = SearchIndexModel(
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
                        "attributes": {
                            "type": "document",
                            "fields": {
                                "make": {"type": "token"},
                                "color": {"type": "token"},
                            },
                        },
                    },
                }
            },
            name=TEXT_SEARCH_INDEX_NAME,
            type="search",
        )
        coll.create_search_index(text_model)
        print(f"Submitted text search index: {TEXT_SEARCH_INDEX_NAME}")
    else:
        print(f"Text search index already exists: {TEXT_SEARCH_INDEX_NAME}")

    # 2b. VIN autocomplete index -- the fleet-portal reference UI does
    # substring VIN search ("6493" matches VINs with that string anywhere,
    # not just a prefix) at 50K+ scale. A plain regex scan can't use a
    # B-tree index for an unanchored substring match; Atlas Search's
    # `autocomplete` field type (nGram tokenization) is the right tool.
    if VIN_AUTOCOMPLETE_INDEX_NAME not in existing:
        vin_model = SearchIndexModel(
            definition={
                "mappings": {
                    "dynamic": False,
                    "fields": {
                        "attributes": {
                            "type": "document",
                            "fields": {
                                "vin": {
                                    "type": "autocomplete",
                                    "tokenization": "nGram",
                                    "minGrams": 3,
                                    "maxGrams": 7,
                                    "foldDiacritics": False,
                                },
                            },
                        },
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
            name=VIN_AUTOCOMPLETE_INDEX_NAME,
            type="search",
        )
        coll.create_search_index(vin_model)
        print(f"Submitted VIN autocomplete index: {VIN_AUTOCOMPLETE_INDEX_NAME}")
    else:
        print(f"VIN autocomplete index already exists: {VIN_AUTOCOMPLETE_INDEX_NAME}")

    # 3. Attempt spec's literal autoEmbed vector index (REQ-04).
    # `filter`-type fields let $vectorSearch pre-filter by tenant/role so
    # vector search composes with REQ-01 authorization (without them, vector
    # search alone returns other tenants'/roles' documents too).
    autoembed_definition = {
        "fields": [
            {
                "type": "autoEmbed",
                "path": "unstructuredNotes",
                "model": "voyage-4",
                "modality": "text",
                "quantization": "float",
                "similarity": "cosine",
            },
            {"type": "filter", "path": "segmentAssignments.tenantId"},
            {"type": "filter", "path": "segmentAssignments.authorizedRolesOrTeams"},
        ]
    }
    used_autoembed = False
    if AUTOEMBED_INDEX_NAME not in existing:
        try:
            model = SearchIndexModel(
                definition=autoembed_definition,
                name=AUTOEMBED_INDEX_NAME,
                type="vectorSearch",
            )
            coll.create_search_index(model)
            print(f"Submitted autoEmbed vector index: {AUTOEMBED_INDEX_NAME}")
            wait_for_index(coll, AUTOEMBED_INDEX_NAME)
            used_autoembed = True
            print("autoEmbed vector index is QUERYABLE — native server-side embedding confirmed.")
        except Exception as e:
            print(f"autoEmbed index creation/build FAILED: {type(e).__name__}: {e}")
            print("Falling back to client-side embedding + standard vectorSearch index.")
    else:
        existing_idx = next(i for i in list_search_indexes_retry(coll) if i["name"] == AUTOEMBED_INDEX_NAME)
        used_autoembed = existing_idx.get("queryable", False)
        print(f"autoEmbed index already exists, queryable={used_autoembed}")

    # Always also build the client-side-embedding fallback index, regardless of
    # whether autoEmbed succeeded, so the notebook can show both approaches
    # side by side. Scoped to docs that actually HAVE unstructuredNotes --
    # only the small curated fixture does; the 50K generated vehicles
    # deliberately don't (see scripts/generate_fleet_data.py docstring), to
    # avoid embedding cost/time at scale for no additional demo value.
    import voyageai

    vo = voyageai.Client(api_key=VOYAGE_API_KEY)
    docs = list(coll.find({"unstructuredNotes": {"$exists": True}}, {"_id": 1, "unstructuredNotes": 1}))
    texts = [d["unstructuredNotes"] for d in docs]
    print(f"Embedding {len(texts)} docs client-side via Voyage AI (voyage-3.5)...")
    result = vo.embed(texts, model="voyage-3.5", input_type="document")
    for doc, emb in zip(docs, result.embeddings):
        coll.update_one({"_id": doc["_id"]}, {"$set": {"unstructuredNotesEmbedding": emb}})
    print(f"Backfilled unstructuredNotesEmbedding on {len(docs)} docs (curated fixture only).")

    fallback_existing = {i["name"] for i in list_search_indexes_retry(coll)}
    if FALLBACK_INDEX_NAME not in fallback_existing:
        fallback_model = SearchIndexModel(
            definition={
                "fields": [
                    {
                        "type": "vector",
                        "path": "unstructuredNotesEmbedding",
                        "numDimensions": len(result.embeddings[0]),
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "segmentAssignments.tenantId"},
                    {"type": "filter", "path": "segmentAssignments.authorizedRolesOrTeams"},
                ]
            },
            name=FALLBACK_INDEX_NAME,
            type="vectorSearch",
        )
        coll.create_search_index(fallback_model)
        print(f"Submitted fallback vector index: {FALLBACK_INDEX_NAME}")
        wait_for_index(coll, FALLBACK_INDEX_NAME)
        print("Fallback vector index is QUERYABLE.")
    else:
        print(f"Fallback vector index already exists: {FALLBACK_INDEX_NAME}")

    # wait for the other search indexes too
    wait_for_index(coll, TEXT_SEARCH_INDEX_NAME)
    print(f"Text search index is QUERYABLE: {TEXT_SEARCH_INDEX_NAME}")
    wait_for_index(coll, VIN_AUTOCOMPLETE_INDEX_NAME)
    print(f"VIN autocomplete index is QUERYABLE: {VIN_AUTOCOMPLETE_INDEX_NAME}")

    print("\n--- SUMMARY ---")
    print(f"used_autoembed = {used_autoembed}")
    print("Vector index to use in queries:", AUTOEMBED_INDEX_NAME if used_autoembed else FALLBACK_INDEX_NAME)


if __name__ == "__main__":
    main()
