"""Durable local one-minute candle archive; no credentials or network access.

Each import is a validated continuous slice available at its explicit cutoff.
Separate imports may represent separate sessions or leave gaps. Loading an
archive does NOT certify continuity, freshness, coverage, or trading sessions.
Source, symbol, and instrument kind are fixed for the lifetime of a dataset.
Corrections must use a new dataset; conflicting records are never overwritten.
"""

from contextlib import closing
import csv
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .core import Candle, validate_series


def _dataset_name(dataset):
    if not isinstance(dataset, str) or not dataset.strip():
        raise ValueError("dataset must be a nonempty string")
    return dataset


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _create_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS candle_datasets (
        dataset TEXT PRIMARY KEY, source TEXT NOT NULL,
        symbol TEXT NOT NULL, kind TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS candles (
        dataset TEXT NOT NULL REFERENCES candle_datasets(dataset),
        start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
        open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
        close REAL NOT NULL, volume REAL, open_interest REAL,
        imported_as_of_utc TEXT NOT NULL,
        PRIMARY KEY (dataset, start_utc)
    )""")


def _has_schema(connection):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candle_datasets'"
    ).fetchone() is not None


def save_candles(database, candles, *, dataset, as_of):
    """Atomically save one continuous, completed slice; return import counts.

    Identical reimports are counted as duplicates. A conflict anywhere rolls
    back the entire batch. ``as_of`` records the information cutoff supplied by
    the importer, not proof of the original provider's publication time.
    """
    dataset = _dataset_name(dataset)
    checked = validate_series(list(candles), as_of=as_of, expected_minutes=1)
    counts = {"inserted": 0, "duplicates": 0}
    if not checked:
        return counts
    identity = (checked[0].source, checked[0].symbol, checked[0].kind)
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _create_schema(connection)
            previous = connection.execute(
                "SELECT source, symbol, kind FROM candle_datasets WHERE dataset=?",
                (dataset,),
            ).fetchone()
            if previous is not None and previous != identity:
                raise ValueError("Dataset cannot mix sources, symbols, or instrument kinds.")
            if previous is None:
                connection.execute(
                    "INSERT INTO candle_datasets VALUES (?, ?, ?, ?)",
                    (dataset, *identity),
                )
            for candle in checked:
                start = _utc(candle.start)
                values = (
                    _utc(candle.end), float(candle.open), float(candle.high),
                    float(candle.low), float(candle.close),
                    None if candle.volume is None else float(candle.volume),
                    None if candle.open_interest is None else float(candle.open_interest),
                )
                existing = connection.execute(
                    "SELECT end_utc, open, high, low, close, volume, open_interest "
                    "FROM candles WHERE dataset=? AND start_utc=?",
                    (dataset, start),
                ).fetchone()
                if existing is not None:
                    if existing != values:
                        raise ValueError("Conflicting candle already stored; use a new dataset for corrections.")
                    counts["duplicates"] += 1
                    continue
                overlap = connection.execute(
                    "SELECT 1 FROM candles WHERE dataset=? AND start_utc<? "
                    "AND end_utc>? LIMIT 1", (dataset, values[0], start),
                ).fetchone()
                if overlap is not None:
                    raise ValueError("Imported candle overlaps an existing candle.")
                connection.execute(
                    "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (dataset, start, *values, _utc(as_of)),
                )
                counts["inserted"] += 1
    return counts


def list_datasets(database):
    """Return archive summaries; counts and endpoints do not imply continuity."""
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if not _has_schema(connection):
            return []
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT d.dataset, d.source, d.symbol, d.kind, COUNT(c.start_utc) AS count,
                   MIN(c.start_utc) AS first_start, MAX(c.end_utc) AS last_end
            FROM candle_datasets AS d LEFT JOIN candles AS c ON c.dataset=d.dataset
            GROUP BY d.dataset, d.source, d.symbol, d.kind ORDER BY d.dataset
        """).fetchall()
        return [dict(row) for row in rows]


def load_candles(database, *, dataset):
    """Load sorted UTC candles, which may contain gaps between imported slices.

    Callers must validate their chosen window before calculations. A missing
    dataset raises ValueError, and reading never creates an empty database.
    """
    dataset = _dataset_name(dataset)
    path = Path(database)
    if not path.exists():
        raise ValueError("Dataset does not exist.")
    with closing(sqlite3.connect(path)) as connection:
        if not _has_schema(connection):
            raise ValueError("Dataset does not exist.")
        identity = connection.execute(
            "SELECT source, symbol, kind FROM candle_datasets WHERE dataset=?", (dataset,)
        ).fetchone()
        if identity is None:
            raise ValueError("Dataset does not exist.")
        rows = connection.execute(
            "SELECT start_utc, end_utc, open, high, low, close, volume, open_interest "
            "FROM candles WHERE dataset=? ORDER BY start_utc", (dataset,),
        ).fetchall()
    return [Candle(
        source=identity[0], symbol=identity[1], kind=identity[2],
        start=datetime.fromisoformat(row[0]), end=datetime.fromisoformat(row[1]),
        open=row[2], high=row[3], low=row[4], close=row[5],
        volume=row[6], open_interest=row[7],
    ) for row in rows]


def _excel_safe(value):
    if isinstance(value, str):
        # CSV quoting alone does not stop Excel from evaluating formulas.
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
    return value


def export_csv(database, destination, *, dataset):
    """Export a dataset with provenance, UTC boundaries and Excel-safe text."""
    target = Path(destination)
    source = Path(database)
    if target.resolve() == source.resolve() or (
        target.exists() and source.exists() and target.samefile(source)
    ):
        raise ValueError("Export destination must not overwrite the archive database.")
    candles = load_candles(database, dataset=dataset)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("dataset", "source", "symbol", "kind", "start_utc", "end_utc",
                         "open", "high", "low", "close", "volume", "open_interest"))
        for candle in candles:
            row = (dataset, candle.source, candle.symbol, candle.kind,
                   _utc(candle.start), _utc(candle.end), candle.open, candle.high,
                   candle.low, candle.close, candle.volume, candle.open_interest)
            writer.writerow([_excel_safe(value) for value in row])
    return len(candles)
