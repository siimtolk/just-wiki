"""The web UI's one stylesheet: light and dark, phone width, no external assets."""
CSS = """
:root{--bg:#fbfaf7;--card:#ffffff;--panel:#f2efe8;--soft:#f6f4ee;--hover:#f3f0e8;--text:#1f1d1a;--muted:#6b665d;--line:#e0dbd0;
--link:#1f5fa8;--ring:rgba(31,95,168,.28);--ok:#2d7a3e;--warn:#9a6a00;--bad:#b3261e;--code:#efece4;--add:#e6f4e8;--del:#fbe9e7;--mine:#fff6dc}
@media (prefers-color-scheme:dark){:root{--bg:#171614;--card:#1c1b19;--panel:#201f1c;--soft:#22211e;--hover:#262420;--text:#e8e4dc;
--muted:#9b958a;--line:#33312c;--link:#8ab4f0;--ring:rgba(138,180,240,.35);--ok:#7cc98a;--warn:#e0b24f;--bad:#f08a80;--code:#26241f;
--add:#1d3323;--del:#3a1f1c;--mine:#332b16}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15.5px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;gap:14px;align-items:center;padding:8px 18px;border-bottom:1px solid var(--line);background:var(--panel);
position:sticky;top:0;z-index:5}
.brand{font-weight:650;color:var(--text);white-space:nowrap}
.tabs{display:flex;gap:2px;flex:1;min-width:0;overflow-x:auto;scrollbar-width:none}.tabs::-webkit-scrollbar{display:none}
.tabs a{padding:5px 11px;border-radius:7px;color:var(--text);font-size:14.5px;white-space:nowrap}
.tabs a:hover{background:var(--hover);text-decoration:none}.tabs a.here{background:var(--code);font-weight:600}
header form{flex:none}header input{width:min(210px,36vw);padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);
color:var(--text);font:inherit;font-size:14px}
.wrap{display:flex;max-width:1280px;margin:0 auto}
.wrap.wide{display:block}.wrap.wide main{max-width:1120px;margin:0 auto}
nav{width:250px;flex:none;padding:14px 16px;font-size:14px;border-right:1px solid var(--line);position:sticky;top:49px;
height:calc(100vh - 49px);overflow:auto}
nav summary{display:none}nav h3{font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:18px 0 4px}
nav ul{list-style:none;margin:0;padding:0}nav ul.top{margin-top:8px}nav li a{display:block;padding:2px 6px;border-radius:5px;color:var(--text)}
nav li a:hover{background:var(--hover);text-decoration:none}nav a.here{background:var(--code);font-weight:600}
nav .newpage{margin:0 0 6px}nav .newpage a{display:block;padding:6px 10px;border:1px dashed var(--line);border-radius:8px;text-align:center}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-left:6px;vertical-align:middle;background:var(--muted)}
.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
main{flex:1;min-width:0;padding:22px 34px 80px;max-width:860px}
h1{font-size:1.65rem;line-height:1.25;margin:.1em 0 .2em;letter-spacing:-.01em}
h2{font-size:1.2rem;margin-top:1.8em;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{font-size:1rem;margin:1.2em 0 .3em}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.88em;overflow-wrap:anywhere}
pre{background:var(--code);padding:12px;border-radius:8px;overflow:auto;font-size:13px;line-height:1.45}pre code{padding:0;background:none}
table{border-collapse:collapse;display:block;overflow-x:auto;max-width:100%}th,td{border:1px solid var(--line);padding:5px 10px;text-align:left;vertical-align:top}
th{background:var(--panel)}
table.list{display:table;width:100%;font-size:14.5px}table.list td p{margin:0}
.meta{color:var(--muted);font-size:14px;margin:2px 0 10px}
.id{color:var(--muted);font-size:13px}
.nowrap{white-space:nowrap}
.tag{display:inline-block;padding:0 8px;border-radius:10px;background:var(--code);font-size:12.5px;line-height:1.55}
.tag.ok,.ok{color:var(--ok)}.tag.warn,.warn{color:var(--warn)}.tag.bad,.bad{color:var(--bad)}
.count{display:inline-block;min-width:1.6em;padding:0 6px;border-radius:10px;background:var(--code);font-size:13px;text-align:center;font-weight:500}
.sources{font-size:14px;color:var(--muted);margin-bottom:1em}.sources ul{margin:4px 0}
.broken{color:var(--bad);border-bottom:1px dashed var(--bad)}
.live{border-left:3px solid var(--line);padding-left:12px;margin:1em 0}.live>p:first-child em{color:var(--muted);font-size:13px}
.side{margin-top:3em;font-size:14px}.side h2{font-size:1rem}
ul.plain{list-style:none;padding:0}ul.plain li{margin:3px 0}
.hits{list-style:none;padding:0}.hits li{margin-bottom:14px}.hits p{margin:2px 0;color:var(--muted);font-size:14px}
.subnav{display:flex;gap:4px;margin:0 0 6px}.subnav a{padding:3px 11px;border-radius:14px;color:var(--text);font-size:14px;border:1px solid var(--line)}
.subnav a:hover{background:var(--hover);text-decoration:none}.subnav a.here{background:var(--link);border-color:var(--link);color:#fff;font-weight:600}
.commit{padding:7px 0;border-bottom:1px solid var(--line)}.commit .files{font-size:14px;color:var(--muted)}
.sha{font-family:ui-monospace,monospace;font-size:13px}.when{color:var(--muted);font-size:13px;margin:0 6px}
.diff .add{background:var(--add);display:block}.diff .del{background:var(--del);display:block}
.diff .hunk{color:var(--link)}.diff .file{font-weight:650;display:block;margin-top:1em}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:12px 0}
.card.bad{border-color:var(--bad)}
.notice{background:var(--soft);border:1px solid var(--line);border-left:4px solid var(--muted);border-radius:8px;padding:10px 14px;margin:12px 0}
.notice,.notice.ok,.notice.bad,.notice.warn{color:var(--text)}.notice.ok{border-left-color:var(--ok)}.notice.bad{border-left-color:var(--bad)}.notice.warn{border-left-color:var(--warn)}
details.note{border:1px solid var(--line);border-radius:10px;padding:7px 14px;margin:6px 0;background:var(--card)}details.note summary{cursor:pointer}
/* buttons and forms */
button,.btn{font:inherit;font-size:14.5px;padding:7px 16px;border-radius:8px;border:1px solid var(--link);background:var(--link);color:#fff;
cursor:pointer;display:inline-block;line-height:1.4}.btn:hover{text-decoration:none}
button:hover,.btn:hover{filter:brightness(1.08)}button[disabled]{opacity:.5;cursor:default}
button.alt,.btn.alt{background:var(--card);color:var(--text);border-color:var(--line)}button.alt:hover,.btn.alt:hover{border-color:var(--muted);filter:none}
button.danger,.btn.danger{background:var(--card);color:var(--bad);border-color:var(--line)}button.danger:hover,.btn.danger:hover{border-color:var(--bad);filter:none}
button.sm,.btn.sm{padding:2px 10px;font-size:13px;border-radius:7px}
button.link{background:none;border:0;padding:0;color:var(--link);font-size:13px;cursor:pointer}
a.danger{color:var(--bad)}
input,select,textarea{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 11px;width:100%}
input[type=checkbox],input[type=radio]{width:auto}
input:focus,select:focus,textarea:focus{outline:2px solid var(--ring);outline-offset:0;border-color:var(--link)}
select{width:auto;max-width:100%}
textarea{display:block}
form.row,div.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}form.row input{flex:1;min-width:130px;width:auto}
form.inline{display:inline;margin-left:6px}
label.opt{display:inline-flex;gap:6px;align-items:center;margin:2px 12px 2px 0;font-size:14px}
details.actions summary{cursor:pointer;color:var(--link);font-size:14px}details.actions form{margin:8px 0}
.form{display:grid;gap:16px}
.field{display:grid;gap:5px}.field>.lbl{display:block;min-width:0;font-weight:600;font-size:13.5px;text-transform:none;letter-spacing:0;color:var(--text)}.field .hint{font-size:12.5px;color:var(--muted);font-weight:400}
.req{color:var(--bad)}
.editor-grid{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:24px;align-items:start}
.sidecard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;display:grid;gap:16px;position:sticky;top:64px}
textarea.code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13.5px;line-height:1.6;min-height:62vh;resize:vertical;tab-size:2}
.actionbar{position:sticky;bottom:0;z-index:4;margin:22px -34px -80px;padding:11px 34px;background:var(--bg);border-top:1px solid var(--line);
display:flex;gap:10px;align-items:center;flex-wrap:wrap}.actionbar .grow{flex:1}.actionbar .id{margin:0}
.pagehead{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}
.crumbs{font-size:13px;color:var(--muted);margin:0 0 2px}.crumbs a{color:var(--muted)}
.pageactions{display:flex;gap:6px;margin-top:4px}
.preview{border:1px solid var(--line);border-radius:12px;padding:4px 22px 14px;background:var(--card);margin:0 0 18px}
.preview>.tag{margin-top:12px}
/* panels: stats, memory and custom tabs */
section.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px 18px;margin:16px 0}
section.panel>h2{margin:0 0 4px;border:0;padding:0;font-size:1.08rem}
section.panel>.meta{margin:0 0 12px}
section.panel>details.fold>summary h2{margin:0}
.cols{display:grid;grid-template-columns:minmax(190px,2fr) 7fr;gap:22px;align-items:start}.cols>*{min-width:0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:8px 0 4px}
.stat{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:0}
.stat .sv{font-size:1.55rem;font-weight:650;line-height:1.25;overflow-wrap:anywhere}.stat .sl{font-size:13.5px;font-weight:500}
.stat .sn{font-size:12.5px;color:var(--muted);line-height:1.4}
.stat.ok .sv{color:var(--ok)}.stat.warn .sv{color:var(--warn)}.stat.bad .sv{color:var(--bad)}
.bars{margin:6px 0}.bars-title{font-size:13px;font-weight:600;color:var(--muted);margin:10px 0 2px}
.bar{display:grid;grid-template-columns:minmax(70px,150px) 1fr 2.6em;gap:10px;align-items:center;font-size:13px;margin:3px 0}
.bar .bl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bar .bt{background:var(--code);border-radius:5px;height:10px;overflow:hidden}
.bar .bt i{display:block;height:100%;background:var(--link);border-radius:5px}.bar .bv{text-align:right;color:var(--muted)}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:9px;margin:8px 0 2px}
.tablewrap table.list{font-size:14px;margin:0}
.tablewrap th{background:var(--soft);font-weight:600;font-size:13px;color:var(--muted);padding:7px 10px;border:0;border-bottom:1px solid var(--line);white-space:nowrap}
.tablewrap td{padding:7px 10px;border:0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.colchart{display:flex;align-items:flex-end;gap:3px;height:96px;border-bottom:1px solid var(--line);padding-top:6px;margin-top:4px}
.colchart .c{flex:1;min-width:3px;background:var(--link);border-radius:3px 3px 0 0;opacity:.9}.colchart .c:hover{opacity:1}
.colaxis{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:3px}
.tablewrap tr:last-child td{border-bottom:0}.tablewrap tbody tr:hover td{background:var(--hover)}
.cards{display:grid;gap:8px;margin:8px 0}.cards .card{margin:0;background:var(--soft)}
.q{border:1px solid var(--line);border-radius:10px;padding:9px 14px;margin:8px 0;background:var(--card)}.q.mine{background:var(--mine)}
.qhead{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13.5px}.qtext p{margin:4px 0}
.qsum{margin:6px 0}.qsum div{margin:2px 0}.lbl{display:inline-block;min-width:3.2em;color:var(--muted);font-size:13px;text-transform:uppercase;letter-spacing:.04em}
details.full summary{cursor:pointer;color:var(--muted);font-size:13px}
label.sugg{align-items:flex-start;padding:6px 8px;border:1px solid var(--line);border-radius:8px;margin:4px 0}label.sugg input{margin-top:5px}
tr.mine td{background:var(--mine)}
.badge{display:inline-block;min-width:1.3em;padding:0 5px;margin-left:4px;border-radius:9px;background:var(--bad);color:#fff;font-size:12px;text-align:center}
.badge.soft{background:var(--code);color:var(--text)}.badge.dot-run{background:none;color:var(--ok)}
details.fold>summary{cursor:pointer;list-style:none}details.fold>summary h2{display:inline-block;margin:0 0 .3em;border:0;padding:0;font-size:1.08rem}
details.fold>summary::before{content:'▸ ';color:var(--muted)}details.fold[open]>summary::before{content:'▾ '}
ul.answered li{padding:4px 0;border-bottom:1px solid var(--line)}
.scopes{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center}
.secthead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:1.6em 0 .6em;border-bottom:1px solid var(--line);padding-bottom:4px}
.secthead h2{margin:0;border:0;padding:0;flex:1}.secthead form{margin:0}
.acts{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.acts details{display:inline-block}.acts summary{cursor:pointer;color:var(--link);font-size:14px}
.tablewrap td input{min-width:120px}.tablewrap td input[name=about]{min-width:280px}
@media (max-width:980px){.editor-grid{grid-template-columns:1fr}.sidecard{position:static}.cols{grid-template-columns:1fr}}
@media (max-width:760px){.wrap{display:block}nav{width:auto;height:auto;position:static;border-right:0;border-bottom:1px solid var(--line);padding:8px 16px}
nav summary{display:block;cursor:pointer;color:var(--muted)}main{padding:14px 16px 90px}header{gap:6px 10px;padding:8px 12px;flex-wrap:wrap;position:static}
.tabs{order:3;flex:1 1 100%;flex-wrap:wrap;overflow:visible}header form{margin-left:auto}
.actionbar{margin:18px -16px -90px;padding:10px 16px}.bar{grid-template-columns:minmax(70px,120px) 1fr 2.4em}
section.panel{padding:14px 14px 16px}.stats{grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}}
"""
