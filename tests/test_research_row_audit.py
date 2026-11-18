"""Research exports must disclose gaps and never shift features by horizon."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from banknifty.groww import IST
from banknifty.research_row_audit import audit_research_rows


FEATURES = ("current_price", "return_5m_pct")


def rows_for_day(day, partition="unassigned_too_few_dates"):
    origin = datetime(2026, 10, day, 10, 16, tzinfo=IST)
    rows = []
    for horizon in (15, 30, 60):
        rows.append({
            "source": "Groww index feed sampled updates",
            "session_date_ist": origin.date().isoformat(),
            "partition": partition,
            "origin_at_utc": origin.isoformat(),
            "decision_at_utc": (origin + timedelta(seconds=5)).isoformat(),
            "target_at_utc": (origin + timedelta(minutes=horizon)).isoformat(),
            "horizon_minutes": horizon,
            "origin_close_age_seconds": 4.0,
            "target_close_age_seconds": 4.0,
            "label_status": "ready",
            "target_return_pct": 0.2,
            "current_price": 55000.0,
            "return_5m_pct": 0.1,
            "known_recent_bank_events": None,
            "known_recent_macro_events": None,
            "known_upcoming_events": None,
            "event_context_status": "no_event_source",
            "event_context_forecast_eligible": False,
        })
    return rows


class ResearchRowAuditTests(unittest.TestCase):
    def test_consistent_rows_pass_without_claiming_source_authentication(self):
        result = audit_research_rows(rows_for_day(5), FEATURES)
        self.assertEqual((result["checked_rows"], result["checked_origins"],
                          result["checked_dates"]), (3, 1, 1))
        self.assertIn("not_source_authentication", result["status"])

    def test_rejects_future_cutoff_wrong_target_and_horizon_dependent_feature(self):
        for change, message in (
            ({"decision_at_utc": datetime(2026, 10, 5, 10, 17, tzinfo=IST).isoformat()}, "cutoff"),
            ({"target_at_utc": datetime(2026, 10, 5, 10, 16, tzinfo=IST).isoformat()}, "target"),
            ({"current_price": 56000.0}, "horizon-dependent"),
            ({"source": "official daily OHLC"}, "source"),
        ):
            with self.subTest(change=change):
                rows = rows_for_day(5)
                rows[1].update(change)
                with self.assertRaisesRegex(ValueError, message):
                    audit_research_rows(rows, FEATURES)

    def test_unavailable_label_cannot_carry_a_return_or_close_age(self):
        rows = rows_for_day(5)
        rows[0].update(label_status="missing_data", target_return_pct=None,
                       target_close_age_seconds=None)
        self.assertEqual(audit_research_rows(rows, FEATURES)["checked_rows"], 3)
        rows[0]["target_return_pct"] = 1.0
        with self.assertRaisesRegex(ValueError, "fabricated"):
            audit_research_rows(rows, FEATURES)

    def test_expiry_reference_must_precede_decision(self):
        rows = rows_for_day(27)
        for row in rows:
            row.update(expiry_date="2026-10-27", calendar_days_to_expiry=0,
                       is_expiry_day=True, is_pre_expiry_session=False,
                       expiry_reference_observed_at_utc=datetime(2026, 10, 27, 9, 0,
                           tzinfo=IST).astimezone(timezone.utc).isoformat(),
                       expiry_reference_source="Groww instrument catalogue",
                       expiry_catalog_sha256="0" * 64)
        self.assertEqual(audit_research_rows(rows, FEATURES)["checked_rows"], 3)
        rows[0]["expiry_reference_observed_at_utc"] = datetime(2026, 10, 27,
            10, 17, tzinfo=IST).astimezone(timezone.utc).isoformat()
        with self.assertRaisesRegex(ValueError, "Expiry context"):
            audit_research_rows(rows, FEATURES)

    def test_ready_label_and_origin_identity_are_independently_checked(self):
        rows = rows_for_day(5)
        rows[0]["target_close_age_seconds"] = 10.0
        with self.assertRaisesRegex(ValueError, "near-end"):
            audit_research_rows(rows, FEATURES)
        rows = rows_for_day(5)
        rows.append(deepcopy(rows[0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            audit_research_rows(rows, FEATURES)
        rows = rows_for_day(5)[:2]
        with self.assertRaisesRegex(ValueError, "missing a reviewed horizon"):
            audit_research_rows(rows, FEATURES)

    def test_whole_date_partitions_must_be_chronological(self):
        rows = []
        for day, partition in ((5, "development"), (6, "development"),
                               (7, "development"), (8, "validation"), (9, "holdout")):
            rows.extend(rows_for_day(day, partition))
        self.assertEqual(audit_research_rows(rows, FEATURES)["checked_dates"], 5)
        rows[-1]["partition"] = "development"
        with self.assertRaisesRegex(ValueError, "spans"):
            audit_research_rows(rows, FEATURES)


if __name__ == "__main__":
    unittest.main()
