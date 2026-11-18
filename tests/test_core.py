"""Data integrity and offline-label boundary tests; no external services."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from banknifty.core import Candle, Session, label_returns, prepare_windows, validate_series


IST = timezone(timedelta(hours=5, minutes=30))


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.opening = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        self.session = Session(
            open=self.opening,
            close=self.opening.replace(hour=15, minute=30),
            source="test fixture: explicitly supplied session",
        )

    def candle(self, end_at, close=100.0, **changes):
        fields = {
            "symbol": "BANKNIFTY",
            "kind": "index",
            "start": end_at - timedelta(minutes=1),
            "end": end_at,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "source": "test fixture",
        }
        fields.update(changes)
        return Candle(**fields)

    def series(self, count=61):
        return [
            self.candle(self.opening + timedelta(minutes=minute + 1), 100.0 + minute)
            for minute in range(count)
        ]

    def test_candle_rejects_invalid_prices_and_naive_or_reversed_times(self):
        end = self.opening + timedelta(minutes=1)
        for changes in (
            {"close": float("nan")}, {"open": float("inf")}, {"low": 0},
            {"high": 99}, {"low": 101}, {"close": -1}, {"open": True},
            {"start": end}, {"start": end.replace(tzinfo=None)},
            {"end": end.replace(tzinfo=None)}, {"symbol": ""},
            {"source": " "}, {"kind": "unknown"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.candle(end, **changes)

    def test_index_never_claims_traded_volume_or_open_interest(self):
        candle = self.candle(self.opening + timedelta(minutes=1), volume=100, open_interest=500)
        self.assertIsNone(candle.volume)
        self.assertIsNone(candle.open_interest)
        future = replace(candle, kind="future", volume=0, open_interest=500)
        self.assertEqual(future.volume, 0)
        self.assertEqual(future.open_interest, 500)
        for field in ("volume", "open_interest"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(future, **{field: -1})

    def test_prepared_windows_are_exact_15_30_and_60_minutes(self):
        as_of = self.opening + timedelta(minutes=1)
        rows = prepare_windows(as_of, self.session)
        self.assertEqual([row["horizon_minutes"] for row in rows], [15, 30, 60])
        for row in rows:
            self.assertEqual(row["status"], "ready")
            self.assertEqual(
                datetime.fromisoformat(row["target_at"]),
                as_of + timedelta(minutes=row["horizon_minutes"]),
            )

    def test_window_close_is_allowed_as_target_but_not_as_origin(self):
        rows = prepare_windows(self.session.close - timedelta(minutes=30), self.session)
        self.assertEqual([row["status"] for row in rows], ["ready", "ready", "crosses_session_close"])
        for origin in (self.session.close, self.opening - timedelta(seconds=1)):
            self.assertEqual(
                {row["status"] for row in prepare_windows(origin, self.session)},
                {"market_closed"},
            )

    def test_horizons_and_session_are_validated(self):
        for values in ((0,), (-1,), (True,), (15.0,), (15, 15), (), None):
            with self.subTest(values=values), self.assertRaises(ValueError):
                prepare_windows(self.opening, self.session, values)
        with self.assertRaises(ValueError):
            prepare_windows(self.opening.replace(tzinfo=None), self.session)
        with self.assertRaises(ValueError):
            Session(self.opening, self.opening, "fixture")
        with self.assertRaises(ValueError):
            Session(self.opening, self.session.close, "")

    def test_validate_series_sorts_and_accepts_exactly_completed_candles(self):
        candles = self.series(3)
        self.assertEqual(validate_series(candles[::-1], candles[-1].end), candles)
        self.assertEqual(validate_series([], candles[-1].end), [])

    def test_validate_series_rejects_missing_and_duplicate_intervals(self):
        candles = self.series(3)
        for supplied in ([candles[0], candles[2]], candles + [candles[0]]):
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                validate_series(supplied, candles[-1].end)

    def test_validate_series_rejects_future_knowledge_and_open_candle(self):
        candles = self.series(3)
        for cutoff in (candles[1].end, candles[2].end - timedelta(seconds=1)):
            with self.subTest(cutoff=cutoff), self.assertRaisesRegex(ValueError, "future"):
                validate_series(candles, cutoff)
        with self.assertRaises(ValueError):
            validate_series([], candles[-1].end.replace(tzinfo=None))

    def test_validate_series_rejects_mixed_identity_wrong_duration_and_overlap(self):
        candles = self.series(2)
        for changes in ({"symbol": "OTHER"}, {"kind": "future"}, {"source": "other feed"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "mix"):
                validate_series([candles[0], replace(candles[1], **changes)], candles[-1].end)
        with self.assertRaisesRegex(ValueError, "duration"):
            validate_series([replace(candles[0], start=self.opening - timedelta(minutes=1))], candles[-1].end)
        overlap = replace(candles[1], start=candles[1].start - timedelta(seconds=30), end=candles[1].end - timedelta(seconds=30))
        with self.assertRaisesRegex(ValueError, "overlapping"):
            validate_series([candles[0], overlap], candles[-1].end)

    def test_offline_labels_use_endpoints_not_row_offsets(self):
        candles = self.series()
        origin = candles[0].end
        rows = label_returns(candles[::-1], origin, self.session)
        for row, expected in zip(rows, (15.0, 30.0, 60.0)):
            self.assertEqual(row["status"], "ready")
            self.assertAlmostEqual(row["return_pct"], expected)
        # The same future-containing history must be rejected on the online path.
        with self.assertRaises(ValueError):
            validate_series(candles, origin)

    def test_offline_labels_require_every_intermediate_minute(self):
        candles = self.series()
        supplied = [candle for index, candle in enumerate(candles) if index != 20]
        rows = label_returns(supplied, candles[0].end, self.session)
        self.assertEqual([row["status"] for row in rows], ["ready", "missing_data", "missing_data"])
        self.assertIsNone(rows[1]["return_pct"])
        # A later candle never substitutes for an absent exact target.
        rows = label_returns(candles[:15] + candles[16:], candles[0].end, self.session)
        self.assertEqual(rows[0]["status"], "missing_data")

    def test_offline_labels_require_origin_and_do_not_bridge_sessions(self):
        candles = self.series()
        rows = label_returns(candles[1:], candles[0].end, self.session)
        self.assertEqual({row["status"] for row in rows}, {"missing_data"})
        rows = label_returns(candles, self.session.close - timedelta(minutes=10), self.session)
        self.assertEqual({row["status"] for row in rows}, {"crosses_session_close"})
        self.assertTrue(all(row["return_pct"] is None for row in rows))

    def test_offline_label_exact_close_target_and_session_boundary(self):
        origin = self.session.close - timedelta(minutes=15)
        candles = [self.candle(origin + timedelta(minutes=minute), 100.0 + minute) for minute in range(16)]
        row = label_returns(candles, origin, self.session, (15,))[0]
        self.assertEqual(row["status"], "ready")
        self.assertAlmostEqual(row["return_pct"], 15.0)
        # An origin close stamped at session open belongs to the prior interval.
        candles = [self.candle(self.opening + timedelta(minutes=minute)) for minute in range(16)]
        row = label_returns(candles, self.opening, self.session, (15,))[0]
        self.assertEqual(row["status"], "missing_data")

    def test_timestamps_in_other_offsets_represent_the_same_instant(self):
        candles = self.series(3)
        utc_candle = replace(candles[1], start=candles[1].start.astimezone(timezone.utc), end=candles[1].end.astimezone(timezone.utc))
        checked = validate_series([candles[0], utc_candle, candles[2]], candles[-1].end)
        self.assertEqual(len(checked), 3)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_series(candles + [utc_candle], candles[-1].end)


if __name__ == "__main__":
    unittest.main()
