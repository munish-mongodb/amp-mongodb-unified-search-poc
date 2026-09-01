#!/usr/bin/env python3
"""Seed amp_poc_db with asset_segments and assets collections.

Usage:
    python scripts/seed.py

Reads MONGODB_URI / MONGODB_DB from .env (or environment). Idempotent:
drops and re-inserts both collections each run.
"""
import json
import os
from pathlib import Path

import certifi
from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MONGODB_URI = os.environ["MONGODB_URI"]
MONGODB_DB = os.environ.get("MONGODB_DB", "amp_poc_db")


def load_json(name: str):
    with open(ROOT / "data" / name) as f:
        return json.load(f)


def main() -> None:
    client = MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
    db = client[MONGODB_DB]

    segments = load_json("segments_seed.json")
    assets = load_json("assets_seed.json")

    db.asset_segments.drop()
    db.assets.drop()

    db.asset_segments.insert_many(segments)
    db.assets.insert_many(assets)

    print(f"Seeded {MONGODB_DB}.asset_segments: {db.asset_segments.count_documents({})} docs")
    print(f"Seeded {MONGODB_DB}.assets: {db.assets.count_documents({})} docs")

    tenants = db.assets.distinct("tenantId")
    types = db.assets.distinct("assetType")
    print(f"Tenants: {tenants}")
    print(f"Asset types: {types}")


if __name__ == "__main__":
    main()
