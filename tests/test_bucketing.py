"""Bucketing. The Python fallback must match what MongoDB produces exactly,
or a chart silently changes shape depending on which path ran.

The aggregation path needs a real MongoDB; set MONGO_TEST_URI to run those.
Everything else runs anywhere.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bucketing import (DATE_TRUNC_ORIGIN, _via_python, bucket_minutes_for,
                           bucketed)

REAL = os.getenv("MONGO_TEST_URI")


class FakeCollection:
    """find() only. bucketed() falls through to Python when aggregate raises."""
    def __init__(self, docs):
        self.docs = docs

    def find(self, query=None, projection=None):
        return self

    def sort(self, *a, **k):
        return list(self.docs)

    def aggregate(self, *a, **k):
        from pymongo.errors import OperationFailure
        raise OperationFailure("no $dateTrunc here")


def hourly_docs(hours=24 * 40, start=datetime(2026, 3, 1)):
    return [{"ts": start + timedelta(hours=h), "air_temp_c": float(h % 24),
             "humidity_pct": 50.0, "dew_point_c": 10.0,
             "leaf_temp_c": 12.0, "leaf_wetness": 0.0}
            for h in range(hours)]


# --- bucket sizing ---

def test_bucket_size_grows_with_the_window():
    assert bucket_minutes_for(24) == 60
    assert bucket_minutes_for(24 * 7) == 360
    assert bucket_minutes_for(24 * 60) == 1440
    assert bucket_minutes_for(24 * 200) == 4320


def test_point_count_stays_drawable():
    """No window should ever ask a browser to plot thousands of points."""
    for hours in (24, 24 * 7, 24 * 30, 24 * 150, 24 * 400):
        points = hours * 60 / bucket_minutes_for(hours)
        assert points <= 320, f"{hours}h gives {points:.0f} points"


# --- the fallback ---

def test_fallback_buckets_hourly():
    rows = _via_python(FakeCollection(hourly_docs(48)), {}, 60)
    assert len(rows) == 48
    assert rows[0]["n"] == 1


def test_fallback_averages_within_a_bucket():
    rows = _via_python(FakeCollection(hourly_docs(24)), {}, 360)
    assert len(rows) == 4
    assert rows[0]["n"] == 6
    assert rows[0]["air"] == pytest.approx(2.5)


def test_fallback_is_anchored_to_2000_not_the_epoch():
    """MongoDB's $dateTrunc bins from 2000-01-01. Anchoring the fallback to the
    Unix epoch put 3-day buckets exactly one day out."""
    rows = _via_python(FakeCollection(hourly_docs(24 * 30)), {}, 4320)
    for r in rows:
        offset = (r["bucket"] - DATE_TRUNC_ORIGIN).total_seconds()
        assert offset % (4320 * 60) == 0


def test_missing_fields_become_none_not_zero():
    docs = [{"ts": datetime(2026, 3, 1, h)} for h in range(3)]
    rows = _via_python(FakeCollection(docs), {}, 60)
    assert all(r["air"] is None for r in rows)


def test_empty_input():
    assert _via_python(FakeCollection([]), {}, 60) == []


def test_bucketed_falls_back_when_aggregation_is_unsupported():
    rows = bucketed(FakeCollection(hourly_docs(24)), {}, 60)
    assert len(rows) == 24


# --- against a real MongoDB ---

@pytest.mark.skipif(not REAL, reason="set MONGO_TEST_URI for the aggregation path")
@pytest.mark.parametrize("minutes", [15, 60, 180, 360, 720, 1440, 4320, 10080])
def test_both_paths_produce_identical_output(minutes):
    from pymongo import MongoClient
    from src.bucketing import _via_aggregation
    db = MongoClient(REAL).get_default_database()
    db.bucket_probe.drop()
    db.bucket_probe.insert_many(hourly_docs(24 * 40))

    agg = _via_aggregation(db.bucket_probe, {}, minutes)
    py = _via_python(db.bucket_probe, {}, minutes)

    assert [r["bucket"] for r in agg] == [r["bucket"] for r in py]
    for a, b in zip(agg, py):
        assert a["n"] == b["n"]
        assert a["air"] == pytest.approx(b["air"], abs=0.011)
