from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from banknifty.agents.risk import check_options_research
from banknifty.options_report import write_report
from banknifty.options_strategy import generate_signal
from banknifty.options_workspace import synthetic_market
from dashboard import Dashboard


class OptionsApplicationTests(unittest.TestCase):
    def test_legacy_connection_timer_is_cancelled_on_window_destruction(self):
        from connect_groww import ConnectionWindow
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk(); root.withdraw()
            try:
                child = tk.Toplevel(root); child.withdraw()
                controller = ConnectionWindow(child, temp)
                pending = controller._poll_id
                self.assertIn(pending, root.tk.call('after','info'))
                child.destroy()
                root.update()
                self.assertTrue(controller._closed)
                self.assertNotIn(pending, root.tk.call('after','info'))
                controller.shutdown()
            finally:
                root.destroy()

    def test_integrated_offline_dashboard_preserves_tabs_and_blocks_connections(self):
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk(); root.withdraw()
            try:
                with patch('dashboard.ConnectionWindow',side_effect=AssertionError('No broker window')), \
                     patch('dashboard.sync_recent_official_days',side_effect=AssertionError('No request')), \
                     patch('dashboard.sync_older_official_days',side_effect=AssertionError('No request')):
                    app = Dashboard(root,temp,offline=True)
                    names = [app.tabs.tab(tab,'text') for tab in app.tabs.tabs()]
                    self.assertIn('Options Research',names)
                    self.assertIn('Saved history',names)
                    for _ in range(2):
                        app.connect(); app.fetch_recent_official_range(); app.backfill_official_range()
                    root.update_idletasks()
                    self.assertIsNone(app.connection_window)
                    self.assertIn('no connection',app.notice.get())
            finally:
                root.destroy()

    def test_options_gate_cannot_authorize_forecast_or_execution(self):
        market = synthetic_market(datetime(2026,1,5,5,tzinfo=timezone.utc))
        signal = generate_signal(market.contracts,market.quotes,as_of=market.decision_at,
            underlying_price=Decimal('50000'),direction='BULLISH',strategy='LONG_OPTION')
        risk = check_options_research(signal)
        self.assertTrue(risk.research_visible)
        self.assertFalse(risk.public_forecast_allowed)
        self.assertFalse(risk.live_signal_available)
        self.assertFalse(risk.trading_allowed)

    def test_local_report_escapes_content_excludes_raw_and_has_no_remote_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)/'report.html'
            write_report(target,'<img src=x onerror=alert(1)>',{'Review':{
                'label':'SYNTHETIC','note':'</pre><script>alert(1)</script>',
                'provenance':{'payload':'FAKE_SECRET'},'access_token':'FAKE_SECRET'}})
            source = target.read_text(encoding='utf-8')
            self.assertNotIn('<script>',source)
            self.assertNotIn('FAKE_SECRET',source)
            self.assertIn('SYNTHETIC',source)
            self.assertIn('viewport',source)
            self.assertIn('does not refresh',source)

    def test_html_retains_manual_fill_correction_and_replay_assumptions(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)/'audit.html'
            write_report(target,'Audit',{'Manual audit':[
                {'event_id':'review-fill','payload':{'price':'102.35','fees':'1.25','quantity':10,
                    'access_token':'FAKE_PRIVATE','provenance':{'payload':'RAW_PRIVATE'}}},
                {'event_id':'correction','payload':{'target_event_id':'review-fill',
                    'replacement':{'price':'103.05','fees':'1.50'}}}],
                'Replay':{'manual_latency_seconds':5,'slippage_per_leg':'0.05',
                    'entries':[{'quote_fingerprint':'abc123','source_at':'2026-01-05T10:00:00+05:30'}]}})
            raw = target.read_text(encoding='utf-8')
            for value in ['102.35','1.25','103.05','1.50','manual_latency_seconds','slippage_per_leg','abc123']:
                self.assertIn(value,raw)
            self.assertNotIn('FAKE_PRIVATE',raw)
            self.assertNotIn('RAW_PRIVATE',raw)


if __name__ == '__main__': unittest.main()
