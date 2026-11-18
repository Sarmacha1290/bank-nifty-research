from datetime import datetime, timezone
from decimal import Decimal
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from banknifty.options_import import CSV_FIELDS, load_options_file, parse_options_bytes
from banknifty.options_data import validate_quote


def document():
    return {'schema_version': 1, 'underlying_price': '50000', 'contracts': [{
        'contract_id': 'EXAMPLE-CALL', 'underlying': 'BANKNIFTY', 'exchange': 'NSE',
        'option_type': 'CALL', 'strike': '50000', 'expiry_at': '2026-01-29T15:30:00+05:30',
        'lot_size': 10, 'tick_size': '0.05', 'effective_from': '2026-01-01T00:00:00+05:30',
        'effective_until': None, 'known_at': '2026-01-01T00:00:00+05:30', 'listed': True}],
        'quotes': [{'contract_id': 'EXAMPLE-CALL', 'source_at': '2026-01-05T10:00:00+05:30',
        'received_at': '2026-01-05T10:00:01+05:30', 'bid': '100.00', 'ask': '101.00',
        'open_interest': 0}]}


class OptionsImportTests(unittest.TestCase):
    def parse(self, data):
        return parse_options_bytes(json.dumps(data).encode())

    def test_import_preserves_claimed_times_zero_oi_and_unverified_provenance(self):
        data = self.parse(document())
        quote = data.quotes[0]
        self.assertEqual(quote.open_interest, 0)
        self.assertIsNone(quote.oi_source_at)
        self.assertEqual(quote.provenance.evidence_kind, 'replay_quote')
        self.assertIn(b'reconstructed_unverified', quote.provenance.payload)
        self.assertEqual(data.underlying_price, Decimal('50000'))
        result = validate_quote(quote, data.contracts[0], quote.received_at)
        self.assertTrue(result.accepted)
        self.assertFalse(result.verified_live)

    def test_missing_source_remains_unknown_and_rejected(self):
        raw = document(); raw['quotes'][0]['source_at'] = None
        result = self.parse(raw)
        quote = result.quotes[0]
        self.assertIsNone(quote.source_at)
        self.assertFalse(validate_quote(quote, result.contracts[0], quote.received_at).accepted)

    def test_unknown_or_credential_fields_and_claimed_verification_rejected(self):
        for key in ['access_token', 'api_key', 'evidence_kind', 'provenance', 'greeks', 'close']:
            with self.subTest(key=key):
                raw = document(); raw['quotes'][0][key] = 'FAKE-TEST-VALUE'
                with self.assertRaises(ValueError) as caught:
                    self.parse(raw)
                self.assertNotIn('FAKE-TEST-VALUE', str(caught.exception))

    def test_malformed_and_nonfinite_and_duplicate_keys(self):
        for raw in [b'{}', b'[]', b'null', b'{', b'{"schema_version":1,"schema_version":1}', b'{"value":NaN}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_options_bytes(raw)
        for bad in ['NaN', 'Infinity', '-1', '99999999999999999999']:
            raw = document(); raw['quotes'][0]['bid'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError): self.parse(raw)

    def test_rejects_naive_dates_booleans_fractional_lots_and_unmapped_contract(self):
        for location, key, value in [('quotes','received_at','2026-01-05T10:00:00'),
                ('quotes','contract_id','MISSING'), ('contracts','lot_size',True),
                ('contracts','lot_size',1.5), ('contracts','listed','true')]:
            raw = document(); raw[location][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.parse(raw)

    def test_csv_roundtrip_and_conflicting_contract_version(self):
        raw = document(); row = {**raw['contracts'][0], **raw['quotes'][0], 'underlying_price':'50000'}
        row['listed'] = 'true'
        output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader(); writer.writerow(row)
        parsed = parse_options_bytes(output.getvalue().encode(), format='csv')
        self.assertEqual(parsed.quotes[0].bid, Decimal('100.00'))
        row['lot_size'] = 20; writer.writerow(row)
        with self.assertRaisesRegex(ValueError, 'Conflicting'): parse_options_bytes(output.getvalue().encode(), format='csv')

    def test_bounded_file_and_existing_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'quotes.json'; raw = json.dumps(document()).encode(); path.write_bytes(raw)
            self.assertEqual(len(load_options_file(path).quotes), 1)
            self.assertEqual(path.read_bytes(), raw)
            path.write_bytes(b' ' * (2*1024*1024+1))
            with self.assertRaises(ValueError): load_options_file(path)

    def test_historical_candles_not_a_quote_schema(self):
        with self.assertRaises(ValueError):
            self.parse({'candles': [{'open': 100, 'high': 101, 'low': 99, 'close':100}]})

    def test_future_rows_and_repeat_import_time_do_not_change_past_record_identity(self):
        raw = document()
        old = self.parse(raw)
        future = dict(raw['quotes'][0], source_at='2026-01-06T10:00:00+05:30',
                      received_at='2026-01-06T10:00:01+05:30', bid='120', ask='121')
        raw['quotes'].append(future)
        revised = self.parse(raw)
        self.assertNotEqual(old.file_sha256, revised.file_sha256)
        self.assertEqual(old.quotes[0].fingerprint, revised.quotes[0].fingerprint)
        self.assertEqual(old.contracts[0].fingerprint, revised.contracts[0].fingerprint)
        self.assertIn(b'reconstructed_unverified', revised.quotes[0].provenance.payload)

    def test_numeric_price_lexemes_keep_precision_for_tick_validation(self):
        raw = json.dumps(document()).replace('"100.00"', '100.00000000000000000001').encode()
        imported = parse_options_bytes(raw)
        quote = imported.quotes[0]
        self.assertEqual(quote.bid, Decimal('100.00000000000000000001'))
        self.assertFalse(validate_quote(quote, imported.contracts[0], quote.received_at).accepted)


if __name__ == '__main__': unittest.main()
