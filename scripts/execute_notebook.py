#!/usr/bin/env python3
"""Execute notebooks/amp_mongodb_poc.ipynb end-to-end and save outputs in place.

Uses local .env for credentials (MONGODB_URI, VOYAGE_API_KEY) since this
runs outside Colab -- the notebook itself falls back to os.environ when
google.colab isn't importable.
"""
import os
from pathlib import Path

import nbformat
from dotenv import load_dotenv
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

NB_PATH = ROOT / "notebooks" / "amp_mongodb_poc.ipynb"

nb = nbformat.read(NB_PATH, as_version=4)
client = NotebookClient(
    nb,
    timeout=600,
    kernel_name="python3",
    resources={"metadata": {"path": str(ROOT)}},
)
client.execute()
nbformat.write(nb, NB_PATH)
print(f"Executed and saved: {NB_PATH}")
