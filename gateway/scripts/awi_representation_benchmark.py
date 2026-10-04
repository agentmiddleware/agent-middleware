#!/usr/bin/env python3
"""
Deterministic local benchmark for AWI progressive representations.

This is a smoke benchmark, not a WebArena substitute. It gives CI a stable
artifact for comparing representation payload size, approximate token cost,
latency, and task-relevant field coverage on a fixed page fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.schemas.awi import AWIRepresentationType
from app.services.awi_representation import ProgressiveRepresentationEngine


EXPECTED_TERMS = (
    "luna trail jacket",
    "add to cart",
    "$128",
    "size",
    "shipping",
)


def build_fixture_page_state() -> dict[str, Any]:
    """Return a stable commerce-like page state for representation benchmarks."""
    return {
        "html": """
        <html>
          <head><title>Luna Trail Jacket</title></head>
          <body>
            <main>
              <h1>Luna Trail Jacket</h1>
              <p>Water resistant shell with two-way stretch.</p>
              <span class="price">$128</span>
              <label>Size</label>
              <select name="size">
                <option>XS</option><option>M</option><option>L</option>
              </select>
              <button id="add-to-cart">Add to cart</button>
              <p>Free shipping over $75.</p>
            </main>
          </body>
        </html>
        """,
        "title": "Luna Trail Jacket",
        "url": "https://shop.example.com/products/luna-trail-jacket",
        "forms_count": 0,
        "links_count": 4,
        "elements": [
            {
                "role": "heading",
                "name": "Luna Trail Jacket",
                "actions": [],
            },
            {
                "role": "button",
                "name": "Add to cart",
                "actions": ["click"],
            },
            {
                "role": "combobox",
                "name": "Size",
                "state": {"expanded": False},
                "actions": ["select"],
            },
        ],
    }


def _serialize(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _approx_tokens(serialized: str) -> int:
    return max(1, (len(serialized) + 3) // 4)


def _field_coverage(serialized: str) -> float:
    normalized = serialized.lower()
    matches = sum(1 for term in EXPECTED_TERMS if term in normalized)
    return round(matches / len(EXPECTED_TERMS), 3)


async def run_benchmark() -> dict[str, Any]:
    """Run the deterministic local AWI representation benchmark."""
    engine = ProgressiveRepresentationEngine()
    page_state = build_fixture_page_state()
    results = []

    for representation_type in AWIRepresentationType:
        representation = await engine.generate_representation(
            "awi-benchmark-local",
            representation_type,
            page_state,
            {"max_length": 500},
        )
        serialized = _serialize(representation["content"])
        results.append(
            {
                "representation_type": representation_type.value,
                "size_bytes": representation["metadata"]["size_bytes"],
                "serialized_bytes": len(serialized.encode("utf-8")),
                "approx_tokens": _approx_tokens(serialized),
                "latency_ms": representation["metadata"]["generation_time_ms"],
                "task_field_coverage": _field_coverage(serialized),
            }
        )

    return {
        "benchmark": "awi-local-representation-v0",
        "fixture": "commerce_product_page",
        "expected_terms": list(EXPECTED_TERMS),
        "results": results,
    }


def _validate(report: dict[str, Any]) -> None:
    results = report["results"]
    counts = Counter(item["representation_type"] for item in results)
    observed = set(counts)
    expected = {item.value for item in AWIRepresentationType}
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if len(results) != len(expected) or missing or unexpected or duplicates:
        raise SystemExit(
            "invalid representation benchmark rows: "
            f"expected_count={len(expected)} actual_count={len(results)} "
            f"missing={missing} unexpected={unexpected} duplicates={duplicates}"
        )
    for item in results:
        if item["size_bytes"] <= 0:
            raise SystemExit(f"non-positive size for {item['representation_type']}")
        if item["approx_tokens"] <= 0:
            raise SystemExit(f"non-positive tokens for {item['representation_type']}")
        coverage = item["task_field_coverage"]
        if coverage < 0 or coverage > 1:
            raise SystemExit(f"invalid coverage for {item['representation_type']}")


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate benchmark shape without enforcing performance thresholds",
    )
    args = parser.parse_args()

    report = await run_benchmark()
    if args.check:
        _validate(report)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
