"""Generate a self-contained, read-only visual map of the actual project state."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .equity_history import count_equity_ticks
from .futures_history import load_future_snapshots
from .groww import IST
from .option_history import count_option_chain_snapshots
from .tick_history import count_index_ticks
from .rest_sampler import summarize_rest_observations


EXPECTED_ROLES = (
    "Data Quality", "Technical", "Fundamentals", "Index Contribution",
    "Options & Futures", "Institutional Flows", "News", "Macro", "Risk",
    "Bull", "Bear", "Verification", "Evaluation", "Master",
)

PHASES = (
    ("0", "Scope & rules", "Defined", "Targets, source boundaries, 14 role contracts and no-order rule are documented.",
     "Keep requirements and source permissions current."),
    ("1", "Collect & store", "Collector prototype", "Read-only collection supports authorized local inputs; neither archive nor live validation is included.",
     "Review feed quality using sources you are authorized to access."),
    ("2", "Measurements", "Partial prototype", "Candle, price, bank, option and futures calculations have offline implementations; three source roles remain planned.",
     "Verify real fields and timing; obtain interval-valid weights and authenticated events."),
    ("3", "Research app", "Offline prototype", "Windows screens, role workbench, paper log, safeguards and review exports exist.",
     "Review a complete real session and refine operational screens."),
    ("4", "Train & evaluate", "Blocked by data", "Research-row and evaluation code exists; no real intraday training rows or trained forecast model exist.",
     "Collect verified sessions; then use chronological holdouts and forward paper results."),
    ("5", "Release & operate", "Not released", "Manual integrity-checked backups exist; public forecasts and trading remain disabled.",
     "Set evidence-based release criteria only after out-of-sample performance is measured."),
)

FLOW = (
    ("Sources", "Groww REST receipt-time prices and separate socket-feed index, banks, options and futures; NSE daily data"),
    ("Raw archive", "Timestamped local SQLite records and source labels"),
    ("Quality gate", "Reject weak, late, missing or misaligned observations"),
    ("Specialists", "Technical, bank, derivatives and context roles; Bull and Bear cases"),
    ("Safety gates", "Verification and Risk check frozen cutoffs and availability"),
    ("Master", "Currently abstains; no trained forecast is released"),
    ("Evaluation", "Later outcomes, whole-date holdouts, baselines and paper review"),
)

ROLE_GROUPS = (
    ("1 · Quality", ("Data Quality",)),
    ("2 · Market specialists", ("Technical", "Fundamentals", "Index Contribution",
                               "Options & Futures", "Institutional Flows", "News", "Macro")),
    ("3 · Opposing cases", ("Bull", "Bear")),
    ("4 · Safety gates", ("Verification", "Risk")),
    ("5 · Decision & learning", ("Master", "Evaluation")),
)


def _plain_markdown(value):
    value = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value)
    return value.replace("**", "").replace("`", "").strip()


def _read_roles(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or line.startswith("| Role ") or line.startswith("| ---"):
            continue
        cells = [item.strip() for item in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0] in EXPECTED_ROLES:
            rows.append({"name": cells[0], "input": _plain_markdown(cells[1]),
                         "output": _plain_markdown(cells[2]),
                         "state": _plain_markdown(cells[3])})
    if tuple(row["name"] for row in rows) != EXPECTED_ROLES:
        raise ValueError("Agent role map does not match all 14 reviewed roles")
    return rows


def _count_or_unavailable(operation):
    try:
        return operation()
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return None


def build_project_map_data(project_dir):
    project = Path(project_dir)
    metadata = json.loads((project / "project.json").read_text(encoding="utf-8"))
    if not isinstance(metadata.get("version"), str):
        raise ValueError("Project version is unavailable")
    database = project / "data" / "market-data.sqlite3"
    roles = _read_roles(project / "AGENT_ROLES.md")
    if sorted(role for _, names in ROLE_GROUPS for role in names) != sorted(EXPECTED_ROLES):
        raise ValueError("Visual role topology does not cover all 14 roles once")
    return {
        "version": metadata["version"],
        "generated_at_ist": datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST"),
        "roles": roles,
        "role_groups": [{"name": name, "roles": names} for name, names in ROLE_GROUPS],
        "prototype_roles": sum(row["state"] == "Prototype" for row in roles),
        "planned_roles": sum(row["state"].startswith("Planned") for row in roles),
        "forecast_models_implemented": metadata.get("forecast_models_implemented") is True,
        "archive": {
            "index_updates": _count_or_unavailable(lambda: count_index_ticks(database)),
            "rest_price_samples": _count_or_unavailable(lambda: summarize_rest_observations(database)["count"]),
            "bank_price_updates": _count_or_unavailable(lambda: count_equity_ticks(database)),
            "option_snapshots": _count_or_unavailable(lambda: count_option_chain_snapshots(database)),
            "futures_quotes": _count_or_unavailable(lambda: len(load_future_snapshots(database))),
        },
        "phases": [{"number": number, "name": name, "state": state,
                    "exists": exists, "next": next_step}
                   for number, name, state, exists, next_step in PHASES],
        "flow": [{"name": name, "detail": detail} for name, detail in FLOW],
    }


def generate_project_map(project_dir, destination=None):
    project = Path(project_dir).resolve()
    target = Path(destination).resolve() if destination else project / "project-map.html"
    data = build_project_map_data(project)
    serialized = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    html = HTML_TEMPLATE.replace("__PROJECT_DATA__", serialized)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                prefix=".project-map-", suffix=".html", dir=target.parent,
                delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(html)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


HTML_TEMPLATE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bank Nifty · Project map</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141d32;--ink:#edf4ff;--muted:#a8b8d0;--line:#3e5274;--cyan:#50d7d0;--blue:#7da7ff;--amber:#f7c465;--gray:#a4afc4}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0%,#17284c 0,#0b1020 42%);color:var(--ink);font:16px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:1680px;margin:auto;padding:26px 28px 54px}h1{margin:0;font-size:clamp(27px,3vw,42px);letter-spacing:-.03em}h2{font-size:21px;margin:36px 0 14px}p{margin:5px 0}.sub{color:var(--muted);margin-top:8px}.top{display:flex;gap:18px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}.stamp{color:var(--muted);font-size:13px;text-align:right}.banner{margin:20px 0;padding:14px 18px;border-left:4px solid var(--amber);background:#282337;border-radius:8px;color:#ffe8ba}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.metric{background:var(--panel);border:1px solid #31425f;border-radius:12px;padding:14px}.metric strong{display:block;font-size:28px;color:var(--cyan)}.metric span{color:var(--muted);font-size:13px}.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:13px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;background:var(--amber)}.dot.defined{background:var(--cyan)}.dot.planned{background:var(--gray)}
.phase-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:14px}.phase,.flow-node,.role{font:inherit;color:var(--ink);text-align:left;background:var(--panel);border:1px solid #334767;border-radius:12px;padding:15px;min-width:0;cursor:pointer}.phase:hover,.flow-node:hover,.role:hover,.selected{border-color:var(--cyan);background:#1b3046}.phase{position:relative;min-height:155px}.phase:not(:last-child):after{content:'→';position:absolute;right:-15px;top:40%;z-index:2;color:var(--cyan);font-weight:700}.phase small,.role small{display:block;color:var(--muted)}.phase b{display:block;font-size:16px;margin:8px 0}.phase .status{font-size:12px;color:var(--amber)}.phase:first-child .status{color:var(--cyan)}
.flow{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:13px}.flow-node{min-height:96px;position:relative;font-weight:650}.flow-node:not(:last-child):after{content:'→';position:absolute;right:-14px;top:39%;z-index:2;color:var(--cyan)}.flow-node small{display:block;margin-top:6px;font-weight:400;color:var(--muted)}
.role-heading{display:flex;justify-content:space-between;align-items:end;gap:12px;flex-wrap:wrap}.role-groups{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:15px}.role-group{position:relative;border:1px solid #2c3f5e;border-radius:12px;padding:10px;background:#10192b}.role-group:not(:last-child):after{content:'→';position:absolute;right:-15px;top:28px;z-index:2;color:var(--cyan)}.role-group h3{font-size:13px;color:var(--blue);margin:2px 3px 12px}.role-list{display:grid;gap:7px}.role{width:100%;min-height:62px;padding:9px 11px}.role b{display:block;font-size:13px}.role small{font-size:11px;margin-top:4px}.role.planned{border-style:dashed}.detail{margin-top:18px;padding:18px 22px;border:1px solid var(--line);border-radius:12px;background:#111b2f;min-height:154px}.detail h3{margin:0 0 8px;font-size:19px}.detail p{color:var(--muted);margin:5px 0}.detail strong{color:var(--ink)}.foot{color:var(--muted);font-size:13px;margin-top:26px}button:focus-visible{outline:3px solid var(--cyan);outline-offset:2px}
@media(max-width:1100px){.phase-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.phase:after,.flow-node:after,.role-group:after{display:none}.flow{grid-template-columns:repeat(4,minmax(0,1fr))}.role-groups{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:650px){main{padding:18px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.phase-grid,.flow,.role-groups{grid-template-columns:repeat(2,minmax(0,1fr))}.phase{min-height:126px}}
@media(max-width:380px){.flow,.role-groups{grid-template-columns:1fr}}
</style></head><body><main>
<header class="top"><div><h1>Bank Nifty project map</h1><p class="sub">What is built, how the 14 roles connect, and what evidence is still missing</p></div><p class="stamp" id="stamp"></p></header>
<div class="banner" id="truth"></div>
<section aria-label="Recorded market data"><div class="metrics" id="metrics"></div></section>
<section><h2>Build phases</h2><div class="phase-grid" id="phases"></div></section>
<section><h2>How one assessment would flow</h2><div class="flow" id="flow"></div></section>
<section><div class="role-heading"><h2>Fourteen software roles</h2><div class="legend"><span><i class="dot"></i>Offline prototype</span><span><i class="dot planned"></i>Planned source engine</span></div></div><div class="role-groups" id="roles"></div></section>
<section class="detail" id="detail" aria-live="polite"><h3>Select a phase or role</h3><p>Click any box to see its input, output and next gate.</p></section>
<p class="foot">This map is generated from the local project version, role contracts and archive counts when opened from the desktop app. A prototype is working offline code, not proven predictive skill. No automatic trading is implemented.</p>
</main><script>
const DATA=__PROJECT_DATA__;
const $=id=>document.getElementById(id);
function el(tag,klass,text){const node=document.createElement(tag);if(klass)node.className=klass;if(text!==undefined)node.textContent=text;return node}
function show(title,lines,source){document.querySelectorAll('.selected').forEach(n=>n.classList.remove('selected'));if(source)source.classList.add('selected');const box=$('detail');box.replaceChildren(el('h3','',title));lines.forEach(([label,value])=>{const p=el('p');p.append(el('strong','',label+' '),document.createTextNode(value));box.append(p)})}
$('stamp').textContent='Version '+DATA.version+' · refreshed '+DATA.generated_at_ist;
$('truth').textContent=DATA.forecast_models_implemented?'Forecast code exists, but live validation and release still require evidence.':DATA.prototype_roles+' offline prototypes · '+DATA.planned_roles+' planned roles · 0 trained forecast models · public forecasts and trading disabled';
const labels=[['index_updates','Exchange-timestamped Bank Nifty index updates'],['rest_price_samples','REST receipt-time Bank Nifty price samples (not verified ticks)'],['bank_price_updates','Bank equity price updates'],['option_snapshots','Option-chain snapshots'],['futures_quotes','Futures quotes']];
labels.forEach(([key,label])=>{const card=el('div','metric');card.append(el('strong','',DATA.archive[key]===null?'Unavailable':DATA.archive[key].toLocaleString()),el('span','',label));$('metrics').append(card)});
DATA.phases.forEach(phase=>{const node=el('button','phase');node.type='button';node.append(el('small','','PHASE '+phase.number),el('b','',phase.name),el('span','status',phase.state));node.onclick=()=>show('Phase '+phase.number+' · '+phase.name,[['State:',phase.state],['Built:',phase.exists],['Next gate:',phase.next]],node);$('phases').append(node)});
DATA.flow.forEach((step,i)=>{const node=el('button','flow-node');node.type='button';node.append(document.createTextNode((i+1)+'. '+step.name),el('small','',step.detail));node.onclick=()=>show(step.name,[['Purpose:',step.detail],['Rule:','Later information cannot enter an earlier decision. Missing evidence stays missing.']],node);$('flow').append(node)});
const byRole=Object.fromEntries(DATA.roles.map(role=>[role.name,role]));
DATA.role_groups.forEach(group=>{const column=el('div','role-group');column.append(el('h3','',group.name));const list=el('div','role-list');group.roles.forEach(name=>{const role=byRole[name];const node=el('button','role'+(role.state.startsWith('Planned')?' planned':''));node.type='button';node.append(el('b','',role.name),el('small','',role.state.startsWith('Planned')?'Planned source engine':'Offline prototype'));node.onclick=()=>show(role.name,[['Current state:',role.state],['Input and responsibility:',role.input],['Output and release gate:',role.output]],node);list.append(node)});column.append(list);$('roles').append(column)});
</script></body></html>'''
