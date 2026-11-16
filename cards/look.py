"""Look and feel: one stylesheet (system font, quiet greys, white panels, a single blue accent, real dark mode), the
card-face drawing used everywhere a card appears, and the tiny script that re-asks "Which card?" when a choice changes."""
import hashlib
import html

e = lambda v: html.escape("" if v is None else str(v))

CSS = r"""
:root{
  --bg:#f5f5f7;--panel:#fff;--fg:#1d1d1f;--muted:#6e6e73;--faint:#86868b;--line:rgba(0,0,0,.08);
  --accent:#0071e3;--accent-fg:#fff;--ok:#1d8a3a;--warn:#b25000;--bad:#c9302c;--chip:rgba(0,0,0,.05);--seg:rgba(118,118,128,.12);
  --seg-on:#fff;--shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.06);--hdr:rgba(245,245,247,.8);
  --ok-bg:rgba(29,138,58,.09);--warn-bg:rgba(178,80,0,.09);--sat:env(safe-area-inset-top,0px);--sab:env(safe-area-inset-bottom,0px)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#000;--panel:#1c1c1e;--fg:#f5f5f7;--muted:#a1a1a6;--faint:#8e8e93;--line:rgba(255,255,255,.1);
  --accent:#2997ff;--ok:#30d158;--warn:#ff9f0a;--bad:#ff453a;--chip:rgba(255,255,255,.08);--seg:rgba(118,118,128,.24);--seg-on:#636366;
  --shadow:0 0 0 1px rgba(255,255,255,.06);--hdr:rgba(22,22,23,.8);--ok-bg:rgba(48,209,88,.13);--warn-bg:rgba(255,159,10,.13)}}
:root[data-theme=dark]{
  --bg:#000;--panel:#1c1c1e;--fg:#f5f5f7;--muted:#a1a1a6;--faint:#8e8e93;--line:rgba(255,255,255,.1);
  --accent:#2997ff;--ok:#30d158;--warn:#ff9f0a;--bad:#ff453a;--chip:rgba(255,255,255,.08);--seg:rgba(118,118,128,.24);--seg-on:#636366;
  --shadow:0 0 0 1px rgba(255,255,255,.06);--hdr:rgba(22,22,23,.8);--ok-bg:rgba(48,209,88,.13);--warn-bg:rgba(255,159,10,.13)}
:root[data-theme=dark]{color-scheme:dark}:root[data-theme=light]{color-scheme:light}
@view-transition{navigation:auto}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.47 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Helvetica Neue",system-ui,sans-serif;
  letter-spacing:-.022em;-webkit-font-smoothing:antialiased;overflow-x:hidden}
a{color:var(--accent);text-decoration:none}
@media (hover:hover){a:hover{text-decoration:underline}a.row:hover,.tabs a:hover,.sub a:hover{text-decoration:none}a.row:hover{background:var(--chip)}}
header{position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;padding:var(--sat) 22px 0;
  height:calc(52px + var(--sat));background:var(--hdr);-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);
  border-bottom:1px solid var(--line)}
header .brand{font-weight:600;font-size:19px;color:var(--fg);letter-spacing:-.03em}
header .who{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px;min-height:44px}
header .who svg{width:20px;height:20px}header .right{display:flex;align-items:center;gap:4px}header .theme{background:none;border:0;padding:0;width:44px;height:44px;display:flex;align-items:center;justify-content:center;color:var(--muted);cursor:pointer;border-radius:50%}header .theme svg{width:21px;height:21px}header .theme .sun{display:none}:root[data-theme=dark] header .theme .sun{display:block}:root[data-theme=dark] header .theme .moon{display:none}@media (prefers-color-scheme:dark){:root:not([data-theme=light]) header .theme .sun{display:block}:root:not([data-theme=light]) header .theme .moon{display:none}}
.tabs{position:fixed;top:var(--sat);left:0;right:0;z-index:11;height:52px;display:flex;justify-content:center;gap:4px;align-items:center;pointer-events:none}
.tabs a{pointer-events:auto;display:flex;align-items:center;gap:7px;color:var(--muted);font-size:14px;font-weight:500;padding:7px 13px;border-radius:999px}
.tabs a svg{width:18px;height:18px;flex:none}.tabs a.on{color:var(--fg);background:var(--seg)}
main{max-width:980px;margin:0 auto;padding:28px 22px 80px}
.sub{display:flex;gap:6px;overflow-x:auto;margin:0 -22px 18px;padding:2px 22px;scrollbar-width:none}.sub::-webkit-scrollbar{display:none}
.sub a{flex:none;font-size:14px;font-weight:500;color:var(--muted);padding:7px 14px;border-radius:999px;background:var(--chip);white-space:nowrap}
.sub a.on{background:var(--fg);color:var(--bg)}
h1{font-size:40px;line-height:1.08;font-weight:700;letter-spacing:-.035em;margin:6px 0 20px}
h2{font-size:22px;font-weight:600;letter-spacing:-.025em;margin:36px 0 12px}
h3{font-size:15px;font-weight:600;color:var(--muted);margin:22px 0 8px}
p{margin:0 0 12px}.legal{margin:44px 0 0;font-size:12px;color:var(--muted)}.muted,small{color:var(--muted)}small{font-size:13px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.lede{font-size:19px;color:var(--muted);letter-spacing:-.02em;margin:-10px 0 24px}
.panel{background:var(--panel);border-radius:18px;box-shadow:var(--shadow);padding:20px}
.list{background:var(--panel);border-radius:18px;box-shadow:var(--shadow);overflow:hidden}
.row{display:flex;align-items:center;gap:14px;padding:12px 16px;border-top:1px solid var(--line);color:var(--fg);min-height:56px}
.row:first-child{border-top:0}
.row .grow{flex:1;min-width:0}.row .r{font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums;font-size:17px;text-align:right}
.row .r small{display:block;font-weight:400}
.row small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.row small.w,.row.wrapt small{white-space:normal}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;background:var(--chip);font-size:12px;font-weight:500;color:var(--muted);margin:2px 3px 2px 0;letter-spacing:0;white-space:nowrap}
.chip.warn{color:var(--warn);background:var(--warn-bg)}.chip.ok{color:var(--ok);background:var(--ok-bg)}
a.chip{color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:8px}
.tile{background:var(--panel);border-radius:18px;box-shadow:var(--shadow);padding:18px 20px}.tile b{display:block;font-size:30px;font-weight:700;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.tile span{color:var(--muted);font-size:14px}
input,select,textarea{font:inherit;font-size:17px;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--fg);outline:none;max-width:100%}
select{-webkit-appearance:none;appearance:none;padding-right:38px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' fill='none' stroke='%2386868b' stroke-width='2' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 18%,transparent)}
input[type=checkbox]{width:18px;height:18px;padding:0;vertical-align:-3px;accent-color:var(--accent)}
input[type=file]{width:100%;font-size:15px}
button,.btn{display:inline-block;font:inherit;font-size:16px;font-weight:500;padding:12px 20px;min-height:44px;border:0;border-radius:999px;background:var(--accent);color:var(--accent-fg);cursor:pointer;text-align:center}
button.quiet,.btn.quiet{background:var(--chip);color:var(--fg)}button.sm{min-height:32px;padding:5px 12px;font-size:13px}
form.f{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 18px;align-items:center}
form.stack{display:flex;flex-direction:column;gap:12px;margin:0 0 18px}
.search{width:100%;font-size:20px;padding:15px 18px;border-radius:14px}
.price{display:flex;align-items:center;gap:10px;font-size:15px;color:var(--muted);min-height:44px}.price input{width:10em;font-size:17px}
.room{margin-top:10px;font-size:14px;color:var(--fg);white-space:normal}.room p{margin:8px 0 0}.room p:first-child{margin-top:0}.room b{font-variant-numeric:tabular-nums}.ratesub{font-size:15px;color:var(--muted);margin-top:2px}.room .sh{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:14px 0 6px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(70px,1fr));gap:6px}.stats div{background:var(--chip);border-radius:10px;padding:7px 9px}.stats small{display:block;font-size:12px;color:var(--muted)}.stats b{font-size:16px;font-variant-numeric:tabular-nums;white-space:nowrap}.room .bar{height:8px;margin-top:4px}.row .room{margin-top:6px;font-size:13px}
.seg{display:flex;background:var(--seg);border-radius:10px;padding:2px;gap:2px}
.seg label{flex:1;position:relative;text-align:center;font-size:14px;font-weight:500;padding:9px 6px;border-radius:8px;cursor:pointer;color:var(--fg);white-space:nowrap}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg label:has(input:checked){background:var(--seg-on);box-shadow:0 1px 3px rgba(0,0,0,.12),0 0 0 .5px rgba(0,0,0,.04)}
.toggle{display:flex;align-items:center;gap:10px;font-size:15px;color:var(--muted);min-height:44px}
.toggle input{appearance:none;-webkit-appearance:none;width:44px;height:26px;border-radius:999px;background:var(--seg);position:relative;padding:0;border:0;cursor:pointer;transition:.2s}
.toggle input::after{content:"";position:absolute;top:2px;left:2px;width:22px;height:22px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.25);transition:.2s}
.toggle input:checked{background:var(--ok)}.toggle input:checked::after{left:20px}
.pick{display:flex;gap:26px;align-items:center;margin:22px 0 10px}
.pick .info{flex:1;min-width:0}.pick .label{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.pick .name{font-size:28px;font-weight:700;letter-spacing:-.03em;line-height:1.15;margin:4px 0}
.pick .rate{font-size:56px;font-weight:700;letter-spacing:-.04em;line-height:1;color:var(--fg)}.pick .rate span{font-size:22px;color:var(--muted);font-weight:600;margin-left:4px}
.hint{margin-top:12px;padding:10px 14px;border-radius:12px;background:var(--chip);font-size:14px}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:18px;box-shadow:var(--shadow);background:var(--panel)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 16px;border-top:1px solid var(--line);vertical-align:middle;font-size:15px}
.cmpwrap{width:calc(100vw - 24px);max-width:1900px;position:relative;left:50%;transform:translateX(-50%)}table.cmp{width:auto;min-width:100%}table.cmp th,table.cmp td{padding:6px 7px;font-size:13px;text-align:center;border-left:1px solid var(--line);line-height:1.25}table.cmp tr>:first-child{position:sticky;left:0;z-index:1;background:var(--panel);text-align:left;border-left:0;min-width:86px;max-width:118px;white-space:normal;font-weight:600}table.cmp thead th{vertical-align:bottom;font-size:12px;font-weight:600;min-width:74px;max-width:104px;white-space:normal}table.cmp thead th a{color:var(--fg)}table.cmp .cf.sm{width:46px;margin:0 auto 4px}table.cmp td small{display:block;font-size:11px;color:var(--muted);white-space:normal}table.cmp td.best{background:var(--chip);font-weight:700}table.cmp td b{font-weight:inherit}table.cmp .x{font-weight:400;color:var(--muted);font-size:11px}
table.earn td small{display:block;font-weight:400}table.earn td.n{white-space:nowrap}table.earn th.n{text-align:right}.ph{display:none!important}button.linkrow{width:100%;text-align:left;background:none;border:0;border-radius:0;color:var(--fg);font-weight:400;font-size:16px}form.stack textarea{width:100%}.panel.fee{border-left:5px solid var(--warn);margin:14px 0}.panel.fee.soon{border-left-color:var(--line)}.panel.fee .say{font-size:17px;margin-bottom:8px}.chat{display:flex;flex-direction:column;gap:10px;max-width:720px;margin:0 auto}.chathead{display:flex;align-items:baseline;gap:10px}.chathead b{font-size:22px}.chathead .muted{flex:1;font-size:13px}.thread{display:flex;flex-direction:column;gap:10px;min-height:38vh;padding:6px 0}.msg{max-width:86%;padding:11px 15px;border-radius:20px;font-size:16px;line-height:1.45;overflow-wrap:anywhere}.msg p{margin:0 0 8px}.msg p:last-child{margin:0}.msg.bot{align-self:flex-start;background:var(--panel);box-shadow:var(--shadow);border-bottom-left-radius:6px}.msg.me{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:6px}.msg.err{color:var(--warn)}.msg.typing{display:flex;gap:5px;align-items:center;padding:15px 16px}.msg.typing i{width:8px;height:8px;border-radius:50%;background:var(--muted);animation:blink 1.2s infinite both}.msg.typing i:nth-child(2){animation-delay:.2s}.msg.typing i:nth-child(3){animation-delay:.4s}.msg.typing span{font-size:13px;color:var(--muted);margin-left:6px}@keyframes blink{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-3px)}}@media (prefers-reduced-motion:reduce){.msg.typing i{animation:none;opacity:.6}}.suggest{display:flex;flex-wrap:wrap;gap:8px}.suggest[hidden],.chat [hidden]{display:none!important}.suggest button.chip{border:0;cursor:pointer;font:inherit;font-size:14px;text-align:left;padding:8px 12px;border-radius:14px;white-space:normal;max-width:100%;color:var(--fg)}.composer{display:flex;gap:8px;align-items:flex-end;position:sticky;bottom:12px;background:var(--bg);padding:8px 0;margin:0}.composer textarea{flex:1;resize:none;max-height:140px;border-radius:22px;padding:12px 16px}.composer button{border-radius:22px;padding:12px 18px}.composer button[disabled]{opacity:.5}a.tile{color:inherit;display:block}a.tile:hover{text-decoration:none}.tile .go{display:block;color:var(--accent);margin-top:4px}.row.goal{flex-wrap:wrap}.lvl{display:flex;justify-content:space-between;gap:14px;padding:10px 0;border-top:1px solid var(--line)}.lvl>span:last-child{text-align:right}.lvl small{display:block;white-space:normal}.row.goal details.lv{flex:1 1 100%;margin-top:6px}.links{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}tr:first-child th{border-top:0}th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
details>summary{cursor:pointer;color:var(--accent);font-size:15px;margin:10px 0;min-height:32px;list-style:none;display:flex;align-items:center;gap:6px}
details>summary::-webkit-details-marker{display:none}details>summary::after{content:"›";transition:.15s;font-size:18px}details[open]>summary::after{transform:rotate(90deg)}
.wallet{display:flex;gap:14px;overflow-x:auto;padding:4px 22px 14px;margin:0 -22px;scroll-snap-type:x mandatory;scrollbar-width:none}.wallet::-webkit-scrollbar{display:none}
.wallet a{scroll-snap-align:center;flex:none}
.bar{height:6px;border-radius:3px;background:var(--seg);margin-top:6px;overflow:hidden}.bar i{display:block;height:100%;border-radius:3px;background:var(--ok)}
.bar i.hot{background:var(--warn)}.bar i.acc{background:var(--accent)}
/* the advisor */
.verdict{background:var(--panel);border-radius:18px;box-shadow:var(--shadow);padding:20px;border-top:4px solid var(--faint);margin:14px 0}
.verdict.v-ok{border-top-color:var(--ok)}.verdict.v-warn{border-top-color:var(--warn)}
.verdict .tag{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.verdict.v-ok .tag{color:var(--ok)}.verdict.v-warn .tag{color:var(--warn)}
.verdict .say{font-size:20px;font-weight:600;letter-spacing:-.02em;line-height:1.3;margin:6px 0 12px}
.verdict .take{font-size:16px;border-top:1px solid var(--line);padding-top:12px;margin-top:12px}
.verdict ul{margin:8px 0 0;padding-left:20px}.verdict li{margin:5px 0;font-size:15px;line-height:1.4}.verdict li.c{color:var(--muted)}.verdict li.c::marker{content:'⚠  ';font-size:12px}
.sums{margin:12px 0 0}.sums div{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid var(--line);font-size:15px}
.sums div span:last-child{font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
.pc{display:grid;grid-template-columns:1fr 1fr;gap:12px}.pc .panel{padding:16px 18px}.pc h3{margin:0 0 8px}
.pc ul{list-style:none;margin:0;padding:0}.pc li{position:relative;padding:5px 0 5px 24px;font-size:15px;line-height:1.35}
.pc li::before{position:absolute;left:0;font-weight:700}.pc .good li::before{content:"+";color:var(--ok)}.pc .bad li::before{content:"−";color:var(--warn)}
.tx{border-top:1px solid var(--line)}.tx:first-child{border-top:0}.tx>summary{margin:0;padding:12px 16px;color:var(--fg);gap:12px;min-height:56px}
.tx>summary::after{color:var(--faint)}.tx .d{width:50px;flex:none;font-size:13px;color:var(--muted);white-space:nowrap}
.tx .grow{flex:1;min-width:0}.tx .grow b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
.tx small{display:block}.tx .r{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
.tx summary .cf.sm{width:46px;flex:none}.cardbar{display:flex;gap:8px;overflow-x:auto;padding:4px 2px 10px;margin:0 -2px 6px;scrollbar-width:none;-webkit-overflow-scrolling:touch}.cardbar::-webkit-scrollbar{display:none}.cardchip{flex:none;display:flex;align-items:center;gap:8px;padding:8px 12px 8px 8px;border-radius:14px;background:var(--panel);box-shadow:var(--shadow);color:var(--fg);min-height:48px}.cardchip:hover{text-decoration:none}.cardchip .cf.sm{width:44px}.cardchip b{display:block;font-size:15px;line-height:1.1;font-variant-numeric:tabular-nums}.cardchip small{font-size:11.5px;color:var(--muted);white-space:nowrap}.cardchip.all{padding-left:14px}.cardchip.on{outline:2px solid var(--accent);outline-offset:-2px}.txlist .tx>summary,.txhead{display:grid;grid-template-columns:46px minmax(0,1fr) 62px 58px 40px 12px;column-gap:10px;align-items:center}.txhead{padding:10px 16px 8px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);border-bottom:1px solid var(--line)}.txlist .c{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.txlist .tx .c{font-size:15px;font-weight:600}.txlist .tx .c.mi{font-weight:700}.txlist .tx .c.ok{color:var(--ok)}.txlist .tx .c.warn{color:var(--warn)}.txlist .tx .c.rt{font-weight:500;color:var(--muted)}.txlist .tx .c.rt.ok{color:var(--ok)}.txlist .tx .c.rt.warn{color:var(--warn)}.txlist .tx>summary::after{justify-self:end}.txlist .tx .grow b{white-space:normal;overflow:visible}.tx .r.both{line-height:1.25}.tx .r.both b{display:block;font-variant-numeric:tabular-nums}.tx .r.both .amt{font-size:16px;font-weight:600;color:var(--fg)}.tx .r.both .mi{font-size:16px;font-weight:700}.tx .r.both .mi.ok{color:var(--ok)}.tx .r.both .mi.warn{color:var(--warn)}.tx .r.both small{font-size:11.5px;font-weight:500;color:var(--muted)}.tx .r.miles{font-size:19px;font-weight:700;line-height:1.15}.tx .r.miles small{font-size:11.5px;font-weight:500;color:var(--muted)}.tx .r.miles.ok{color:var(--ok)}.tx .r.miles.warn{color:var(--warn)}.tx .more{padding:0 16px 14px 70px;font-size:14px}.tx form.quick{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:6px 0 2px;grid-column:2/-1;grid-row:2}.txlist .tx>summary::after{grid-row:1;grid-column:-2}.tx .badge{font-size:11.5px;font-weight:600;color:var(--warn);background:var(--warn-bg);border-radius:999px;padding:2px 8px}.tx form.quick button.sm{min-height:28px;padding:3px 10px;font-size:12.5px}.tx form.quick button.on{outline:1px solid var(--faint);outline-offset:-1px}.tx .more p{margin:0 0 6px}
.q{border-left:3px solid var(--line);padding:2px 0 2px 12px;margin:6px 0;font-size:14px;color:var(--muted);font-style:italic}
/* card faces */
.cf{display:block;width:300px;max-width:100%;aspect-ratio:1.586;border-radius:14px;position:relative;overflow:hidden;flex:none;
  color:var(--cf-fg,#fff);background:var(--cf-bg);box-shadow:0 10px 30px rgba(0,0,0,.18),inset 0 0 0 .5px rgba(255,255,255,.25);font-size:15px}
.cf::before{content:"";position:absolute;inset:0;background:linear-gradient(115deg,rgba(255,255,255,.28) 0%,rgba(255,255,255,0) 38%,rgba(255,255,255,0) 62%,rgba(255,255,255,.1) 100%)}
.cf .bk{position:absolute;top:8%;left:7%;font-weight:700;letter-spacing:.02em;font-size:1.1em}
.cf .chipm{position:absolute;top:38%;left:7%;width:15%;aspect-ratio:1.3;border-radius:16%;background:linear-gradient(135deg,#e9d8a6,#b8955a);box-shadow:inset 0 0 0 .5px rgba(0,0,0,.25)}
.cf .nm{position:absolute;left:7%;right:30%;bottom:9%;font-weight:600;line-height:1.15;font-size:.95em;text-shadow:0 1px 2px rgba(0,0,0,.2)}
.cf .nw{position:absolute;right:7%;bottom:9%;font-weight:800;font-style:italic;font-size:1.05em;opacity:.95}
.cf img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
.cf.photo{background:transparent;box-shadow:none;overflow:visible}.cf.photo::before{display:none}
.cf.photo img{filter:drop-shadow(0 6px 14px rgba(0,0,0,.18))}.cf.sm.photo img{filter:drop-shadow(0 1px 3px rgba(0,0,0,.2))}
.cf.sm{width:64px;border-radius:6px;box-shadow:0 2px 6px rgba(0,0,0,.18)}.cf.sm .nm,.cf.sm .nw,.cf.sm .bk{display:none}.cf.sm .chipm{top:34%}
.cf.md{width:190px;border-radius:10px;font-size:10px}.cf.lg{width:340px;border-radius:16px;font-size:17px}
@media (max-width:760px){
  header{padding:var(--sat) 16px 0}
  .tabs{top:auto;bottom:0;height:calc(58px + var(--sab));padding:0 4px var(--sab);gap:0;justify-content:space-around;align-items:stretch;pointer-events:auto;
    background:var(--hdr);-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);border-top:1px solid var(--line)}
  .tabs a{flex:1;flex-direction:column;justify-content:center;gap:3px;font-size:10.5px;padding:0;border-radius:0;letter-spacing:0}
  .tabs a svg{width:25px;height:25px}.tabs a.on{background:none;color:var(--accent)}
  main{padding:18px 16px calc(92px + var(--sab))}.txlist .tx>summary,.txhead{grid-template-columns:40px minmax(0,1fr) 54px 50px 30px 10px;column-gap:8px;padding-left:12px;padding-right:10px}.txlist .tx summary .cf.sm{width:40px}.txlist .tx .c{font-size:14px}.seg.tall{flex-direction:column}.seg.tall label{width:100%}.composer{bottom:calc(74px + var(--sab))}.msg{max-width:92%}
  .sub{margin:0 -16px 14px;padding:2px 16px}.wallet{margin:0 -16px;padding:4px 16px 14px}
  h1{font-size:32px;margin-bottom:16px}h2{font-size:20px;margin-top:30px}.lede{font-size:17px;margin-bottom:20px}
  .pick{flex-direction:column;align-items:stretch;gap:16px}.pick>a{display:block}.cf.lg{width:100%;font-size:5vw}.cf.md{width:160px;font-size:8.5px}
  .pick.now{flex-direction:row;align-items:center;gap:16px;margin-top:16px}.pick.now>a{flex:none}.pick.now .cf.lg{width:132px;font-size:7px;border-radius:9px}
  .pick.now .name{font-size:22px}.pick.now .rate{font-size:44px}.pick .rate{font-size:48px}.seg label{font-size:13px;padding:10px 2px}.grid{grid-template-columns:1fr 1fr;gap:10px}.tile{padding:14px 16px}.tile b{font-size:24px}
  .tile span{font-size:13px}th,td{padding:10px 12px;font-size:14px}table.earn .dk{display:none}table.earn td:first-child{width:56%}table.earn .ph{display:block!important}.pc{grid-template-columns:1fr}.cf.sm{width:52px}.row{gap:12px;padding:12px 14px}
  .verdict .say{font-size:18px}.tx .more{padding-left:16px}form.f>input,form.f>select{flex:1 1 100%}
  .row .r{font-size:16px}}
@media (display-mode:standalone){header .brand{font-size:21px}}
"""

