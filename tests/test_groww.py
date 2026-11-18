from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

from banknifty.groww import (DataAccessError, GrowwDataClient, NoRedirect,
                           IST, normalize_candles, resolve_banknifty, resolve_nifty50)
from banknifty.storage import record_event

CATALOG = "exchange,segment,instrument_type,trading_symbol,groww_symbol,exchange_token\nNSE,CASH,IDX,BANKNIFTY,NSE-BANKNIFTY,BANKNIFTY\nNSE,FNO,CE,BANKNIFTY_OPTION,NSE-SOME-OPTION,12345\n"


class GrowwTests(unittest.TestCase):
    def test_index_is_resolved_from_catalogue(self):
        self.assertEqual(resolve_banknifty(CATALOG)["exchange_token"], "BANKNIFTY")

    def test_ambiguous_catalogue_rejected(self):
        with self.assertRaises(DataAccessError):
            resolve_banknifty(CATALOG + CATALOG.splitlines()[1] + "\n")

    def test_nifty_fallback_uses_distinct_exact_catalogue_identity(self):
        catalog = CATALOG + "NSE,CASH,IDX,NIFTY,NSE-NIFTY,NIFTY\n"
        self.assertEqual(resolve_nifty50(catalog)["groww_symbol"], "NSE-NIFTY")
        with self.assertRaises(DataAccessError):
            resolve_nifty50(CATALOG)
        with self.assertRaises(DataAccessError):
            resolve_nifty50(catalog + catalog.splitlines()[-1] + "\n")

    def test_token_not_in_repr_and_client_can_be_closed(self):
        client = GrowwDataClient("SECRET_TEST_TOKEN")
        self.assertNotIn("SECRET_TEST_TOKEN", repr(client))
        client.close()
        with self.assertRaises(DataAccessError):
            client.index_snapshot(resolve_banknifty(CATALOG))

    def test_order_endpoint_blocked_before_network(self):
        with patch("banknifty.groww.build_opener") as network:
            with self.assertRaises(DataAccessError):
                GrowwDataClient("fixture")._get("/order/create", {})
            network.assert_not_called()

    @patch("banknifty.groww.build_opener")
    def test_catalogue_option_quote_keeps_get_controls_and_exact_decimal(self, opener):
        from decimal import Decimal
        from tests.test_provider_adapters import instruments
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"SUCCESS","payload":{"bid_price":100.00000000000000000001}}'
        opener.return_value.open.return_value = response
        result = GrowwDataClient("fixture").option_quote(instruments()[1])
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertIn("/live-data/quote?", request.full_url)
        self.assertIn("segment=FNO", request.full_url)
        self.assertNotIn("fixture", request.full_url)
        self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 25)
        self.assertEqual(result["payload"]["bid_price"], Decimal("100.00000000000000000001"))

    def test_option_quote_requires_catalogue_identity_before_network(self):
        with patch("banknifty.groww.build_opener") as network:
            with self.assertRaises(ValueError): GrowwDataClient("fixture").option_quote("BANKNIFTY25DEC27000PE")
            network.assert_not_called()

    def test_redirect_blocked(self):
        with self.assertRaises(DataAccessError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")

    @patch("banknifty.groww.build_opener")
    def test_snapshot_is_not_labelled_fresh(self, opener):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "status": "SUCCESS", "payload": {"NSE_BANKNIFTY": 50000}}).encode()
        opener.return_value.open.return_value = response
        result = GrowwDataClient("fixture").index_snapshot(resolve_banknifty(CATALOG))
        self.assertEqual(result["freshness"], "unverified")
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertNotIn("fixture", request.full_url)

    @patch("banknifty.groww.build_opener")
    def test_http_error_does_not_echo_server_secret(self, opener):
        opener.return_value.open.side_effect = HTTPError("https://example", 401,
            "SECRET_TEST_TOKEN", {}, BytesIO(b"SECRET_TEST_TOKEN"))
        with self.assertRaises(DataAccessError) as context:
            GrowwDataClient("fixture").index_snapshot(resolve_banknifty(CATALOG))
        self.assertNotIn("SECRET_TEST_TOKEN", str(context.exception))

    def test_normalization_requires_explicit_time_conventions(self):
        response = {"status": "SUCCESS", "payload": {"interval_in_minutes": 1,
            "candles": [["2026-10-01T10:00:00", 100, 102, 99, 101, 0, None]]}}
        with self.assertRaises(ValueError):
            normalize_candles(response, symbol="NSE-BANKNIFTY", kind="index", timestamp_basis="start")
        candle = normalize_candles(response, symbol="NSE-BANKNIFTY", kind="index",
            timestamp_basis="start", naive_timezone=IST)[0]
        self.assertEqual(candle.end, datetime(2026, 10, 1, 10, 1, tzinfo=IST))
        self.assertIsNone(candle.volume)

    def test_historical_range_limit(self):
        start = datetime(2026, 1, 1, tzinfo=IST)
        with self.assertRaises(ValueError):
            GrowwDataClient("fixture").historical_candles(resolve_banknifty(CATALOG),
                start, start + timedelta(days=31))

    def test_audit_rejects_credentials_and_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            with self.assertRaises(ValueError):
                record_event(database, "bad", {"headers": {"Authorization": "secret"}})
            with self.assertRaises(ValueError):
                record_event(database, "bad", {"items": ({"api_key": "secret"},)})
            one = record_event(database, "test", {"value": 10})
            two = record_event(database, "test", {"value": 10})
            self.assertNotEqual(one, two)


if __name__ == "__main__":
    unittest.main()
