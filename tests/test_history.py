"""Archive persistence and corruption-prevention checks, without any network."""

import csv
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from banknifty.core import Candle, validate_series
from banknifty.history import export_csv, list_datasets, load_candles, save_candles


IST = timezone(timedelta(hours=5, minutes=30))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "history.sqlite3"
        opening = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
        self.candles = [Candle(
            symbol="DEMO-BANKNIFTY", kind="index",
            start=opening + timedelta(minutes=i),
            end=opening + timedelta(minutes=i + 1),
            open=100 + i, high=102 + i, low=99 + i, close=101 + i,
            source="synthetic",
        ) for i in range(4)]
        self.as_of = self.candles[-1].end

    def save(self, candles=None, **kwargs):
        return save_candles(self.database, self.candles if candles is None else candles,
                            dataset=kwargs.pop("dataset", "demo"),
                            as_of=kwargs.pop("as_of", self.as_of), **kwargs)

    def test_archive_survives_reopen_preserving_provenance_and_utc(self):
        self.assertEqual(self.save(), {"inserted": 4, "duplicates": 0})
        restored = load_candles(self.database, dataset="demo")
        self.assertEqual(restored, self.candles)
        self.assertEqual(restored[0].start.tzinfo, timezone.utc)
        self.assertIsNone(restored[0].volume)
        summaries = list_datasets(self.database)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["source"], "synthetic")
        self.assertEqual(summaries[0]["count"], 4)
        self.assertEqual(datetime.fromisoformat(summaries[0]["first_start"]), self.candles[0].start)
        self.assertEqual(datetime.fromisoformat(summaries[0]["last_end"]), self.as_of)

    def test_duplicate_import_uses_instant_not_timezone_spelling(self):
        self.save()
        utc_rows = [replace(c, start=c.start.astimezone(timezone.utc),
                            end=c.end.astimezone(timezone.utc)) for c in self.candles]
        self.assertEqual(self.save(utc_rows), {"inserted": 0, "duplicates": 4})
        self.assertEqual(list_datasets(self.database)[0]["count"], 4)

    def test_conflict_rolls_back_earlier_new_rows_in_same_batch(self):
        self.save([self.candles[-1]])
        conflicting = replace(self.candles[-1], close=self.candles[-1].close + 0.5)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.save(self.candles[:-1] + [conflicting])
        self.assertEqual(load_candles(self.database, dataset="demo"), [self.candles[-1]])
        # An explicit new dataset preserves both the original and corrected values.
        self.save([conflicting], dataset="demo-correction")
        self.assertEqual(len(list_datasets(self.database)), 2)

    def test_rejects_invalid_slices_without_creating_archive(self):
        cases = (
            ([self.candles[0], self.candles[2]], self.as_of),
            (self.candles, self.as_of - timedelta(seconds=1)),
            (self.candles, self.as_of - timedelta(minutes=1)),
            (self.candles, self.as_of.replace(tzinfo=None)),
            ([replace(self.candles[0], end=self.candles[0].end + timedelta(minutes=1))], self.as_of),
            ([self.candles[0], self.candles[0]], self.as_of),
        )
        for candles, cutoff in cases:
            with self.subTest(candles=candles, cutoff=cutoff), self.assertRaises(ValueError):
                self.save(candles, as_of=cutoff)
        self.assertFalse(self.database.exists())

    def test_dataset_cannot_be_reused_for_other_provenance(self):
        self.save([self.candles[0]])
        for changes in ({"source": "Groww"}, {"symbol": "NSE-BANKNIFTY"}, {"kind": "future"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "mix"):
                self.save([replace(self.candles[1], **changes)])
        self.assertEqual(load_candles(self.database, dataset="demo"), [self.candles[0]])
        with self.assertRaisesRegex(ValueError, "mix"):
            self.save([self.candles[0], replace(self.candles[1], source="Groww")], dataset="mixed")
        self.assertEqual(len(list_datasets(self.database)), 1)

    def test_separate_chunks_keep_gaps_visible(self):
        self.save([self.candles[0]])
        self.save([self.candles[2]])
        archive = load_candles(self.database, dataset="demo")
        self.assertEqual(len(archive), 2)
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_series(archive, self.as_of)
        self.save([self.candles[1]])
        self.assertEqual(validate_series(load_candles(self.database, dataset="demo"), self.as_of), self.candles[:3])

    def test_separate_chunk_cannot_overlap_existing_time_range(self):
        self.save([self.candles[0]])
        overlap = replace(self.candles[1],
                          start=self.candles[1].start - timedelta(seconds=30),
                          end=self.candles[1].end - timedelta(seconds=30))
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.save([overlap])
        self.assertEqual(load_candles(self.database, dataset="demo"), [self.candles[0]])

    def test_csv_is_excel_readable_and_text_cannot_become_formula(self):
        dangerous = replace(self.candles[0], symbol="+1+1", source="\t=1+1")
        dataset = "  @SUM(1,1)"
        self.save([dangerous], dataset=dataset)
        destination = Path(self.temp.name) / "export.csv"
        self.assertEqual(export_csv(self.database, destination, dataset=dataset), 1)
        self.assertTrue(destination.read_bytes().startswith(b"\xef\xbb\xbf"))
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["dataset"], "'" + dataset)
        self.assertEqual(rows[0]["source"], "'\t=1+1")
        self.assertEqual(rows[0]["symbol"], "'+1+1")
        self.assertEqual(rows[0]["volume"], "")
        self.assertEqual(float(rows[0]["close"]), dangerous.close)
        self.assertEqual(load_candles(self.database, dataset=dataset)[0].symbol, "+1+1")

    def test_export_cannot_overwrite_archive(self):
        self.save()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            export_csv(self.database, self.database, dataset="demo")
        self.assertEqual(len(load_candles(self.database, dataset="demo")), 4)

    def test_empty_and_missing_datasets_do_not_create_files(self):
        self.assertEqual(list_datasets(self.database), [])
        self.assertEqual(self.save([]), {"inserted": 0, "duplicates": 0})
        with self.assertRaisesRegex(ValueError, "does not exist"):
            load_candles(self.database, dataset="missing")
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self.save(dataset=" ")
        self.assertFalse(self.database.exists())

    def test_existing_audit_database_can_share_archive(self):
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute("CREATE TABLE events (message TEXT)")
                connection.execute("INSERT INTO events VALUES ('kept')")
        self.assertEqual(list_datasets(self.database), [])
        self.save()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT message FROM events").fetchone(), ("kept",))
        self.assertEqual(len(load_candles(self.database, dataset="demo")), 4)


if __name__ == "__main__":
    unittest.main()