# bank colours (approximate brand palettes) -> gradient stops; card-name words pick a finish
BANK = {"dbs": ("#d6001c", "#8c0012"), "uob": ("#0b3d91", "#04214f"), "ocbc": ("#e2231a", "#9b1210"),
        "citi": ("#056dae", "#00346a"), "hsbc": ("#db0011", "#6d0009"), "sc": ("#0473ea", "#0a5c36"),
        "maybank": ("#ffcc00", "#e0a800", "#1d1d1f"), "amex": ("#2e77bb", "#1b4f80"), "cimb": ("#8b0000", "#4a0000"),
        "boc": ("#b2232f", "#6d1219"), "trust": ("#2d2d2d", "#0f0f0f"), "dcs": ("#5a3e91", "#2b1d4a"),
        "icbc": ("#c7000b", "#7a0007")}
FINISH = [(("infinite", "reserve", "prestige", "metal", "beyond", "insignia", "visa infinite", "solitaire pps", "voyage"),
           ("#3a3a3c", "#0b0b0c")),
          (("platinum",), ("#e3e4e6", "#9fa3a8", "#1d1d1f")),
          (("krisflyer", "pps"), ("#1b2a4a", "#0d1528")),
          (("lady",), ("#c2577a", "#7c2846")),
          (("gold",), ("#e6c36a", "#a9822e", "#1d1d1f")),
          (("world",), ("#2c2c2e", "#111"))]


