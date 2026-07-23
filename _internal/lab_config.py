"""Shared configuration for the Firestore Enterprise anti-patterns lab.

Each Firestore Enterprise database is its own MongoDB-compatible endpoint
(distinct host, keyed by database UID + region, with its own users/creds) —
not just a database name on a shared cluster. So the lab doesn't reuse the
101 demo's connection helpers (../../demo_config.py, hardcoded to `tailoredb`);
instead the full connection string lives in the lab directory's `.env` file
(MONGO_URI), letting the lab point at any Firestore Enterprise database.
Lab data lives in lab_-prefixed collections so it never collides with the
101 demo's customers/products/orders/sensor_readings collections.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

# This file lives in _internal/, one level below the lab directory .env
# actually belongs in -- parent.parent, not parent, or this silently looks
# for .env inside _internal/ and fails.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MONGO_URI = os.environ["MONGO_URI"]

LAB_COLLECTIONS = {
    "counters": "lab_counters",
    "devices": "lab_devices",
    "events": "lab_events",
    "bookings": "lab_bookings",
}


def get_lab_db():
    """Return the database handle named in MONGO_URI's connection string."""
    client = MongoClient(MONGO_URI)
    return client.get_default_database()
