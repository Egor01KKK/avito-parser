"""A self-contained HTML page with the owner-listing table and filters.

    uv run python html_view.py flats-2k-spb-owners
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime
from pathlib import Path

import apartments
import avito

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --bg:#fbfbfa; --fg:#1d1d1b; --muted:#6b6b66; --line:#e4e3de; --card:#fff;
  --accent:#1f5fd1; --owner:#1f7a3d; --check:#9a6700; --agent:#a33; --chip:#f0efea; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ecebe6; --muted:#9d9c95;
  --line:#2e2d2a; --card:#1e1e1c; --accent:#7aa7ff; --owner:#5fc27e; --check:#e0b04a; --agent:#ff7b7b; --chip:#2a2926; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif; }
header { padding:20px 16px 8px; max-width:1400px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; }
.sub { color:var(--muted); }
.controls { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line);
  padding:10px 16px; display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; max-width:1400px; margin:0 auto; }
.controls input[type=search], .controls input[type=number], .controls select { font:inherit; padding:6px 8px;
  border:1px solid var(--line); border-radius:6px; background:var(--card); color:var(--fg); }
.controls input[type=search] { min-width:220px; flex:1; }
.controls input[type=number] { width:110px; }
label.chk { display:inline-flex; gap:5px; align-items:center; white-space:nowrap; }
.count { color:var(--muted); margin-left:auto; }
main { max-width:1400px; margin:0 auto; padding:0 16px 40px; overflow-x:auto; }
table { width:100%; border-collapse:collapse; background:var(--card); }
th, td { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
th { background:var(--card); font-weight:600; cursor:pointer; user-select:none; white-space:nowrap; }
td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
tr.row:hover { background:var(--chip); }
.v { font-weight:600; white-space:nowrap; }
.v.owner { color:var(--owner); } .v.check { color:var(--check); } .v.agent { color:var(--agent); }
.chip { display:inline-block; background:var(--chip); border-radius:10px; padding:1px 8px; margin:1px 4px 1px 0; font-size:12px; white-space:nowrap; }
.why { color:var(--muted); font-size:12px; }
a { color:var(--accent); }
details summary { cursor:pointer; color:var(--accent); font-size:12px; }
details p { white-space:pre-wrap; margin:6px 0 0; max-width:640px; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="sub">Собрано: __STAMP__ · всего в базе: __TOTAL__</div>
</header>
<div class="controls">
  <input type="search" id="q" placeholder="Поиск: улица, метро, слова из описания">
  <select id="verdict">
    <option value="notagent">Собственники и «проверить»</option>
    <option value="собственник">Только собственники</option>
    <option value="проверить">Только «проверить»</option>
    <option value="агент">Отсеянные агенты</option>
    <option value="all">Все</option>
  </select>
  <input type="number" id="pmin" placeholder="Цена от, млн" step="0.5">
  <input type="number" id="pmax" placeholder="до, млн" step="0.5">
  <label class="chk"><input type="checkbox" id="hideNoAgents"> скрыть «агентам не звонить»</label>
  <label class="chk"><input type="checkbox" id="hideOwnAgent"> скрыть «свой агент есть»</label>
  <label class="chk"><input type="checkbox" id="callsOnly"> только со звонками</label>
  <span class="count" id="count"></span>
</div>
<main>
<table>
  <thead><tr>
    <th data-k="verdict">Вердикт</th><th data-k="price">Цена</th><th data-k="ppm">₽/м²</th>
    <th data-k="area">м²</th><th data-k="floor">Этаж</th><th data-k="address">Адрес</th>
    <th data-k="metro">Метро</th><th data-k="ts">Дата</th><th>Пометки</th><th>Объявление</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
</main>
<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = n => n ? n.toLocaleString("ru-RU") : "—";
let sortKey = "ts", sortDir = -1;
function render() {
  const q = $("q").value.trim().toLowerCase();
  const v = $("verdict").value;
  const pmin = parseFloat($("pmin").value) * 1e6 || 0;
  const pmax = parseFloat($("pmax").value) * 1e6 || Infinity;
  const rows = DATA.filter(r =>
    (v === "all" || (v === "notagent" ? r.verdict !== "агент" : r.verdict === v)) &&
    (!r.price || (r.price >= pmin && r.price <= pmax)) &&
    !($("hideNoAgents").checked && r.noAgents) &&
    !($("hideOwnAgent").checked && r.ownAgent) &&
    !($("callsOnly").checked && !r.calls) &&
    (!q || r.search.includes(q))
  ).sort((a, b) => {
    const x = a[sortKey] ?? "", y = b[sortKey] ?? "";
    return (x > y ? 1 : x < y ? -1 : 0) * sortDir;
  });
  $("count").textContent = `Показано: ${rows.length}`;
  $("rows").innerHTML = rows.map(r => `<tr class="row">
    <td><span class="v ${r.vclass}">${esc(r.verdict)}</span><div class="why">${esc(r.reasons)}</div></td>
    <td class="num">${fmt(r.price)}</td><td class="num">${fmt(r.ppm)}</td>
    <td class="num">${r.area ?? "—"}</td><td class="num">${esc(r.floor)}</td>
    <td>${esc(r.address)}</td><td>${esc(r.metro)}</td><td class="num">${esc(r.date)}</td>
    <td>${r.flags.map(f => `<span class="chip">${esc(f)}</span>`).join("")}</td>
    <td><a href="${esc(r.url)}" target="_blank" rel="noopener">открыть</a>
      <details><summary>описание</summary><p>${esc(r.description)}</p></details></td>
  </tr>`).join("");
}
document.querySelectorAll("th[data-k]").forEach(th => th.addEventListener("click", () => {
  const k = th.dataset.k; sortDir = sortKey === k ? -sortDir : (k === "ts" ? -1 : 1); sortKey = k; render();
}));
["q","verdict","pmin","pmax","hideNoAgents","hideOwnAgent","callsOnly"].forEach(id =>
  $(id).addEventListener("input", render));
render();
</script>
</body>
</html>
"""