def network_of(c):
    n = (c.get("network") or c.get("name") or "").lower()
    if "amex" in n or "american express" in n or c.get("bank_id") == "amex":
        return "AMEX"
    if "master" in n:
        return "mastercard"
    if "unionpay" in n or "union pay" in n:
        return "UnionPay"
    return "VISA"


def palette(c):
    name = (c.get("name") or "").lower()
    for words, pal in FINISH:
        if any(w in name for w in words):
            return pal
    pal = BANK.get(c.get("bank_id"))
    if pal:
        return pal
    h = int(hashlib.sha1((c.get("id") or "").encode()).hexdigest()[:6], 16)
    return (f"hsl({h % 360} 45% 42%)", f"hsl({h % 360} 50% 24%)")


BANK_SHORT = {"dbs": "DBS", "uob": "UOB", "ocbc": "OCBC", "citi": "citi", "hsbc": "HSBC", "sc": "standard chartered",
              "maybank": "Maybank", "amex": "AMERICAN EXPRESS", "cimb": "CIMB", "boc": "BANK OF CHINA", "trust": "trust",
              "dcs": "DCS", "icbc": "ICBC"}


def face(c, size="md", has_photo=False):
    """A card face: the household's photo when there is one, else a drawn face in the bank's colours."""
    pal = palette(c)
    fg = pal[2] if len(pal) > 2 else "#fff"
    style = f"--cf-bg:linear-gradient(135deg,{pal[0]},{pal[1]});--cf-fg:{fg}"
    ver = f"?v={e(has_photo)}" if isinstance(has_photo, str) else ""   # new picture = new URL, never a stale cached one
    if ver and size == "sm":
        ver += "&s=1"   # list rows get a 192 px copy, not the 900 px picture
    inner = (f"<img src='/img/{e(c['id'])}{ver}' alt='' loading=lazy decoding=async>" if has_photo else
             f"<span class=bk>{e(BANK_SHORT.get(c.get('bank_id'), (c.get('bank_id') or '').upper()))}</span><span class=chipm></span>"
             f"<span class=nm>{e(c.get('name'))}</span><span class=nw>{e(network_of(c))}</span>")
    return f"<span class='cf {size}{' photo' if has_photo else ''}' style='{style}' role=img aria-label='{e(c.get('name'))}'>{inner}</span>"


