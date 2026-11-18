"""Official-schema fixtures, dated illustrative values; no live account calls."""
import ast
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import unittest

from banknifty.options_data import validate_quote
from banknifty.provider_adapters import (
    adapt_index_feed, adapt_rest_ltp, adapt_option_quote, adapt_option_depth_feed,
    adapt_option_ltp_feed, adapt_option_chain, decode_provider_json,
    instrument_contract, parse_groww_catalogue, plan_option_quote, plan_index_ltp,
    plan_option_chain, provider_readiness,
)

D = Decimal
AT = datetime(2025, 5, 2, 9, 0, tzinfo=timezone.utc)
MS = int(AT.timestamp() * 1000)
HEADER = 'exchange,exchange_token,trading_symbol,groww_symbol,instrument_type,segment,underlying_symbol,expiry_date,strike_price,lot_size,tick_size'
INDEX = 'NSE,26009,BANKNIFTY,NSE-BANKNIFTY,IDX,CASH,,,,,'
OPTION = 'NSE,49445,BANKNIFTY25DEC27000PE,NSE-BANKNIFTY-24Dec25-27000-PE,PE,FNO,BANKNIFTY,2025-12-24,27000,35,0.05'
CALL = 'NSE,60123,BANKNIFTY25DEC51000CE,NSE-BANKNIFTY-24Dec25-51000-CE,CE,FNO,BANKNIFTY,2025-12-24,51000,35,0.05'


def catalogue(text=None, at=AT-timedelta(days=1)):
    return parse_groww_catalogue(text or '\n'.join((HEADER, INDEX, OPTION, CALL)), at)


def instruments():
    rows = catalogue().instruments
    return rows[0], rows[1], rows[2]


def contract():
    return instrument_contract(instruments()[1], expiry_at=datetime(2025, 12, 24, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))))


def rest():
    # Groww REST quote documented names. last_trade_time is explicitly a trade.
    return {'status': 'SUCCESS', 'payload': {'bid_price': 150, 'bid_quantity':1000,
        'offer_price':150.5, 'offer_quantity':2000, 'last_price':149.5,
        'last_trade_time':MS, 'open_interest':2000, 'volume':10000, 'implied_volatility':0.25}}


def depth(**changes):
    row = {'tsInMillis':MS, 'buyBook':{'2':{'price':1418.7,'qty':23.0},'1':{'price':1418.8,'qty':113.0}},
           'sellBook':{'2':{'price':1419.0,'qty':472.0},'1':{'price':1418.9,'qty':3.0}}}
    row.update(changes)
    return {'NSE':{'FNO':{'49445':row}}}


