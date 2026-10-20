#!/usr/bin/env python3
"""把本机跑通的两屏页面，压成一个**零依赖、零凭证、可静态托管**的演示页。

为什么这么做
------------
真跑要两把钥匙（麦当劳 MCP Token、高德 key），陌生人不可能为了看一眼就都去申请。
所以对外展示的这一版不联网：把一次**真实运行**返回的 JSON 原样烤进页面，
前端照原来的渲染逻辑跑 —— 看到的价格、时长、文案都是真的，只是不重新查。

陌生人最容易误会的一件事
------------------------
「我填我的地址，它会算我的吧？」—— 不会。演示版没有后端，算不了。
所以演示页**不能长成一个可以随手填的表单**：填了不算，还照你的条件显示别人的结果，
那是骗人。这一版把入口换成三张场景卡，表单隐藏、只做内部传参；
点卡就回放那一份，认不出就明说认不出，绝不拿 A 顶替。

产物
----
index.html        单文件，在仓库根目录 —— 双击可开，GitHub Pages 默认从根目录发布，开箱即用。
demo/snap/*.json  三份真实运行的原始返回（构建输入，也是可核验的数据）
app/index.html    不动。本地跑仍然是真请求（planFetch 走 fetch）。

用法
----
    python3 tools/build_demo.py

改前端之后重跑一次即可，快照不用重抓。
要换快照：改下面 SCENARIOS，并把对应的真实返回存到 demo/snap/<id>.json。
"""

import json
import os
import re
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "app", "index.html")
SNAP_DIR = os.path.join(ROOT, "demo", "snap")
# 输出到仓库根：GitHub Pages 默认从根发布，不用额外设置 docs/ 目录
OUT_DIR = ROOT
OUT = os.path.join(OUT_DIR, "index.html")

# ---------------------------------------------------------------- 场景定义
# id     = 快照文件名
# title  = 卡片主标题（用户视角的一句话）
# place  = 地点短名，拼在条件行里
# see    = 「看什么」，{} 里依次填 panes 的价格（dinein / delivery / dinein）
SCENARIOS = [
    # 卡片只看一句结论，价格留给结果页 —— 塞两个价会折行，把「外卖」拆成两半
    {"id": "A", "title": "演唱会散场，还没定",
     "place": "杭州大莲花",
     "see": "堂食 / 外卖 两套账都给你"},
    {"id": "B", "title": "点名要吃麦辣鸡翅",
     "place": "武汉江汉路",
     "see": "想要的排第一位　{0} 拿下"},
    {"id": "C", "title": "6 人外卖，人均 30",
     "place": "上海陆家嘴",
     "see": "够不够，直接算给你看"},
]

# 地址不再打码：三个场景现在都是公共地标（场馆 / 商场 / 酒店 / 街区），
# 本来就是公开地址；改门牌号只会变成一条不存在的假地址，反而更假。
# 快照里也不含联系人、手机号、addressId —— 只留门店名和价格。


def load_snaps():
    snaps = {}
    for sc in SCENARIOS:
        p = os.path.join(SNAP_DIR, sc["id"] + ".json")
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        snaps[sc["id"]] = data
        echo = data.get("echo") or {}
        print("  %s  %-24s %4.1f 秒  %s" % (
            sc["id"], sc["title"], (data.get("took_ms") or 0) / 1000.0,
            "＋".join(k for k, v in (data.get("panes") or {}).items() if (v or {}).get("ok"))))
        del echo
    return snaps


def money(v):
    if v is None:
        return "—"
    return ("¥%d" % round(v)) if abs(v - round(v)) < 0.004 else ("¥%.2f" % v)


def pane_price(snap, kind):
    p = snap.get("panes") or {}
    b = ((p.get(kind) or {}).get("best") or {})
    return money(b.get("price"))


def cond_line(snap, place):
    """条件行：周三 12:10 · 3 人 · 泰州万达"""
    e = snap.get("echo") or {}
    when = e.get("when") or ""
    hhmm = when.split(" ")[-1] if " " in when else ""
    bits = []
    if e.get("weekday"):
        bits.append(e["weekday"])
    if hhmm:
        bits.append(hhmm)
    if e.get("people"):
        bits.append("%d 人" % e["people"])
    if place:
        bits.append(place)
    return " · ".join(bits)