JS = """document.addEventListener('change',function(ev){var f=ev.target.form;if(f&&f.dataset.auto!==undefined&&(ev.target.dataset.go!==undefined||(ev.target.type!=='text'&&ev.target.type!=='search'))){f.requestSubmit?f.requestSubmit():f.submit();}});
(function(){var t=document.querySelector('[data-take]');if(!t)return;var n=0;function go(){fetch(t.dataset.take,{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
if(d.status==='ready'){t.textContent=d.text;t.hidden=false;}else if(d.status==='writing'&&++n<12){setTimeout(go,4000);}}).catch(function(){});}setTimeout(go,2500);})();
(function(){var chat=document.getElementById('chat');if(!chat)return;var th=document.getElementById('thread'),f=document.getElementById('composer'),ta=f.querySelector('textarea'),
send=f.querySelector('button'),sug=document.getElementById('suggest'),clr=document.getElementById('chatclear'),hist=[],busy=false,K='cards-ask';
try{hist=JSON.parse(sessionStorage.getItem(K)||'[]')}catch(e){hist=[]}
function add(cls,text){var d=document.createElement('div');d.className='msg '+cls;String(text).split(/\\n+/).forEach(function(p){if(p.trim()){var el=document.createElement('p');el.textContent=p;d.appendChild(el);}});
th.appendChild(d);d.scrollIntoView({block:'end',behavior:'smooth'});return d;}
function save(){try{sessionStorage.setItem(K,JSON.stringify(hist.slice(-12)))}catch(e){}clr.hidden=!hist.length;sug.hidden=!!hist.length;}
hist.forEach(function(t){add('me',t.q);add('bot',t.a);});save();
function grow(){ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,140)+'px';}
function ask(q){q=(q||'').trim();if(!q||busy)return;busy=true;send.disabled=true;ta.value='';grow();add('me',q);sug.hidden=true;
var t=document.createElement('div');t.className='msg bot typing';t.innerHTML='<i></i><i></i><i></i><span>Looking at your cards…</span>';th.appendChild(t);t.scrollIntoView({block:'end',behavior:'smooth'});
var s0=Date.now(),lab=t.querySelector('span'),tick=setInterval(function(){var s=Math.round((Date.now()-s0)/1000);if(s>=8)lab.textContent='Still working… '+s+' s';},1000);
var body=new URLSearchParams();body.set('q',q);body.set('h',JSON.stringify(hist.slice(-3)));
fetch('/ask/api',{method:'POST',body:body,credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error(r.status);return r.json();}).then(function(d){
if(d.answer){add('bot',d.answer);hist.push({q:q,a:d.answer});save();}else{add('bot err',d.error||'No answer. Try again.');}})
.catch(function(){add('bot err','That did not go through. Check your connection and try again.');})
.then(function(){clearInterval(tick);t.remove();busy=false;send.disabled=false;ta.focus();});}
f.addEventListener('submit',function(ev){ev.preventDefault();ask(ta.value);});
ta.addEventListener('keydown',function(ev){if(ev.key==='Enter'&&!ev.shiftKey&&!ev.isComposing){ev.preventDefault();ask(ta.value);}});ta.addEventListener('input',grow);
sug.addEventListener('click',function(ev){var b=ev.target.closest('[data-ask]');if(b)ask(b.dataset.ask);});
clr.addEventListener('click',function(){hist=[];save();Array.prototype.slice.call(th.querySelectorAll('.msg')).slice(1).forEach(function(n){n.remove();});ta.focus();});})();
document.addEventListener('touchstart',function(){},{passive:true});"""

