import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.research_video_cpu import search


class CpuSearchTests(unittest.TestCase):
    def run_search(self, values):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/"trials.jsonl"
            args = SimpleNamespace(video=Path("clip.mp4"), output=destination,
                                   poll_fps=30, compact=True, seconds=6, warmup=2,
                                   start=0, modules=None, patience=50)
            def measurement(*_args, **_kwargs):
                cpu, valid = next(values)
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "cpu_pct": cpu, "valid": valid, "fps": 30 if valid else 1,
                }))
            with mock.patch("scripts.research_video_cpu.subprocess.run", side_effect=measurement), \
                 contextlib.redirect_stdout(io.StringIO()):
                search(args)
            rows = [json.loads(line) for line in destination.read_text().splitlines()]
            summary = json.loads(destination.with_suffix(".summary.json").read_text())
            return rows, summary

    def test_stops_after_exactly_50_attempts_without_a_gain_over_one_percent(self):
        values = iter([(20, True)]*3 + [(19.8, True)]*50)
        rows, summary = self.run_search(values)
        self.assertEqual(len(rows), 51)
        self.assertEqual(summary["stale_iterations"], 50)
        self.assertEqual(summary["stop_reason"], "patience")
        self.assertEqual(summary["best"]["cpu_pct"], 20)
        self.assertEqual(sum(row["accepted"] for row in rows), 1)

    def test_confirmed_improvement_resets_the_patience_counter(self):
        values = iter([(20, True)]*3 + [(19.7, True)]*3 + [(19.7, True)]*50)
        rows, summary = self.run_search(values)
        self.assertEqual(len(rows), 52)
        self.assertEqual(rows[1]["stale_iterations"], 0)
        self.assertTrue(rows[1]["accepted"])
        self.assertEqual(summary["best"]["cpu_pct"], 19.7)
        self.assertEqual(summary["stale_iterations"], 50)

    def test_lower_cpu_with_failed_fps_confirmation_is_rejected(self):
        values = iter([(20, True)]*3 + [(18, True), (18, True), (1, False)]
                      + [(20, True)]*49)
        rows, summary = self.run_search(values)
        self.assertFalse(rows[1]["accepted"])
        self.assertFalse(rows[1]["valid"])
        self.assertEqual(summary["best"]["cpu_pct"], 20)
        self.assertEqual(summary["stale_iterations"], 50)


if __name__ == "__main__":
    unittest.main()
