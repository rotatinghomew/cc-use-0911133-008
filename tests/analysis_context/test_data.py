from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DataContextTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures/access_scenarios.json").read_text(encoding="utf-8"))

    def test_graph_references_and_household_units(self):
        nodes = {row["village"] for row in self.data["households"]} | {row["id"] for row in self.data["shelters"]}
        self.assertTrue(all(row["from"] in nodes and row["to"] in nodes for row in self.data["edges"]))
        self.assertTrue(all(row["size"] > 0 for row in self.data["households"]))

    def test_facility_and_bed_capacity_are_distinct(self):
        self.assertTrue(any(row["beds"] > row["facility_slots"]["accessible"] for row in self.data["shelters"]))
        self.assertTrue(self.data["closures"])
        self.assertGreater(len(self.data["larger_households"]), len(self.data["households"]))

