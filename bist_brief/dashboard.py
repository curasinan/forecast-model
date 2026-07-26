"""Standalone interactive HTML dashboard. No external dependencies."""
import json, datetime as dt
import numpy as np, pandas as pd

def _f(x, d=2):
    return None if x is None or (isinstance(x,float) and (np.isnan(x) or np.isinf(x))) else round(float(x), d)

def build_html(df, bench, bseries, mode, tz_now):
    recs = []
    for s, r in df.iterrows():
        recs.append({"sym":s,"sector":r.sector,"close":_f(r.close),"r1":_f(r.r1),"r5":_f(r.r5),
            "r20":_f(r.r20),"r60":_f(r.r60),"ytd":_f(r.ytd),"vol20":_f(r.vol20,1),
            "volreg":_f(r.vol_regime),"relvol":_f(r.relvol),"trend":r.trend,"tscore":int(r.tscore),
            "rs20":_f(r.rs20),"rsytd":_f(r.rs_ytd),"sup20":_f(r.sup20),"res20":_f(r.res20),
            "tores":_f(r.to_res20,1),"tosup":_f(r.to_sup20,1),"atr":_f(r.atr_pct,1),
            "tlvol":_f(r.tl_vol/1e6,1),"z":_f(r.zmove)})
    sec = df.groupby("sector").agg(n=("r1","size"), r1=("r1","mean"), r20=("r20","mean"),
        ytd=("ytd","mean"), vol=("vol20","mean"), rel=("relvol","mean")).round(2)
    sec = sec[sec.n >= 2].sort_values("r1", ascending=False)
    secrecs = [{"sector":i,"n":int(r.n),"r1":_f(r.r1),"r20":_f(r.r20),"ytd":_f(r.ytd),
                "vol":_f(r.vol,1),"rel":_f(r.rel)} for i,r in sec.iterrows()]
    spark = [round(float(x),2) for x in bseries.iloc[-60:].tolist()]
    hdr = {"close":_f(bseries.iloc[-1]),"r1":_f(bench[1]),"r5":_f(bench[5]),
           "r20":_f(bench[20]),"ytd":_f(bench["ytd"]),"mode":mode,"ts":tz_now}
    return TEMPLATE.replace("__DATA__", json.dumps(recs, ensure_ascii=False)) \
        .replace("__SECTORS__", json.dumps(secrecs, ensure_ascii=False)) \
        .replace("__HDR__", json.dumps(hdr, ensure_ascii=False)) \
        .replace("__SPARK__", json.dumps(spark))

