"""Read-only browser companion bound exclusively to IPv4 loopback.

No account, credentials, external resources, file serving or write endpoints.
Start explicitly; closing the process stops it. This is not a hosted/mobile
service, and does not run while the laptop is off.
"""
from __future__ import annotations
import argparse
from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time


_CSS = '''*{box-sizing:border-box}body{margin:0;background:#f1f4f8;color:#16283b;font:16px system-ui,sans-serif}
header{background:#16283b;color:#fff;padding:24px max(18px,calc((100vw - 1140px)/2))}header small{color:#75ded6;letter-spacing:.08em}
h1{font-size:clamp(24px,4vw,34px);margin:8px 0}header p{max-width:1000px;color:#dce7ed;line-height:1.5}
main{max-width:1180px;margin:auto;padding:18px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
section,.card{background:white;border-radius:8px;padding:20px;margin:0 0 16px;min-width:0}.card span{display:block;font-size:13px;color:#526579;margin-bottom:8px}.card strong{overflow-wrap:anywhere}
h2{font-size:20px;color:#087e83;margin-top:0}.bar{display:flex;gap:15px;align-items:center;flex-wrap:wrap;margin:12px 0}
button{border:1px solid #087e83;border-radius:5px;background:white;color:#075c60;padding:10px 16px;font:inherit;cursor:pointer}
label{font-size:14px}#connection{font-size:14px;color:#526579}.warning{background:#fff1d8;border-left:4px solid #b36b00;padding:12px;margin:8px 0;white-space:pre-wrap}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid #dce5eb;padding:10px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{width:32%;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 ui-monospace,monospace}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}
footer{padding:20px;text-align:center;color:#526579;font-size:13px}@media(max-width:560px){main{padding:10px}section{padding:14px}th{width:40%}.grid{display:block}}'''

_JS = r'''"use strict";
const $=id=>document.getElementById(id);let stopped=false,busy=false,timer=null;
function val(x){if(x===null||x===undefined)return "Unavailable";if(typeof x==="object")return JSON.stringify(x,null,2);return String(x);}
function table(id,data,keys){const box=$(id);box.replaceChildren();if(!data||typeof data!=="object"){box.textContent=val(data);return;}
 const t=document.createElement("table");for(const k of keys){if(!(k in data))continue;const tr=document.createElement("tr"),th=document.createElement("th"),td=document.createElement("td");th.textContent=k.replaceAll("_"," ");td.textContent=val(data[k]);tr.append(th,td);t.append(tr);}box.append(t);}
function warning(text){const p=document.createElement("div");p.className="warning";p.textContent=val(text);$("alerts").append(p);}
function render(s){$("evidence").textContent=val(s.evidence_quality?.label||s.evidence_quality?.source_label||"NO DATA");
 $("pause").textContent=s.paused?"Paused in desktop app":"Research enabled locally";$("live").textContent="Unverified / not connected by companion";
 $("observed").textContent=val(s.as_of);$("alerts").replaceChildren();
 for(const a of (s.alerts||[]))warning(a.message||a); if(!s.alerts?.length)$("alerts").textContent="No current alert reported. This is not a safety guarantee.";
 table("quality",s.evidence_quality,["label","status","source_age_seconds","receipt_age_seconds","accepted","rejected","reasons","warnings"]);
 table("research",s.latest_signal,["label","status","strategy","direction","horizon_minutes","underlying_price","entry_net","quantity","underlying_quality","sizing_reasons","reasons","confidence"]);
 table("replay",s.latest_replay,["label","status","entry_at","exit_at","exit_reason","net_pnl","fees_total","reasons","actual_fills_created"]);
 table("evaluation",s.evaluation,["status","experiment_fingerprint","summary","reasons","validated"]);
 table("archive",s.archive,["status","label","counts","dates","manifest_count","quarantined_manifests","latest_source_at","latest_received_at"]);
 table("worker",s.worker,["label","status","heartbeat_fresh","last_seen_at","last_cutoff","reasons"]);
 $("explanation").textContent=s.explanation?.text||(s.explanation?[s.explanation.notice,...(s.explanation.facts||[]).map(f=>f.text)].filter(Boolean).join("\n\n"):"Run a research analysis and choose Explain latest in the desktop app.");
 $("exposures").replaceChildren();const exposures=Array.isArray(s.exposures)?s.exposures:Object.values(s.exposures||{});
 if(!exposures.length)$("exposures").textContent="No manually reported exposure.";
 for(const e of exposures){const d=document.createElement("pre");d.textContent=val(e);$("exposures").append(d);}
 $("limits").textContent=val(s.limitations||"Live data and predictive calibration remain unverified.");}
async function refresh(){if(busy||stopped)return;busy=true;$("refresh").disabled=true;try{const r=await fetch("/api/status",{cache:"no-store",credentials:"omit"});if(!r.ok)throw Error("Status unavailable");render(await r.json());$("connection").textContent="Local snapshot refreshed at "+new Date().toLocaleTimeString();}catch(e){$("connection").textContent="Status unavailable or companion stopped. Displayed values may be stale.";}finally{busy=false;$("refresh").disabled=false;if(!stopped&&$("auto").checked)timer=setTimeout(refresh,3000);}}
$("refresh").addEventListener("click",()=>{clearTimeout(timer);refresh();});$("auto").addEventListener("change",()=>{clearTimeout(timer);if($("auto").checked)refresh();});
window.addEventListener("pagehide",()=>{stopped=true;clearTimeout(timer);});refresh();'''