class ProviderAdapterTests(unittest.TestCase):
    def test_documented_catalogue_fields_and_observation_dates_are_preserved(self):
        snapshot = catalogue()
        self.assertEqual(len(snapshot.instruments), 3)
        instrument = snapshot.instruments[1]
        self.assertEqual((instrument.exchange_token, instrument.lot_size, instrument.tick_size), ('49445',35,D('0.05')))
        self.assertEqual(instrument.expiry_date, date(2025,12,24))
        metadata = contract()
        self.assertEqual(metadata.known_at, AT-timedelta(days=1))
        self.assertEqual(metadata.effective_from, metadata.known_at)
        self.assertEqual(metadata.option_type, 'PUT')
        self.assertEqual(metadata.provenance.evidence_kind, 'live_unverified')
        self.assertIn(b'"lot_size":"35"', metadata.provenance.payload)
        self.assertEqual(len(snapshot.file_sha256), 64)
        self.assertTrue(snapshot.raw.startswith(b'exchange,'))

    def test_expiry_clock_is_required_and_never_inferred(self):
        option = instruments()[1]
        with self.assertRaises(TypeError): instrument_contract(option)
        for stamp in (datetime(2025,12,24), AT, datetime(2025,12,25,tzinfo=timezone.utc)):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                instrument_contract(option, expiry_at=stamp)
        with self.assertRaises(ValueError):
            instrument_contract(option, expiry_at=contract().expiry_at, effective_until=option.observed_at)

    def test_lot_and_tick_changes_are_observed_versions_not_backdated(self):
        newer = catalogue('\n'.join((HEADER, INDEX, OPTION.replace(',35,0.05',',30,0.10'))), AT)
        old, new = catalogue().instruments[1], newer.instruments[1]
        self.assertEqual(old.lot_size,35)
        self.assertEqual(new.lot_size,30)
        self.assertEqual(new.observed_at,AT)
        self.assertEqual(new.tick_size,D('0.10'))
        with self.assertRaises(ValueError): adapt_option_quote(rest(),new,AT-timedelta(seconds=1))

    def test_conflicting_catalogue_identity_and_invalid_columns_fail_closed(self):
        invalid = [HEADER+'\n'+OPTION+'\n'+OPTION.replace(',35,',',30,'),
                   HEADER+'\n'+OPTION.replace(',35,',',0,'),
                   HEADER+'\n'+OPTION.replace('2025-12-24','tomorrow'),
                   HEADER+'\n'+INDEX.replace('NSE-BANKNIFTY','NSE-NIFTY'),
                   HEADER+',access_token\n'+OPTION+',FAKE_TEST_TOKEN',
                   HEADER+',exchange\n'+OPTION+',NSE']
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError): catalogue(text)
        self.assertEqual(len(catalogue(HEADER+'\n'+OPTION+'\n'+OPTION).instruments),1)

    def test_unrelated_instruments_are_excluded_and_unknown_columns_retained(self):
        unrelated = OPTION.replace('BANKNIFTY','BANKINDIA')
        snapshot = catalogue(HEADER+',future_column\n'+OPTION+',safe\n'+unrelated+',other')
        self.assertEqual(len(snapshot.instruments),1)
        self.assertIn(b'future_column',snapshot.instruments[0].provenance.payload)

    def test_index_feed_mapping_and_missing_timestamp(self):
        index,_,_ = instruments()
        payload={'NSE':{'CASH':{'26009':{'tsInMillis':MS,'value':24386.7}}}}
        observed=adapt_index_feed(payload,index,AT)
        self.assertEqual(observed.source_at,AT)
        self.assertEqual(observed.value,D('24386.7'))
        self.assertEqual(observed.symbol,'BANKNIFTY')
        self.assertEqual(observed.provenance.evidence_kind,'live_unverified')
        del payload['NSE']['CASH']['26009']['tsInMillis']
        self.assertIsNone(adapt_index_feed(payload,index,AT).source_at)
        payload['NSE']['CASH']['26009']=None
        self.assertIsNone(adapt_index_feed(payload,index,AT))

    def test_rest_index_ltp_is_receipt_only(self):
        index,_,_=instruments()
        result=adapt_rest_ltp({'status':'SUCCESS','payload':{'NSE_BANKNIFTY':50000,'timestamp':MS}},index,AT)
        self.assertIsNone(result.source_at)
        self.assertEqual(result.received_at,AT)
        self.assertIn(b'timestamp',result.provenance.payload)

    def test_rest_quote_trade_timestamp_never_dates_quote_or_oi(self):
        result=adapt_option_quote(rest(),instruments()[1],AT)
        self.assertEqual((result.quote.bid,result.quote.ask),(D('150'),D('150.5')))
        self.assertIsNone(result.quote.source_at)
        self.assertIsNone(result.quote.oi_source_at)
        fields={f.name:f for f in result.fields}
        self.assertEqual(fields['last_price'].source_at,AT)
        self.assertIsNone(fields['open_interest'].source_at)
        self.assertFalse(validate_quote(result.quote,contract(),AT).accepted)

    def test_unknown_rest_timestamp_fields_cannot_upgrade_freshness(self):
        response=rest();response['payload'].update(timestamp=MS,exchange_timestamp=MS,tsInMillis=MS,oi_timestamp=MS)
        mapped=adapt_option_quote(response,instruments()[1],AT)
        self.assertIsNone(mapped.quote.source_at)
        self.assertIsNone(mapped.quote.oi_source_at)
        self.assertIn(b'exchange_timestamp',mapped.quote.provenance.payload)

    def test_zero_oi_and_missing_oi_remain_distinct(self):
        response=rest();response['payload']['open_interest']=0
        self.assertEqual(adapt_option_quote(response,instruments()[1],AT).quote.open_interest,0)
        del response['payload']['open_interest']
        self.assertIsNone(adapt_option_quote(response,instruments()[1],AT).quote.open_interest)

    def test_depth_feed_uses_best_prices_and_actual_sizes(self):
        mapped=adapt_option_depth_feed(depth(),instruments()[1],AT)
        self.assertEqual((mapped.quote.bid,mapped.quote.ask),(D('1418.8'),D('1418.9')))
        self.assertEqual((mapped.quote.bid_size,mapped.quote.ask_size),(113,3))
        self.assertEqual(mapped.quote.source_at,AT)
        self.assertTrue(validate_quote(mapped.quote,contract(),AT).accepted)
        self.assertFalse(validate_quote(mapped.quote,contract(),AT).verified_live)
        self.assertIsNone(mapped.quote.open_interest)

    def test_depth_documentation_seconds_sized_example_is_never_autoscaled(self):
        mapped=adapt_option_depth_feed(depth(tsInMillis=1746156600.0),instruments()[1],AT)
        self.assertEqual(mapped.quote.source_at.year,1970)
        self.assertIn('documented_millisecond_timestamp_far_from_receipt',mapped.issues)
        self.assertFalse(validate_quote(mapped.quote,contract(),AT).accepted)

    def test_missing_crossed_fractional_and_duplicate_depth_fail_conservatively(self):
        missing=adapt_option_depth_feed(depth(buyBook=None),instruments()[1],AT)
        self.assertIsNone(missing.quote.bid)
        crossed=adapt_option_depth_feed(depth(buyBook={'1':{'price':1500,'qty':1}}),instruments()[1],AT)
        self.assertIn('crossed_book',validate_quote(crossed.quote,contract(),AT).reasons)
        for book in ({'1':{'price':100,'qty':1.5}}, {'1':{'price':100,'qty':1},'2':{'price':100,'qty':2}}):
            with self.subTest(book=book), self.assertRaises(ValueError): adapt_option_depth_feed(depth(buyBook=book),instruments()[1],AT)

    def test_option_ltp_feed_is_not_book(self):
        data={'ltp':{'NSE':{'FNO':{'49445':{'tsInMillis':MS,'ltp':26111.10}}}}}
        result=adapt_option_ltp_feed(data,instruments()[1],AT)
        self.assertEqual(result.fields[0].source_at,AT)
        self.assertIsNone(result.quote.source_at)
        self.assertIsNone(result.quote.bid)
        self.assertIsNone(result.quote.ask)
        self.assertFalse(validate_quote(result.quote,contract(),AT).accepted)

    def chain(self):
        return {'status':'SUCCESS','payload':{'underlying_ltp':25641.7,'strikes':{'27000':{'PE':{
            'trading_symbol':'BANKNIFTY25DEC27000PE','ltp':2.05,'open_interest':0,'volume':9339,
            'greeks':{'delta':-0.0064,'gamma':0,'theta':-1.0787,'vega':0.6943,'rho':-0.0373,'iv':25.3409}}}}}}

    def map_chain(self,response):
        index,put,call=instruments()
        return adapt_option_chain(response,[put,call],AT,underlying_instrument=index,expiry_date=date(2025,12,24))

    def test_chain_matches_catalogue_and_all_greek_freshness_remains_unknown(self):
        result=self.map_chain(self.chain())
        self.assertEqual(len(result.options),1)
        option=result.options[0]
        self.assertEqual(option.quote.open_interest,0)
        self.assertIsNone(result.underlying.source_at)
        self.assertIsNone(option.quote.source_at)
        self.assertTrue(all(f.source_at is None for f in option.fields))
        self.assertEqual(next(f.value for f in option.fields if f.name=='iv'),D('25.3409'))
        self.assertFalse(validate_quote(option.quote,contract(),AT).accepted)

    def test_chain_cannot_invent_unmapped_contract_or_override_strike(self):
        response=self.chain();row=response['payload']['strikes']['27000']['PE']
        row['trading_symbol']='BANKNIFTY25DEC99999PE'
        result=self.map_chain(response)
        self.assertEqual(result.options,())
        self.assertTrue(any('unmapped_chain_contract' in s for s in result.issues))
        response=self.chain();response['payload']['strikes']['27005']=response['payload']['strikes'].pop('27000')
        with self.assertRaises(ValueError): self.map_chain(response)

    def test_json_decimal_exactness_duplicates_and_secrets(self):
        raw=b'{"status":"SUCCESS","payload":{"bid_price":100.00000000000000000001}}'
        self.assertEqual(decode_provider_json(raw)['payload']['bid_price'],D('100.00000000000000000001'))
        for raw in (b'{"status":"SUCCESS","status":"SUCCESS"}',b'{"a":NaN}',b'{"payload":{"api_key":"FAKE_TEST_VALUE"}}'):
            with self.subTest(raw=raw),self.assertRaises(ValueError) as caught: decode_provider_json(raw)
            self.assertNotIn('FAKE_TEST_VALUE',str(caught.exception))

    def test_wrong_envelope_and_invalid_numbers_fail_without_payload_echo(self):
        for bad in ({'status':'FAILURE','payload':{}},{'status':'SUCCESS','payload':[]}, {'status':'SUCCESS','payload':{'bid_price':True}}):
            with self.subTest(bad=bad),self.assertRaises(ValueError): adapt_option_quote(bad,instruments()[1],AT)
        for bad in (-1,True,1.1,float('nan')):
            with self.subTest(bad=bad),self.assertRaises(ValueError): adapt_option_depth_feed(depth(tsInMillis=bad),instruments()[1],AT)

    def test_request_plans_are_inert_get_only_and_readiness_never_claims_connection(self):
        index,put,_=instruments()
        plans=(plan_option_quote(put),plan_index_ltp(index),plan_option_chain(date(2025,12,24)))
        self.assertTrue(all(p.method=='GET' for p in plans))
        self.assertEqual(plans[0].path,'/live-data/quote')
        self.assertFalse(provider_readiness()['authenticated'])
        self.assertFalse(provider_readiness()['connection_attempted'])
        self.assertFalse(provider_readiness()['orders_supported'])

    def test_provider_mapping_has_no_transport_broker_or_process_imports(self):
        source=Path(__file__).resolve().parents[1]/'banknifty'/'provider_adapters.py'
        allowed={'dataclasses','datetime','decimal','hashlib','csv','io','json','typing','options_data','options_pipeline'}
        for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
            if isinstance(node,ast.Import): self.assertTrue(all(n.name.split('.')[0] in allowed for n in node.names))
            if isinstance(node,ast.ImportFrom): self.assertIn(node.module.split('.')[0],allowed)


if __name__=='__main__': unittest.main()