TEMPLATE = r"""<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BIST 100 Panel</title><style>
*{box-sizing:border-box}body{margin:0;padding:20px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
background:#0e1117;color:#e6e6e6;font-size:14px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 10px;color:#9db2ce;
border-bottom:1px solid #263041;padding-bottom:6px}
.sub{color:#7d8fa6;font-size:12px;margin-bottom:18px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.card{background:#161b24;border:1px solid #263041;border-radius:8px;padding:12px 16px;min-width:120px}
.card .lbl{font-size:11px;color:#7d8fa6;text-transform:uppercase;letter-spacing:.5px}
.card .val{font-size:19px;font-weight:600;margin-top:3px}
.pos{color:#3fb950}.neg{color:#f85149}.neu{color:#c9d1d9}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
select,input{background:#161b24;color:#e6e6e6;border:1px solid #30363d;border-radius:6px;padding:7px 10px;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:right;padding:8px 7px;background:#161b24;color:#9db2ce;cursor:pointer;
position:sticky;top:0;white-space:nowrap;border-bottom:1px solid #30363d;user-select:none}
th:first-child,td:first-child{text-align:left}th:nth-child(2),td:nth-child(2){text-align:left}
th:hover{color:#fff}td{padding:6px 7px;text-align:right;border-bottom:1px solid #1c2230;white-space:nowrap}
tr:hover td{background:#161b24}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:10.5px;font-weight:600}
.t-up{background:#12331d;color:#3fb950}.t-sup{background:#0d4020;color:#56d364}
.t-dn{background:#3d1418;color:#f85149}.t-fl{background:#2b2f3a;color:#a0aec0}
.flag{background:#161b24;border:1px solid #263041;border-radius:8px;padding:12px 14px;margin-bottom:10px}
.flag h3{margin:0 0 8px;font-size:13px;color:#e3b341}
.chip{display:inline-block;background:#1c2230;border:1px solid #30363d;border-radius:6px;
padding:4px 9px;margin:3px 4px 3px 0;font-size:12px}
.chip b{color:#fff}.empty{color:#6e7681;font-size:12px;font-style:italic}
.bar{height:7px;border-radius:3px;display:inline-block;vertical-align:middle}
footer{margin-top:30px;color:#6e7681;font-size:11px;border-top:1px solid #263041;padding-top:12px}
</style></head><body>
<h1>📈 BIST 100 Panel</h1><div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<svg id="spark" width="100%" height="60" viewBox="0 0 600 60" preserveAspectRatio="none"></svg>
<h2>Tarama Sinyalleri</h2><div id="flags"></div>
<h2>Sektör Görünümü</h2><div id="sectors"></div>
<h2>Tüm Hisseler <span style="font-weight:400;color:#6e7681;font-size:12px">(başlığa tıklayarak sırala)</span></h2>
<div class="controls">
<input id="q" placeholder="Hisse ara..." style="min-width:150px">
<select id="fsec"><option value="">Tüm sektörler</option></select>
<select id="ftr"><option value="">Tüm trendler</option><option>Güçlü yükseliş</option>
<option>Yükseliş</option><option>Yatay/Sıkışma</option><option>Düşüş</option></select>
<select id="fsp"><option value="">Filtre yok</option><option value="relvol">Hacim &gt; 1.5x</option>
<option value="volexp">Volatilite genişleyen</option><option value="rs">RS &gt; 0</option></select>
</div>
<div style="overflow-x:auto"><table id="tbl"><thead><tr>
<th data-k="sym">Hisse</th><th data-k="sector">Sektör</th><th data-k="close">Kapanış</th>
<th data-k="r1">1G%</th><th data-k="r5">5G%</th><th data-k="r20">20G%</th><th data-k="ytd">YBB%</th>
<th data-k="rs20">RS20</th><th data-k="vol20">Vol%</th><th data-k="volreg">VolRej</th>
<th data-k="relvol">BgHcm</th><th data-k="tlvol">Hcm(mnTL)</th><th data-k="trend">Trend</th>
<th data-k="sup20">Dest20</th><th data-k="res20">Dir20</th><th data-k="atr">ATR%</th>
</tr></thead><tbody id="tb"></tbody></table></div>
<footer>Kaynak: Yahoo Finance EOD (yfinance). Vol% = 20 günlük yıllıklandırılmış gerçekleşen volatilite.
VolRej = 20g vol / 60g vol. BgHcm = son seans hacmi / 20 gün ortalaması. RS20 = 20 günlük XU100'e göre
aşırı getiri (puan). Dest/Dir20 = son 20 seansın en düşük/en yüksek seviyesi.
<b>Yatırım tavsiyesi değildir.</b></footer>
<script>
const D=__DATA__, S=__SECTORS__, H=__HDR__, SP=__SPARK__;
const f=(v,d=2)=>v==null?'-':v.toFixed(d);
const cl=v=>v==null?'neu':v>0?'pos':v<0?'neg':'neu';
const sg=(v,d=2)=>v==null?'<span class="neu">-</span>':`<span class="${cl(v)}">${v>0?'+':''}${v.toFixed(d)}</span>`;
const TC={'Güçlü yükseliş':'t-sup','Yükseliş':'t-up','Düşüş':'t-dn','Yatay/Sıkışma':'t-fl'};
document.getElementById('sub').textContent=
 (H.mode==='preopen'?'Açılış öncesi görünüm':'Kapanış sonrası değerlendirme')+' · '+H.ts+' · 100 hisse';
document.getElementById('cards').innerHTML=[
 ['XU100',f(H.close),null],['1 Gün',H.r1,1],['5 Gün',H.r5,1],['20 Gün',H.r20,1],['YBB',H.ytd,1]
].map(([l,v,d])=>`<div class="card"><div class="lbl">${l}</div><div class="val ${d==null?'neu':cl(v)}">${
 d==null?v:(v>0?'+':'')+v.toFixed(d)+'%'}</div></div>`).join('');
(()=>{const mn=Math.min(...SP),mx=Math.max(...SP),r=mx-mn||1;
 const pts=SP.map((v,i)=>`${i/(SP.length-1)*600},${55-(v-mn)/r*50}`).join(' ');
 document.getElementById('spark').innerHTML=
  `<polyline points="${pts}" fill="none" stroke="${SP[SP.length-1]>=SP[0]?'#3fb950':'#f85149'}" stroke-width="1.8"/>`;})();
const F=[
 ['🔊 Hacim patlaması (≥2x)', D.filter(d=>d.relvol>=2).sort((a,b)=>b.relvol-a.relvol), d=>`${d.relvol.toFixed(1)}x`],
 ['⚡ Volatilite genişlemesi', D.filter(d=>d.volreg>=1.4&&d.vol20>=30).sort((a,b)=>b.volreg-a.volreg), d=>`${d.vol20.toFixed(0)}% vol, rejim ${d.volreg.toFixed(2)}`],
 ['🚀 20g direnç kırılımı', D.filter(d=>d.close>=d.res20&&d.r1>0).sort((a,b)=>b.r1-a.r1), d=>`+${d.r1.toFixed(1)}%`],
 ['🔻 20g destek kaybı', D.filter(d=>d.close<=d.sup20&&d.r1<0).sort((a,b)=>a.r1-b.r1), d=>`${d.r1.toFixed(1)}%`],
 ['📊 Aşırı hareket (|z|≥2.5)', D.filter(d=>Math.abs(d.z)>=2.5).sort((a,b)=>Math.abs(b.z)-Math.abs(a.z)), d=>`${d.r1>0?'+':''}${d.r1.toFixed(1)}% (z=${d.z.toFixed(1)})`],
 ['💪 Güçlü relatif güç', D.filter(d=>d.rs20>=5&&d.tscore>=3).sort((a,b)=>b.rs20-a.rs20), d=>`RS +${d.rs20.toFixed(1)}`],
 ['🩸 Zayıf relatif güç', D.filter(d=>d.rs20<=-5&&d.tscore<=1).sort((a,b)=>a.rs20-b.rs20), d=>`RS ${d.rs20.toFixed(1)}`],
];
document.getElementById('flags').innerHTML=F.map(([t,a,fm])=>
 `<div class="flag"><h3>${t} <span style="color:#6e7681">(${a.length})</span></h3>${
  a.length?a.slice(0,14).map(d=>`<span class="chip"><b>${d.sym}</b> ${fm(d)}</span>`).join(''):
  '<span class="empty">sinyal yok</span>'}</div>`).join('');
document.getElementById('sectors').innerHTML='<div style="overflow-x:auto"><table><thead><tr>'+
 '<th>Sektör</th><th>Adet</th><th>1G%</th><th>20G%</th><th>YBB%</th><th>Vol%</th><th>BgHcm</th></tr></thead><tbody>'+
 S.map(s=>{const w=Math.min(Math.abs(s.r1)*22,90);return `<tr><td>${s.sector}</td><td>${s.n}</td>`+
 `<td>${sg(s.r1)} <span class="bar" style="width:${w}px;background:${s.r1>=0?'#3fb950':'#f85149'};opacity:.5"></span></td>`+
 `<td>${sg(s.r20)}</td><td>${sg(s.ytd)}</td><td>${f(s.vol,1)}</td><td>${f(s.rel)}</td></tr>`}).join('')+
 '</tbody></table></div>';
[...new Set(D.map(d=>d.sector))].sort().forEach(s=>
 document.getElementById('fsec').insertAdjacentHTML('beforeend',`<option>${s}</option>`));
let sk='r1', sa=false;
function render(){
 const q=document.getElementById('q').value.toUpperCase(),
  fs=document.getElementById('fsec').value, ft=document.getElementById('ftr').value,
  fp=document.getElementById('fsp').value;
 let r=D.filter(d=>(!q||d.sym.includes(q))&&(!fs||d.sector===fs)&&(!ft||d.trend===ft)&&
  (!fp||(fp==='relvol'?d.relvol>=1.5:fp==='volexp'?d.volreg>=1.2:d.rs20>0)));
 r.sort((a,b)=>{const x=a[sk],y=b[sk];
  if(typeof x==='string')return sa?x.localeCompare(y,'tr'):y.localeCompare(x,'tr');
  return sa?(x??-1e9)-(y??-1e9):(y??-1e9)-(x??-1e9);});
 document.getElementById('tb').innerHTML=r.map(d=>`<tr><td><b>${d.sym}</b></td><td style="color:#8b98a9">${d.sector}</td>
 <td>${f(d.close)}</td><td>${sg(d.r1)}</td><td>${sg(d.r5)}</td><td>${sg(d.r20)}</td><td>${sg(d.ytd,1)}</td>
 <td>${sg(d.rs20,1)}</td><td>${f(d.vol20,1)}</td><td>${f(d.volreg)}</td>
 <td${d.relvol>=2?' style="color:#e3b341;font-weight:600"':''}>${f(d.relvol)}</td><td>${f(d.tlvol,1)}</td>
 <td><span class="tag ${TC[d.trend]||'t-fl'}">${d.trend}</span></td>
 <td>${f(d.sup20)}</td><td>${f(d.res20)}</td><td>${f(d.atr,1)}</td></tr>`).join('');
}
document.querySelectorAll('th[data-k]').forEach(th=>th.onclick=()=>{
 const k=th.dataset.k; sa = sk===k ? !sa : false; sk=k; render();});
['q','fsec','ftr','fsp'].forEach(i=>document.getElementById(i).addEventListener('input',render));
render();
</script></body></html>"""