def qs(echo):
    """把快照里的输入拼成深链 —— 无 JS 时的兜底路径，也方便单独分享某个场景。"""
    pairs = [
        ("from", echo.get("from_addr") or ""),
        ("to", echo.get("to_addr") or ""),
        ("when", echo.get("when") or ""),
        ("people", str(echo.get("people") or 3)),
        ("form", echo.get("form") or "dinein"),
        ("taste", echo.get("taste") or ""),
        ("budget", (str(int(round(echo["budget"]))) if echo.get("budget") else "")),
        ("go", "1"),
    ]
    return "?" + "&".join(
        "%s=%s" % (k, urllib.parse.quote(str(v), safe=""))
        for k, v in pairs if v != "")


def see_line(sc, snap):
    prices = [pane_price(snap, "dinein"), pane_price(snap, "delivery")]
    if "{" in sc["see"]:
        try:
            return sc["see"].format(*prices)
        except (IndexError, KeyError):
            return sc["see"]
    return sc["see"]


DEMO_CSS = """
/* ===== 演示模式（只在 demo/index.html 里注入） ===== */
/* 表单不参与演示：填了也不会重算，留着只会骗人。内部仍用它的值传参。 */
.formwrap #form{display:none}

.demo{margin:0 0 26px;padding:17px 20px 19px;border-radius:14px;background:#111113;
  border:1px solid #232326;border-left:3px solid var(--gold,#FFC72C)}
@media(max-width:520px){.demo{padding:15px 15px 16px}}
.demo-hd{font-size:12.5px;line-height:1.75;color:#B9B9C0}
.demo-hd b{color:#EDEDF0;font-weight:600}
.demo-tag{display:inline-block;padding:1px 7px;margin-right:8px;border-radius:4px;
  background:rgba(255,199,44,.14);color:#FFC72C;font-size:11px;font-weight:700;
  letter-spacing:1.2px;vertical-align:1px}

/* 显式 minmax(0,1fr)：默认 auto 列会被长串数字撑破，窄屏直接溢出 */
.demo-sc{display:grid;gap:9px;margin-top:16px;grid-template-columns:minmax(0,1fr)}
@media(min-width:640px){.demo-sc{grid-template-columns:repeat(3,minmax(0,1fr))}}
.demo-sc a{position:relative;display:flex;flex-direction:column;min-width:0;
  padding:14px 15px 15px;border-radius:11px;
  border:1px solid #2A2A2F;background:#151517;text-decoration:none;transition:.15s}
.demo-sc a:hover{border-color:#4A4A52;background:#191A1D}
.demo-sc a.on{border-color:rgba(255,199,44,.5);background:rgba(255,199,44,.055)}
.sc-id{display:inline-block;font-size:10px;letter-spacing:1.6px;font-weight:700;color:#71717A}
.demo-sc a.on .sc-id{color:#FFC72C}
.sc-row{display:flex;align-items:baseline;gap:7px;margin-top:7px}
.sc-t{flex:1;min-width:0;font-size:14px;font-weight:700;color:#F2F2F4;line-height:1.45}
.sc-c{display:block;margin-top:6px;font-size:11px;color:#7E7E88;line-height:1.5}
.sc-w{display:block;margin-top:auto;padding-top:11px;border-top:1px solid #232327;
  font-size:11.5px;color:#A6A6B0;line-height:1.65}
.sc-c{margin-bottom:11px}
.sc-go{flex:0 0 auto;font-size:12px;font-style:normal;color:#55555D;transition:.15s}
.demo-sc a:hover .sc-go,.demo-sc a.on .sc-go{color:#FFC72C}

.demo-ft{margin-top:14px;padding-top:13px;border-top:1px solid #1F1F22;
  font-size:11.5px;line-height:1.8;color:#6C6C74}
.demo-ft b{color:#9A9AA2;font-weight:400}

/* 结果页顶部的小标：从链接直接进来的人，也得知道这是回放 */
.demo-mini{display:flex;align-items:center;gap:7px;margin:16px 0 12px;
  font-size:11.5px;color:#8E8E96;letter-spacing:.3px}
.demo-mini i{width:5px;height:5px;border-radius:50%;flex:0 0 5px;
  background:#FFC72C;box-shadow:0 0 7px rgba(255,199,44,.75)}
.demo-mini b{color:#C9C9D0;font-weight:600}

/* 认不出条件时的提示 */
.demo-toast{position:fixed;left:50%;bottom:30px;transform:translate(-50%,12px);opacity:0;
  padding:10px 18px;border-radius:999px;background:#1B1B1F;border:1px solid #33333B;
  color:#E8E8EC;font-size:13px;z-index:99;pointer-events:none;transition:.22s;
  box-shadow:0 8px 24px rgba(0,0,0,.45);max-width:88vw;text-align:center;line-height:1.5}
.demo-toast.on{opacity:1;transform:translate(-50%,0)}
"""