HTML = ('''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bank Nifty · Local companion</title><style>''' + _CSS + '''</style></head><body><header><small>BANK NIFTY · READ-ONLY LOCAL COMPANION</small>
<h1>Research and exposure review</h1><p>Displays this laptop's saved research. No orders, credentials or write controls. Synthetic/replay results and manually reported fills remain separate. Stops are targets, not guaranteed loss limits.</p></header>
<main><div class="bar"><button id="refresh" type="button">Refresh saved results</button><label><input id="auto" type="checkbox" checked> Refresh this view every 3 seconds</label><span id="connection" role="status">Loading local snapshot…</span></div>
<div class="cards"><div class="card"><span>Evidence</span><strong id="evidence">NO DATA</strong></div><div class="card"><span>New research entries</span><strong id="pause">Unknown</strong></div><div class="card"><span>Live connection</span><strong id="live">Unverified</strong></div><div class="card"><span>Snapshot time</span><strong id="observed">Unknown</strong></div></div>
<section><h2>Exposure and data alerts</h2><div id="alerts"></div></section><div class="grid"><section><h2>Data quality</h2><div id="quality">No saved data</div></section><section><h2>Research hypothesis</h2><div id="research">No saved signal</div></section><section><h2>Hypothetical replay</h2><div id="replay">No replay</div></section><section><h2>Evaluation</h2><div id="evaluation">No evaluation</div></section></div>
<div class="grid"><section><h2>Saved archive</h2><div id="archive"></div></section><section><h2>Local archive worker</h2><div id="worker"></div></section></div><section><h2>Explanation</h2><pre id="explanation"></pre></section>
<section><h2>User-reported manual exposure</h2><div id="exposures"></div></section><section><h2>Limits and pending checks</h2><pre id="limits"></pre></section></main>
<footer>Loopback only · Use the desktop app for analysis and manual journal changes · No cloud or remote device connection</footer><script>''' + _JS + '''</script></body></html>''').encode('utf-8')


def _default_status(storage_dir):
    from .options_workspace import research_status
    return research_status(storage_dir)


class LocalResearchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, storage_dir, *, port=0, status_reader=None):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('Port must be an integer from 0 through 65535')
        self.storage_dir = Path(storage_dir).resolve()
        self.status_reader = status_reader or _default_status
        self.request_times = deque()
        self.rate_lock = threading.Lock()
        super().__init__(('127.0.0.1', port), CompanionHandler)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_port}/'

    def permitted_rate(self):
        now = time.monotonic()
        with self.rate_lock:
            while self.request_times and now-self.request_times[0] >= 1:
                self.request_times.popleft()
            if len(self.request_times) >= 12:
                return False
            self.request_times.append(now)
            return True


class CompanionHandler(BaseHTTPRequestHandler):
    server_version = 'LocalResearch'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *_args):
        pass  # Do not log local research, URLs or header values.

    def _send(self, status, payload=b'', kind='application/json; charset=utf-8', *, head=False):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        # Fixed inline code is pinned; no external script/resource permission.
        import base64
        script_hash = base64.b64encode(sha256(_JS.encode()).digest()).decode()
        style_hash = base64.b64encode(sha256(_CSS.encode()).digest()).decode()
        self.send_header('Content-Security-Policy', "default-src 'none'; connect-src 'self'; script-src 'sha256-"+script_hash+"'; style-src 'sha256-"+style_hash+"'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header('Connection','close')
        self.end_headers()
        if not head and self.command != 'HEAD':
            self.wfile.write(payload)

    def _allowed(self):
        expected = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        host = self.headers.get('Host')
        origin = self.headers.get('Origin')
        if (self.client_address[0] != '127.0.0.1' or host not in expected
                or self.headers.get('Sec-Fetch-Site') == 'cross-site'
                or (origin is not None and origin != 'http://'+host)):
            self._send(403,b'{"error":"Local same-origin requests only"}')
            return False
        if not self.server.permitted_rate():
            self._send(429,b'{"error":"Refresh rate exceeded"}')
            return False
        return True

    def do_GET(self):
        self._get()

    def do_HEAD(self):
        self._get(head=True)

    def _get(self, *, head=False):
        if not self._allowed():
            return
        if len(self.path) > 256:
            self._send(414,b'{"error":"Path too long"}',head=head)
        elif self.path == '/':
            self._send(200,HTML,'text/html; charset=utf-8',head=head)
        elif self.path == '/health':
            self._send(200,b'{"status":"ok","mode":"local_read_only"}',head=head)
        elif self.path == '/api/status':
            try:
                result = self.server.status_reader(self.server.storage_dir)
                raw = json.dumps(result,ensure_ascii=False,allow_nan=False).encode()
                if len(raw) > 2_000_000:
                    raise ValueError('Snapshot exceeds limit')
            except Exception:
                # Do not expose filesystem paths, raw data or exception text.
                self._send(503,b'{"error":"Saved research unavailable; inspect desktop diagnostics"}',head=head)
            else:
                self._send(200,raw,head=head)
        else:
            self._send(404,b'{"error":"Unknown local endpoint"}',head=head)

    def do_POST(self):
        self._send(405,b'{"error":"Read-only companion; no writes accepted"}')

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST


def main():
    parser = argparse.ArgumentParser(description='Read-only Bank Nifty browser companion; loopback only')
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'data/options-research')
    parser.add_argument('--port',type=int,default=0)
    args = parser.parse_args()
    with LocalResearchServer(args.data_dir,port=args.port) as server:
        print('Read-only local view:',server.url,flush=True)
        print('Stop with Ctrl+C. No remote/mobile network binding or account connection.',flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
