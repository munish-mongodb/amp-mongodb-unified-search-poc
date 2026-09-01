#!/usr/bin/env python3
"""Create operational + Atlas Search/Vector indexes on amp_poc_db.assets.

Tries the spec's literal `autoEmbed` field-type search index definition first.
If Atlas rejects it (tier/feature not enabled), falls back to a standard
`vectorSearch` index over a client-side-computed embedding field
(`unstructuredNotesEmbedding`), which this script also backfills via Voyage AI
in that case.

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


def wait_for_index(coll, name, timeout=240):
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

    # 1. Operational compound index (REQ-01)
    coll.create_index(
        [("tenantId", ASCENDING), ("authorizedRolesOrTeams", ASCENDING), ("attributes.make", ASCENDING)],
        name="tenant_acl_make_idx",
    )
    print("Created operational index: tenant_acl_make_idx")

    # 2. Plain Atlas Search index for keyword/full-text (REQ-03 keyword half)
    existing = {i["name"] for i in list_search_indexes_retry(coll)}
    if TEXT_SEARCH_INDEX_NAME not in existing:
        text_model = SearchIndexModel(
            definition={
                "mappings": {
                    "dynamic": False,
                    "fields": {
                        "tenantId": {"type": "token"},
                        "authorizedRolesOrTeams": {"type": "token"},
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

    # 3. Attempt spec's literal autoEmbed vector index (REQ-04)
    # Note: spec section 4.2 wrote `"type": "autoEmbed"` nested under `mappings.fields`
    # (Atlas Search dynamic-mapping shape). The actual Atlas vectorSearch index API
    # error revealed the real shape: a top-level `fields` array entry with
    # `type: "autoEmbed"` and `modality: "text"`, plus a supported Voyage model
    # (autoEmbed only supports the voyage-4 family: voyage-4/voyage-4-large/
    # voyage-4-lite/voyage-code-3 -- voyage-3.x is deprecated for this feature).
    # `filter` type fields are required here (same as the manual fallback index)
    # so tenantId/authorizedRolesOrTeams can be used in $vectorSearch's `filter`
    # for single-pass authorization (REQ-01) -- without them, vector search alone
    # leaks cross-tenant/unauthorized docs into results.
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
            {"type": "filter", "path": "tenantId"},
            {"type": "filter", "path": "authorizedRolesOrTeams"},
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
    # side by side (spec's aspiration vs. the traditional, always-works pattern).
    import voyageai

    vo = voyageai.Client(api_key=VOYAGE_API_KEY)
    docs = list(coll.find({}, {"_id": 1, "unstructuredNotes": 1}))
    texts = [d["unstructuredNotes"] for d in docs]
    print(f"Embedding {len(texts)} docs client-side via Voyage AI (voyage-3.5)...")
    result = vo.embed(texts, model="voyage-3.5", input_type="document")
    for doc, emb in zip(docs, result.embeddings):
        coll.update_one({"_id": doc["_id"]}, {"$set": {"unstructuredNotesEmbedding": emb}})
    print("Backfilled unstructuredNotesEmbedding on all docs.")

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
                    {"type": "filter", "path": "tenantId"},
                    {"type": "filter", "path": "authorizedRolesOrTeams"},
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

    # wait for text search index too
    wait_for_index(coll, TEXT_SEARCH_INDEX_NAME)
    print(f"Text search index is QUERYABLE: {TEXT_SEARCH_INDEX_NAME}")

    print("\n--- SUMMARY ---")
    print(f"used_autoembed = {used_autoembed}")
    print("Vector index to use in queries:", AUTOEMBED_INDEX_NAME if used_autoembed else FALLBACK_INDEX_NAME)


if __name__ == "__main__":
    main()
