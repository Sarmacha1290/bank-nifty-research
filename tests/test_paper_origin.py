"""Paper origin evidence cannot be revised by later receipt or archive growth."""

from dataclasses import replace
from contextlib import closing
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from banknifty.feed import IndexTick
from banknifty.feed_technical import assess_recorded_feed
from banknifty.groww import IST
from banknifty.paper_log import (export_paper_report_csv, paper_outcome_summary,
                                record_paper_assessments, resolve_paper_outcomes)
from banknifty.tick_history import save_index_tick
from tests.test_paper_log import sampled_updates


class FrozenPaperOriginTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        self.origin = self.start + timedelta(minutes=61)
        self.cutoff = self.origin + timedelta(seconds=2)
        self.ticks = sampled_updates(self.start)
        _, self.assessments = assess_recorded_feed(self.ticks, generated_at=self.cutoff)

    def save(self, database, ticks=None, assessments=None):
        return record_paper_assessments(database, assessments or self.assessments,
            recorded_at=self.cutoff, evidence_ticks=self.ticks if ticks is None else ticks)

    def read(self, database):
        with closing(sqlite3.connect(database)) as connection, connection:
            return connection.execute("SELECT payload, sha256 FROM paper_rule_assessments ORDER BY horizon_minutes").fetchall()

    def add_future(self, database, *, uplift=0):
        for minute in range(61, 76):
            for second in range(0, 60, 5):
                at = self.start + timedelta(minutes=minute, seconds=second)
                save_index_tick(database, IndexTick("NSE-BANKNIFTY", at,
                    at + timedelta(seconds=1), 55000 + minute * .5 + second * .01 + uplift))

    def test_frozen_price_and_raw_input_hash_are_saved_and_exported(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "paper.sqlite3"
            self.save(database)
            encoded, digest = self.read(database)[0]
            self.assertEqual(sha256(encoded.encode()).hexdigest(), digest)
            frozen = json.loads(encoded)["frozen_origin"]
            self.assertEqual(frozen["price"], self.ticks[-1].value)
            self.assertEqual(len(frozen["input_ticks"]), 61 * 12)
            inputs = json.dumps(frozen["input_ticks"], sort_keys=True, separators=(",", ":"), allow_nan=False)
            self.assertEqual(frozen["input_sha256"], sha256(inputs.encode()).hexdigest())
            output = Path(folder) / "report.csv"
            export_paper_report_csv(database, output)
            self.assertIn(frozen["input_sha256"], output.read_text(encoding="utf-8-sig"))

    def test_late_origin_receipt_cannot_change_original_denominator(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "paper.sqlite3"
            self.save(database)
            before = self.read(database)
            for tick in self.ticks:
                save_index_tick(database, tick)
            # Delivered after the decision, within the old five-second minute
            # delivery cutoff: the previous resolver would rewrite its origin.
            save_index_tick(database, IndexTick("NSE-BANKNIFTY",
                self.origin - timedelta(seconds=1), self.origin + timedelta(seconds=4), 99999))
            self.add_future(database)
            resolve_paper_outcomes(database, evaluated_at=self.origin + timedelta(minutes=20))
            with closing(sqlite3.connect(database)) as connection, connection:
                status, value = connection.execute("SELECT status, realized_return_pct FROM paper_rule_outcomes").fetchone()
            self.assertEqual(status, "ready")
            expected = 100 * ((55000 + 75 * .5 + 55 * .01) / self.ticks[-1].value - 1)
            self.assertAlmostEqual(value, expected)
            self.assertEqual(self.read(database), before)

    def test_all_future_values_can_change_without_changing_prior_input(self):
        with TemporaryDirectory() as folder:
            first, second = (Path(folder) / name for name in ("first.sqlite3", "second.sqlite3"))
            for database, uplift in ((first, 0), (second, 10000)):
                future = [IndexTick("NSE-BANKNIFTY", self.origin + timedelta(seconds=10),
                                   self.origin + timedelta(seconds=11), 55000 + uplift)]
                late = [IndexTick("NSE-BANKNIFTY", self.origin - timedelta(seconds=1),
                                 self.cutoff + timedelta(seconds=1), 56000 + uplift)]
                self.save(database, ticks=self.ticks + future + late)
                self.add_future(database, uplift=uplift)
            self.assertEqual(self.read(first), self.read(second))

    def test_archive_fallback_uses_original_generation_cutoff(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "paper.sqlite3"
            for tick in self.ticks:
                save_index_tick(database, tick)
            save_index_tick(database, IndexTick("NSE-BANKNIFTY", self.origin - timedelta(seconds=1),
                                               self.cutoff + timedelta(seconds=1), 99999))
            self.assertEqual(record_paper_assessments(database, self.assessments,
                recorded_at=self.cutoff + timedelta(seconds=20))["inserted"], 3)
            frozen = json.loads(self.read(database)[0][0])["frozen_origin"]
            self.assertEqual(frozen["price"], self.ticks[-1].value)

    def test_changed_receipt_provenance_cannot_replace_existing_origin(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "paper.sqlite3"
            self.save(database)
            before = self.read(database)
            changed = [replace(self.ticks[0], received_at=self.ticks[0].received_at + timedelta(milliseconds=50)),
                       *self.ticks[1:]]
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                self.save(database, ticks=changed)
            self.assertEqual(self.read(database), before)

    def test_missing_or_changed_input_is_rejected_before_recording(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "paper.sqlite3"
            with self.assertRaisesRegex(ValueError, "eligible point-in-time evidence"):
                record_paper_assessments(database, self.assessments, recorded_at=self.cutoff)
            changed = [*self.ticks[:-1], replace(self.ticks[-1], value=99999)]
            with self.assertRaisesRegex(ValueError, "does not match"):
                self.save(database, ticks=changed)
            self.assertFalse(database.exists())

    def test_legacy_origin_remains_unscored_even_with_complete_future_archive(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "paper.sqlite3"
            self.save(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                for encoded, _ in self.read(database):
                    item = json.loads(encoded)
                    item.pop("frozen_origin")
                    text = json.dumps(item, sort_keys=True, separators=(",", ":"))
                    connection.execute("UPDATE paper_rule_assessments SET payload=?, sha256=? WHERE horizon_minutes=?",
                        (text, sha256(text.encode()).hexdigest(), item["horizon_minutes"]))
            for tick in self.ticks:
                save_index_tick(database, tick)
            self.add_future(database)
            resolve_paper_outcomes(database, evaluated_at=self.origin + timedelta(minutes=20))
            self.assertEqual(paper_outcome_summary(database)[15]["legacy_unscored"], 1)
            self.assertEqual(paper_outcome_summary(database)[15]["labelled"], 0)
            # A pre-upgrade result is retained for audit, never silently counted.
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE paper_rule_outcomes SET status='ready', realized_return_pct=1, direction_hit=1")
            self.assertEqual(paper_outcome_summary(database)[15]["directional_calls"], 0)
            output = Path(folder) / "report.csv"
            export_paper_report_csv(database, output)
            self.assertIn("legacy_unfrozen_origin", output.read_text(encoding="utf-8-sig"))

    def test_origin_tampering_rejected_even_when_outer_hash_is_recomputed(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "paper.sqlite3"
            self.save(database)
            item = json.loads(self.read(database)[0][0])
            item["frozen_origin"]["price"] = 123
            text = json.dumps(item, sort_keys=True, separators=(",", ":"))
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE paper_rule_assessments SET payload=?, sha256=? WHERE horizon_minutes=15",
                    (text, sha256(text.encode()).hexdigest()))
            with self.assertRaisesRegex(ValueError, "origin failed validation"):
                resolve_paper_outcomes(database, evaluated_at=self.origin + timedelta(minutes=20))


if __name__ == "__main__":
    unittest.main()