VERDICT_CLASS = {
    apartments.VERDICT_OWNER: "owner",
    apartments.VERDICT_CHECK: "check",
    apartments.VERDICT_AGENT: "agent",
}


def row(listing: apartments.Listing) -> dict:
    flags = []
    if listing.pays_agent:
        flags.append("готов платить агенту")
    if listing.asks_no_agents:
        flags.append("агентам не звонить")
    if listing.has_own_agent:
        flags.append("свой агент есть")
    if not listing.calls_allowed:
        flags.append("только чат")
    date = ""
    if listing.timestamp_ms:
        date = datetime.fromtimestamp(listing.timestamp_ms / 1000).strftime("%d.%m %H:%M")
    elif listing.published:
        date = listing.published
    address = listing.address.removeprefix("Санкт-Петербург, ")
    return {
        "verdict": listing.verdict,
        "vclass": VERDICT_CLASS.get(listing.verdict, ""),
        "reasons": "; ".join(listing.reasons),
        "price": listing.price,
        "ppm": listing.price_per_meter,
        "area": listing.area,
        "floor": listing.floor,
        "address": address,
        "metro": listing.metro,
        "ts": listing.timestamp_ms or 0,
        "date": date,
        "flags": flags,
        "noAgents": listing.asks_no_agents,
        "ownAgent": listing.has_own_agent,
        "calls": listing.calls_allowed,
        "url": listing.url,
        "description": listing.description,
        "search": f"{address} {listing.metro} {listing.description}".lower(),
    }


def build(name: str) -> Path:
    search = avito.load_search(name)
    base = search.load("listings.json")
    listings = [apartments.classify(apartments.listing_from_item(item)) for item in base.values()]
    data = json.dumps([row(listing) for listing in listings], ensure_ascii=False)
    page = (
        PAGE.replace("__TITLE__", html.escape(search.title))
        .replace("__STAMP__", datetime.now().strftime("%d.%m.%Y %H:%M"))
        .replace("__TOTAL__", str(len(listings)))
        .replace("__DATA__", data.replace("</", "<\\/"))
    )
    path = avito.OUTPUT_DIR / f"{name}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "flats-2k-spb-owners"))
