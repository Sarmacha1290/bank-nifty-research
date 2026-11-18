from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.feed import IndexTick
from banknifty.feed_technical import assess_recorded_feed
from banknifty.groww import IST
from banknifty.paper_log import (count_paper_assessments, export_paper_report_csv,
                                 record_paper_assessments)
from banknifty.paper_log import paper_outcome_summary, resolve_paper_outcomes
from banknifty.paper_pipeline import attempt_boundary_assessment
from banknifty.tick_history import save_index_tick
from banknifty.technical_features import build_technical_snapshot
from banknifty.workspace import synthetic_session
from banknifty.agents.luna import assess


def sampled_updates(start):
    return [IndexTick("NSE-BANKNIFTY", at, at + timedelta(seconds=1),
                      55000 + minute * 0.5 + offset * 0.01)
            for minute in range(61)
            for offset in range(0, 60, 5)
            for at in [start + timedelta(minutes=minute, seconds=offset)]]


class PaperLogTests(unittest.TestCase):
    def test_live_origin_is_immutable_and_repeated_click_is_a_duplicate(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            ticks = sampled_updates(start)
            recorded = start + timedelta(minutes=61, seconds=20)
            _, assessments = assess_recorded_feed(ticks, generated_at=recorded)
            self.assertEqual(record_paper_assessments(database, assessments, recorded_at=recorded,
                                                    evidence_ticks=ticks)["inserted"], 3)
            self.assertEqual(count_paper_assessments(database), 3)
            _, later = assess_recorded_feed(ticks, generated_at=recorded + timedelta(seconds=1))
            self.assertEqual(record_paper_assessments(database, later, recorded_at=recorded + timedelta(seconds=1),
                                                    evidence_ticks=ticks)["duplicates"], 3)
            self.assertEqual(count_paper_assessments(database), 3)
            report = Path(folder) / "paper.csv"
            self.assertEqual(export_paper_report_csv(database, report), 3)
            content = report.read_text(encoding="utf-8-sig")
            self.assertIn("rules_score_not_probability", content)
            self.assertIn("pending", content)
            with self.assertRaisesRegex(ValueError, "overwrite"):
                export_paper_report_csv(database, database)

    def test_synthetic_replay_cannot_enter_live_paper_log(self):
        session, candles = synthetic_session()
        as_of = session.open + timedelta(minutes=75)
        snapshot = build_technical_snapshot(candles[:75], as_of, generated_at=as_of)
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "real Groww"):
                record_paper_assessments(Path(folder) / "market.sqlite3", [assess(snapshot, 15)], recorded_at=as_of)

    def test_session_close_horizons_are_not_logged(self):
        with TemporaryDirectory() as folder:
            start = datetime(2026, 10, 5, 14, 5, tzinfo=IST)
            recorded = start + timedelta(minutes=61, seconds=20)
            _, assessments = assess_recorded_feed(sampled_updates(start), generated_at=recorded)
            result = record_paper_assessments(Path(folder) / "market.sqlite3", assessments, recorded_at=recorded,
                                              evidence_ticks=sampled_updates(start))
            self.assertEqual(result, {"inserted": 1, "duplicates": 0, "ineligible": 2})

    def test_later_outcomes_wait_for_exact_target_and_report_missing_data(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            origin = start + timedelta(minutes=61)
            _, assessments = assess_recorded_feed(sampled_updates(start),
                                                  generated_at=origin + timedelta(seconds=20))
            record_paper_assessments(database, assessments, recorded_at=origin + timedelta(seconds=20),
                                     evidence_ticks=sampled_updates(start))
            self.assertEqual(resolve_paper_outcomes(database, evaluated_at=origin + timedelta(minutes=19))["pending"], 3)
            for minute in range(61 + 15):
                stamp = start + timedelta(minutes=minute)
                for offset in range(0, 60, 5):
                    at = stamp + timedelta(seconds=offset)
                    save_index_tick(database, IndexTick("NSE-BANKNIFTY", at, at + timedelta(seconds=1),
                                                        55000 + minute * 0.5 + offset * 0.01))
            first = resolve_paper_outcomes(database, evaluated_at=origin + timedelta(minutes=20))
            self.assertEqual((first["resolved"], first["pending"], first["missing_data"]), (1, 2, 0))
            final = resolve_paper_outcomes(database, evaluated_at=origin + timedelta(minutes=65))
            self.assertEqual((final["resolved"], final["missing_data"]), (2, 2))
            summary = paper_outcome_summary(database)
            self.assertEqual(summary[15]["labelled"], 1)
            self.assertEqual(summary[30]["labelled"], 0)
            report = Path(folder) / "paper-outcomes.csv"
            self.assertEqual(export_paper_report_csv(database, report), 3)
            self.assertIn("missing_data", report.read_text(encoding="utf-8-sig"))
            self.assertEqual(resolve_paper_outcomes(database, evaluated_at=origin + timedelta(minutes=66))["already_resolved"], 3)

    def test_boundary_pipeline_uses_only_ticks_before_new_minute(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            boundary = start + timedelta(minutes=61)
            for tick in sampled_updates(start):
                save_index_tick(database, tick)
            # This extreme current-minute value must not enter the prior 61 bars.
            save_index_tick(database, IndexTick("NSE-BANKNIFTY", boundary,
                                               boundary + timedelta(seconds=1), 99999))
            self.assertEqual(attempt_boundary_assessment(database, boundary,
                                                        boundary + timedelta(seconds=2)), 3)
            self.assertEqual(count_paper_assessments(database), 3)

    def test_weak_sampled_edges_cannot_become_paper_results(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            origin = start + timedelta(minutes=61)
            ticks = sampled_updates(start)
            last_minute = start + timedelta(minutes=60)
            weak_origin = [tick for tick in ticks
                           if tick.exchange_at < last_minute]
            weak_origin += [IndexTick("NSE-BANKNIFTY", last_minute + timedelta(seconds=second),
                                     last_minute + timedelta(seconds=second + 1), 55030 + second * 0.01)
                            for second in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 49, 50)]
            with self.assertRaisesRegex(ValueError, "too far"):
                assess_recorded_feed(weak_origin, generated_at=origin + timedelta(seconds=2))
            _, assessments = assess_recorded_feed(ticks,
                                                  generated_at=origin + timedelta(seconds=2))
            record_paper_assessments(database, assessments,
                                     recorded_at=origin + timedelta(seconds=2), evidence_ticks=ticks)
            target_minute = 75
            for minute in range(76):
                seconds = (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 49, 50) if minute == target_minute else range(0, 60, 5)
                for second in seconds:
                    at = start + timedelta(minutes=minute, seconds=second)
                    save_index_tick(database, IndexTick("NSE-BANKNIFTY", at,
                        at + timedelta(seconds=1), 55000 + minute * 0.5 + second * 0.01))
            # A better boundary price delivered after the five-second cutoff
            # must not retroactively upgrade the frozen paper outcome.
            late_at = start + timedelta(minutes=target_minute, seconds=55)
            save_index_tick(database, IndexTick("NSE-BANKNIFTY", late_at,
                start + timedelta(minutes=target_minute + 1, seconds=6), 55038.05))
            result = resolve_paper_outcomes(database,
                                             evaluated_at=origin + timedelta(minutes=20))
            self.assertEqual(result["resolved"], 1)
            self.assertEqual(paper_outcome_summary(database)[15]["labelled"], 0)
            report = Path(folder) / "paper.csv"
            export_paper_report_csv(database, report)
            self.assertIn("weak_target_edge", report.read_text(encoding="utf-8-sig"))

    def test_assessment_excludes_updates_not_yet_received(self):
        start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        origin = start + timedelta(minutes=61)
        ticks = sampled_updates(start)
        last = ticks[-1]
        ticks[-1] = IndexTick(last.symbol, last.exchange_at,
                              origin + timedelta(seconds=3), last.value)
        with self.assertRaisesRegex(ValueError, "61 accepted"):
            assess_recorded_feed(ticks, generated_at=origin + timedelta(seconds=2))
        _, assessments = assess_recorded_feed(ticks,
                                              generated_at=origin + timedelta(seconds=4))
        self.assertEqual(len(assessments), 3)


if __name__ == "__main__":
    unittest.main()