ICONS = {
    "pay": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='2.5' y='5' width='19' height='14' rx='3'/><path d='M2.5 10h19M6.5 15h4'/></svg>",
    "wallet": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M4 7.5V17a3 3 0 0 0 3 3h11a2 2 0 0 0 2-2v-7a2 2 0 0 0-2-2H6a2 2 0 0 1-2-2 2 2 0 0 1 2-2h10.5'/><circle cx='16.5' cy='14.5' r='1.2' fill='currentColor' stroke='none'/></svg>",
    "spend": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M6 3.5h12v17l-3-2-3 2-3-2-3 2z'/><path d='M9.5 8.5h5M9.5 12.5h5'/></svg>",
    "ask": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M4.5 5.500h15a1.500 1.500 0 0 1 1.500 1.500v8.500a1.500 1.500 0 0 1-1.500 1.500H11l-4.500 3.500V17H4.500A1.500 1.500 0 0 1 3 15.500V7a1.500 1.500 0 0 1 1.500-1.500z'/><path d='M8 10h8M8 13h5'/></svg>",
    "advisor": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M9 18h6M10 21h4'/><path d='M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.300 1.100 2.200h5c0-.9.4-1.600 1.100-2.200A6 6 0 0 0 12 3z'/></svg>",
    "cards": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='8' width='18' height='12' rx='2.5'/><path d='M6 8V6.500A1.500 1.500 0 0 1 7.500 5h9A1.500 1.500 0 0 1 18 6.500V8'/></svg>",
    "flights": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M21 4 3.500 10.500l6 2.500 2.500 6z'/><path d='M9.500 13 21 4'/></svg>",
    "gear": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='8.500' r='3.500'/><path d='M5 20c.600-3.600 3.400-5.500 7-5.500s6.400 1.900 7 5.500'/></svg>",
    "sun": "<svg class=sun viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='12' r='4'/><path d='M12 2.500v2.500M12 19v2.500M2.500 12H5M19 12h2.500M5.300 5.300l1.800 1.800M16.900 16.900l1.800 1.800M5.300 18.700l1.800-1.800M16.900 7.100l1.800-1.800'/></svg>",
    "moon": "<svg class=moon viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M20 14.500A8 8 0 0 1 9.500 4a7 7 0 1 0 10.500 10.500z'/></svg>",
}


