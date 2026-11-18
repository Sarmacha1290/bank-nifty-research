"""Responsive, self-contained local reports. Nothing is served or uploaded."""
from datetime import date, datetime
from decimal import Decimal
from html import escape
import json
from pathlib import Path


def _clean(value):
    if hasattr(value, 'to_dict'):
        return _clean(value.to_dict())
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()
                if str(key).lower().replace('-', '_') not in {'provenance','access_token','token',
                    'api_key','api_secret','authorization','password','raw','raw_payload',
                    'credentials','private_key','secret'}}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return '[raw bytes excluded]'
    return value


def write_report(path, title, sections):
    """Write a browser-readable snapshot, with unverified source labels intact."""
    if not isinstance(sections, dict) or len(sections) > 30:
        raise ValueError('Expected a bounded report section mapping')
    cards = []
    for name, values in sections.items():
        body = json.dumps(_clean(values), ensure_ascii=False, indent=2, allow_nan=False)
        if len(body) > 2_000_000:
            raise ValueError('Report section exceeds limit')
        cards.append('<section><h2>' + escape(str(name)) + '</h2><pre>' + escape(body) + '</pre></section>')
    content = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>''' + escape(str(title)) + '''</title><style>
*{box-sizing:border-box}body{margin:0;background:#f1f4f8;color:#16283b;font:16px system-ui,sans-serif}
header{background:#16283b;color:white;padding:28px max(20px,calc((100vw - 1100px)/2))}
h1{font-size:clamp(24px,4vw,34px);margin:8px 0}header p{max-width:900px;line-height:1.5;color:#dce7ed}
main{max-width:1140px;margin:auto;padding:20px}section{background:white;border-radius:8px;padding:20px;margin-bottom:18px}
h2{font-size:20px;color:#087e83}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace}
.label{color:#75ded6;font-weight:bold;letter-spacing:.07em}footer{padding:24px;color:#526579}
</style></head><body><header><div class="label">BANK NIFTY · LOCAL RESEARCH SNAPSHOT</div><h1>''' + escape(str(title)) + '''</h1>
<p>Unvalidated research. Synthetic, reconstructed replay and user-reported manual records remain separate.
No live verification, calibrated confidence or automated execution. Stops are targets, not guaranteed caps.
This saved report does not refresh. Check source and receipt times in the application.</p></header><main>''' + ''.join(cards) + '''</main>
<footer>Local file export · No external scripts, tracking, remote resources or server.</footer></body></html>'''
    target = Path(path)
    if target.suffix.lower() != '.html':
        raise ValueError('Choose an HTML report destination')
    target.write_text(content, encoding='utf-8')
    return target
