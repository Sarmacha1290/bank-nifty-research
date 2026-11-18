from pathlib import Path
import socket
import tempfile
import threading
import unittest

from banknifty.browser_companion import LocalResearchServer, HTML


class BrowserCompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        self.snapshot = {'schema_version':1,'paused':True,'evidence_quality':{'label':'SYNTHETIC'},
                         'latest_signal':None,'exposures':[],'limitations':['No live data']}
        def reader(path):
            self.calls.append(path)
            return self.snapshot
        self.server = LocalResearchServer(self.temp.name,status_reader=reader)
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.02},daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self,path='/',method='GET',extra='',host=None):
        host = host or f'127.0.0.1:{self.server.server_port}'
        raw = f'{method} {path} HTTP/1.0\r\nHost: {host}\r\n{extra}Connection: close\r\n\r\n'.encode()
        with socket.create_connection(('127.0.0.1',self.server.server_port),timeout=2) as client:
            client.sendall(raw); pieces=[]
            while piece:=client.recv(65536): pieces.append(piece)
        response=b''.join(pieces)
        headers,body=response.split(b'\r\n\r\n',1)
        return int(headers.split(b' ')[1]),headers,body

    def test_loopback_only_and_fixed_responsive_view(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
        code,headers,body=self.request()
        self.assertEqual(code,200)
        self.assertIn(b'viewport',body)
        self.assertIn(b'read-only',body.lower())
        self.assertIn(b"default-src 'none'",headers)
        self.assertIn(b"connect-src 'self'",headers)
        self.assertIn(b'no-store',headers)
        self.assertNotIn(b'innerHTML',body)
        self.assertNotIn(b'<script src=',body)

    def test_status_snapshot_refreshes_but_does_not_write(self):
        before=list(Path(self.temp.name).iterdir())
        code,_,body=self.request('/api/status')
        self.assertEqual(code,200); self.assertIn(b'SYNTHETIC',body)
        self.snapshot['evidence_quality']['label']='STALE'
        self.assertIn(b'STALE',self.request('/api/status')[2])
        self.assertEqual(before,list(Path(self.temp.name).iterdir()))

    def test_dns_rebinding_cross_origin_and_cross_site_requests_are_rejected(self):
        self.assertEqual(self.request('/api/status',host='evil.invalid')[0],403)
        self.assertEqual(self.request('/api/status',extra='Origin: https://evil.invalid\r\n')[0],403)
        self.assertEqual(self.request('/api/status',extra='Sec-Fetch-Site: cross-site\r\n')[0],403)
        self.assertFalse(self.calls)

    def test_unknown_paths_and_write_methods_never_touch_data(self):
        for path in ['/../private.json','/data/options-ledger.sqlite3','/api/status?file=x']:
            self.assertEqual(self.request(path)[0],404)
        for method in ['POST','PUT','DELETE','PATCH','OPTIONS']:
            self.assertEqual(self.request('/api/status',method=method)[0],405)
        self.assertFalse(self.calls)

    def test_errors_are_sanitized_and_head_omits_body(self):
        self.server.status_reader=lambda _: (_ for _ in ()).throw(ValueError('FAKE_PRIVATE_VALUE'))
        code,_,body=self.request('/api/status')
        self.assertEqual(code,503); self.assertNotIn(b'FAKE_PRIVATE_VALUE',body)
        code,headers,body=self.request('/',method='HEAD')
        self.assertEqual(code,200); self.assertEqual(body,b'')
        self.assertIn(str(len(HTML)).encode(),headers)

    def test_bounded_response_and_refresh_rate(self):
        self.server.status_reader=lambda _: {'too_large':'x'*2_000_001}
        self.assertEqual(self.request('/api/status')[0],503)
        with self.server.rate_lock:
            import time
            self.server.request_times.extend([time.monotonic()]*12)
        self.assertEqual(self.request('/health')[0],429)


if __name__=='__main__': unittest.main()