MANIFEST = """{"name": "Cards", "short_name": "Cards", "id": "/", "start_url": "/", "scope": "/", "display": "standalone",
 "background_color": "#f5f5f7", "theme_color": "#f5f5f7", "description": "Which card to use, what your wallet earns, and which cards to keep, cancel or get",
 "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
           {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}"""

_icons = {}


def icon(size):
    """App icon drawn once: two stacked cards (blue + rose) on white, Wallet-style."""
    if size in _icons:
        return _icons[size]
    import io
    from PIL import Image, ImageDraw
    s = size * 4
    im = Image.new("RGB", (s, s), (245, 245, 247))
    d = ImageDraw.Draw(im)
    w, h = int(s * 0.66), int(s * 0.66 / 1.586)
    def card(x, y, c1, c2):
        layer = Image.new("RGB", (w, h))
        ld = ImageDraw.Draw(layer)
        for i in range(h):
            t = i / h
            ld.line([(0, i), (w, i)], fill=tuple(int(c1[k] * (1 - t) + c2[k] * t) for k in range(3)))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(h * 0.12), fill=255)
        im.paste(layer, (x, y), mask)
        d.rounded_rectangle([x + int(w * .1), y + int(h * .38), x + int(w * .25), y + int(h * .62)], radius=int(h * .04), fill=(222, 196, 140))
    card(int(s * .12), int(s * .2), (11, 61, 145), (4, 33, 79))
    card(int(s * .22), int(s * .42), (194, 87, 122), (124, 40, 70))
    im = im.resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG")
    _icons[size] = out.getvalue()
    return _icons[size]


# /theme.js: tiny, loaded in <head> without defer so the chosen theme is on <html> before the first paint.
THEME_JS = """(function(){var K='cards-theme',r=document.documentElement,t=null;try{t=localStorage.getItem(K)}catch(e){}
function meta(){var d=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
Array.prototype.forEach.call(document.querySelectorAll('meta[name=theme-color]'),function(m){m.removeAttribute('media');m.content=d?'#000':'#f5f5f7';});return d;}
if(t==='dark'||t==='light')r.dataset.theme=t;
document.addEventListener('DOMContentLoaded',function(){meta();var b=document.getElementById('theme');if(!b)return;
b.addEventListener('click',function(){var n=meta()?'light':'dark';r.dataset.theme=n;try{localStorage.setItem(K,n)}catch(e){}meta();
b.setAttribute('aria-label',n==='dark'?'Switch to light':'Switch to dark');});});})();"""