def build():
    with open(SRC, encoding="utf-8") as f:
        html = f.read()

    snaps = load_snaps()

    # ---------------- 场景卡 ----------------
    cards, echos = [], {}
    for i, sc in enumerate(SCENARIOS):
        snap = snaps[sc["id"]]
        echo = snap.get("echo") or {}
        echos[sc["id"]] = echo
        cls = " class=\"on\"" if i == 0 else ""
        cards.append(
            '<a%s data-sid="%s" href="%s">'
            '<span class="sc-id">场景 %s</span>'
            '<span class="sc-row"><b class="sc-t">%s</b><i class="sc-go">→</i></span>'
            '<span class="sc-c">%s</span>'
            '<span class="sc-w">%s</span>'
            '</a>' % (cls, sc["id"], qs(echo), sc["id"], sc["title"],
                      cond_line(snap, sc["place"]), see_line(sc, snap)))

    hd = (
        '<div class="demo-hd"><span class="demo-tag">演示</span>'
        '<b>这不是在线服务，是 3 份真实运行的录像。</b>'
        '真门店、真价格、真耗时，我在本机跑完就原样存下来了。'
        '所以你<b>不用申请任何 Token</b>，但也没法拿它算你自己的地址。'
        '</div>'
    )
    ft = (
        '<div class="demo-ft">'
        '点一张卡 → 看那一份。三份条件都锁死了，改不了，也不会拿别的条件糊弄你。<br>'
        '价格是那一单的真实成交价（含打包费、运费，也给了人均）；换门店、换时段会变，以你 App 里为准。<br>'
        '想算你自己的地址和时间：<b>按 README 在本地跑一次，只要麦当劳 MCP Token，高德可选。</b>'
        '</div>'
    )
    bar = ('<div class="demo">' + hd +
           '<div class="demo-sc">' + "".join(cards) + '</div>' + ft + '</div>')

    # 插到 hero 之下 —— 先看到品牌，再看到「这是演示」
    anchor = '<div class="screen formwrap">'
    assert anchor in html, "找不到表单容器"
    html = html.replace(anchor, anchor + "\n" + bar, 1)

    # 结果页也要有「这是回放」的标记
    res = '<main class="screen" id="screen-result" hidden>'
    assert res in html, "找不到第二屏容器"
    html = html.replace(
        res,
        res + '\n<div class="demo-mini"><i></i>'
              '<span id="demoMini">演示模式 · <b>场景 A</b> 的真实快照回放，未实时查价</span></div>', 1)

    # ---------------- 样式 ----------------
    style = "<style>%s</style>\n" % DEMO_CSS
    close = html.rindex("</head>", 0, html.index("<body"))
    html = html[:close] + style + html[close:]

    # ---------------- 数据 + 接管 ----------------
    blob = json.dumps(snaps, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    eblob = json.dumps(echos, ensure_ascii=False, separators=(",", ":"))
    boot = """<script>
/* 演示模式：把真实运行快照喂给页面，不发起任何网络请求。 */
window.__DEMO__ = true;
window.__DEMO_SNAP__ = %s;
window.__DEMO_ECHO__ = %s;
window.__DEMO_SID__ = 'A';

/* 表单是隐藏的，只用来传参；地址簿给个空壳，别让启动逻辑报错 */
window.demoAddresses = function(){
  return Promise.resolve({ ok: true, addresses: [] });
};

/* 严格对条件：对不上就是没有，绝不拿 A 顶替。
   以前是「没填偏好就给 A」—— 你填武汉 5 人，它给你泰州 3 人，
   顶上还照你的条件写 chips，那是在骗人。 */
window.demoPlan = function(req){
  var echos = window.__DEMO_ECHO__, sid = null;
  ['A', 'B', 'C'].forEach(function(k){
    if(sid || !echos[k]) return;
    var e = echos[k];
    var same = String(e.people) === String(req.people)
      && String(e.when || '') === String(req.when || '')
      && String(e.form || '') === String(req.form || '')
      && String(e.taste || '') === String(req.taste || '')
      && String(e.from_addr || '') === String(req.from_addr || '')
      && String(e.to_addr || '') === String(req.to_addr || '')
      && String(e.budget ? Math.round(e.budget) : '') === String(req.budget || '');
    if(same) sid = k;
  });
  if(sid) window.__DEMO_SID__ = sid;
  syncOn(sid);
  return new Promise(function(res, rej){
    setTimeout(function(){
      if(sid && window.__DEMO_SNAP__[sid]) res(window.__DEMO_SNAP__[sid]);
      else rej('演示版只有 3 份跑好的结果（场景 A/B/C），不会拿别的条件凑一份给你。点「← 换场景」回上一屏挑一张卡就看得到。想算你自己的地址和时间：按 README 本地运行。');
    }, 420);
  });
};

/* 按钮高亮跟着实际回放的那份走 */
function syncOn(sid){
  document.querySelectorAll('.demo-sc a').forEach(function(a){
    a.classList.toggle('on', a.getAttribute('data-sid') === sid);
  });
  var t = document.getElementById('demoMini');
  if(t) t.innerHTML = sid
    ? ('演示模式 · <b>场景 ' + sid + '</b> 的真实快照回放，未实时查价')
    : '演示模式 · 认不出这组条件，下面不是有效结果';
}

/* 把某个场景的条件填进（隐藏的）表单，再走原来的提交流程 */
function fillScene(sid){
  var e = window.__DEMO_ECHO__[sid];
  if(!e || !window.state) return false;
  var set = function(id, v){ var el = document.getElementById(id); if(el) el.value = v == null ? '' : v; };
  set('addr', e.from_addr);
  set('drop', e.to_addr);
  set('when', e.when);
  set('taste', e.taste);
  set('budget', e.budget ? String(Math.round(e.budget)) : '');
  window.state.people = e.people;
  window.state.form = e.form;
  var num = document.getElementById('num');
  if(num) num.textContent = e.people;
  document.querySelectorAll('#segForm .seg').forEach(function(b){
    b.classList.toggle('on', b.dataset.val === e.form);
  });
  if(window.updWhen) window.updWhen();
  if(window.updateRelState) window.updateRelState();
  return true;
}

function goScene(sid){
  if(!fillScene(sid)) return;
  /* URL 同步一下，方便单独分享某一个场景 */
  try{
    var e = window.__DEMO_ECHO__[sid];
    var p = new URLSearchParams({from:e.from_addr||'', to:e.to_addr||'', when:e.when||'',
      people:e.people||3, form:e.form||'dinein', taste:e.taste||'', s:sid});
    history.replaceState(null, '', '?' + p.toString());
  }catch(err){}
  document.getElementById('form').dispatchEvent(new Event('submit', {cancelable:true}));
}

document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.demo-sc a').forEach(function(a){
    a.addEventListener('click', function(ev){
      if(ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1) return; // 新窗口照常
      ev.preventDefault();
      goScene(a.getAttribute('data-sid'));
    });
  });
  /* 深链直接进来（?s=B&go=1）：把卡片高亮对上 */
  var q = new URLSearchParams(location.search);
  syncOn(q.get('s') || 'A');
});
</script>
""" % (blob, eblob)

    head_script = html.index("<script>")
    html = html[:head_script] + boot + html[head_script:]

    # ---------------- 文案对齐：别让演示页说自己是实时 ----------------
    reps = [
        ("填五件事，剩下的账我们来算。",
         "三个真实案例，点开看这笔账怎么算。"),
        ("实时查价、实时算路，通常 5 秒内出结果",
         "回放真实运行快照 · 约半秒出结果"),
        ('<button class="back" id="back">← 改条件</button>',
         '<button class="back" id="back">← 换场景</button>'),
        ("'<div class=\"warn\"><b>请求失败</b>　' + esc(err) +\n      '　·　确认本地服务还在跑（终端那个窗口没关）。</div>'",
         "'<div class=\"warn\"><b>演示版没有这一份</b>　' + esc(err) + '</div>'"),
    ]
    for a, b in reps:
        if a in html:
            html = html.replace(a, b)
        else:
            print("  ! 文案没匹配上（跳过）：%s" % a[:24])

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("  -> %s  %.0f KB" % (OUT, len(html.encode()) / 1024.0))


if __name__ == "__main__":
    print("生成演示页：")
    build()
