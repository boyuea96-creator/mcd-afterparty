#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine.py —— 麦麦散场局的决策计算

一句话：进来是一张表单，出去是两套账（堂食 / 外卖），每套都是真价、真路线。

这个文件不做任何写入动作。允许的工具只有查询类和 `calculate-price`：
下单、取消、领券、建地址、抽奖、兑换、团餐下单 —— 全部禁用。

出参结构与 app/index.html 的渲染字段一一对应，改这里就要改那边。
"""

import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

import mcpc

WEEK = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
CARD_WITH = {"membershipCode": "106", "membershipSpecId": "880"}   # 点餐页随单购麦金卡（¥9.90 / 15 天）
CARD_FEE = 9.90
# 一次计算一个上下文：散场时间在未来时，要用它作为 reservationDate 去查。
# 注意：这里不能用 threading.local —— 计算过程会派发到线程池，
# 子线程读不到主线程的 local。所以用模块级变量，并在 server 侧保证一次只算一单。
_STATE = {"resv": None}


def _resv():
    return _STATE.get("resv")


def set_resv(v):
    _STATE["resv"] = v


def _call(name, **args):
    """带 reservationDate 先试；接口不认或时间不合法，就退回不带的再试一次。"""
    r = _resv()
    if r:
        try:
            return mcpc.mcd_call(name, reservationDate=r, **args)
        except Exception:
            pass
    return mcpc.mcd_call(name, **args)
BIKE_PER_LEG = 1.5         # 共享单车/电瓶车 每人每段估价


# ==========================================================================
# 基础
# ==========================================================================
def _num(x, d=0.0):
    try:
        if x is None:
            return d
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).replace("¥", "").replace(",", "").strip()
        return float(s) if s else d
    except Exception:
        return d


def _min(sec):
    """不足 1 分钟也说 1 分钟 —— 显示「0 分」会被读成算错了。"""
    if not sec:
        return 0
    return max(1, int(round(sec / 60.0)))


# 备注尾巴：「…酒店前台」这种后半截是配送说明，不是地址
_NOTE_RE = re.compile(r"(放前台|放到前台|送前台|前台|大堂|正门|门口|"
                      r"电话联系|打电话|联系我|到酒店|备注)")
# 行政前缀，按 省 → 市 → 区/县 逐级剥。剥完还长，才动「路名+门牌」。
_ADMIN_STEPS = (r"^.*?(?:省|自治区|特别行政区)", r"^.*?(?:市|自治州|地区|盟)",
                r"^.*?(?:区|县|旗)")


def _short_addr(text, keep=14):
    """地址压成一句能念出来的话。

    不能从中间裁字 —— 「浙江省…希尔顿欢朋酒店前台」按尾部截 14 个字会变成
    「尔顿欢朋酒店前台」，酒店名少一个字，等于把别人地址写错了。
    顺序是：先切备注尾巴 → 再剥省市区的行政前缀 → 最后才留楼名。
    """
    t = re.sub(r"\s+", "", text or "")
    t = _NOTE_RE.split(t)[0]
    if len(t) > keep:
        for rx in _ADMIN_STEPS:
            t2 = re.sub(rx, "", t)
            if len(t2) < len(t):
                t = t2
            if len(t) <= keep:
                break
    # 「黎黄陂路黎黄陂路历史文旅街区」：App 里存的地址常把路名写两遍。
    # 必须在剥完行政前缀之后做 —— 锚在行首，「湖北省…」里那个重复根本匹配不到。
    t = re.sub(r"^(.{2,8}?)\1", r"\1", t)
    if len(t) <= keep:
        return t
    # 只剩「路名+门牌+楼名」：楼名自己就能说清是哪儿，门牌反而次要
    m = re.search(r"\d+号(.+)$", t)
    if m and 0 < len(m.group(1)) <= keep:
        return m.group(1)
    return t[-keep:]


def _poi_of(addr):
    """「富城路33号浦东香格里拉」→「浦东香格里拉」。

    「送到哪」说的是店名，不是门牌 —— 从尾部截 8 个字会截出「3号浦东香格里拉」，
    读起来像地址没打完。
    """
    t = addr or ""
    m = re.search(r"\d+号(.+)$", t)
    if m and len(m.group(1)) >= 4:
        return m.group(1)
    return t


def _money(v):
    return "¥%.2f" % v if abs(v - round(v)) > 0.004 else "¥%d" % round(v)


def _sim(a, b):
    a, b = a or "", b or ""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _addr_variants(addr, city):
    """门店地址经常带一堆楼层铺位，直接查查不出来。按精度从高到低给几个写法。"""
    raw = (addr or "").strip()
    if not raw:
        return []
    pre = "" if (not city or city in raw) else city
    out = [pre + raw]

    a1 = re.sub(r"[（(][^)）]*[)）]", "", raw).split("、")[0]
    a1 = re.sub(r"(一层|二层|三层|四层|五层|负一层|负二层|B1|B2|F\d+|商铺|铺位).*$", "", a1).strip()
    if a1 and a1 != raw:
        out.append((("" if city in a1 else city) + a1) if city else a1)

    m = re.match(r"([\u4e00-\u9fa5]{2,8}(?:路|街|大道|道|巷|广场|大厦))", raw)
    if m:
        out.append(city + m.group(1) if city else m.group(1))

    seen, res = set(), []
    for x in out:
        x = x.strip()
        if x and x not in seen:
            seen.add(x)
            res.append(x)
    return res


# ==========================================================================
# 券：两个工具返回 markdown，得自己解析
# ==========================================================================
def parse_my_coupons(text):
    out, cur = [], None
    for line in (text or "").splitlines():
        line = line.rstrip()
        m = re.match(r"^#{2,3}\s*(.+?)\s*$", line)
        if m:
            cur = {"title": m.group(1), "fields": {}}
            out.append(cur)
            continue
        m = re.match(r"^\s*-\s*\*\*(.+?)\*\*\s*[:：]\s*(.+?)\s*$", line)
        if m and cur is not None:
            cur["fields"][m.group(1)] = m.group(2)
    return out


def parse_available(text):
    out, cur = [], None
    for line in (text or "").splitlines():
        s = line.strip().rstrip("\\").strip()
        m = re.match(r"^-\s*优惠券标题\s*[:：]\s*(.+)$", s)
        if m:
            cur = {"title": m.group(1).strip()}
            out.append(cur)
            continue
        m = re.match(r"^状态\s*[:：]\s*(.+)$", s)
        if m and cur is not None:
            cur["state"] = m.group(1).strip()
    return out


def coupon_usable_now(c, now_dt):
    """三层校验：星期 → 日期区间 → 时段。返回 (能不能用, 原因)。"""
    v = (c.get("fields") or {}).get("有效期") or ""
    if not v:
        return True, ""

    days = re.findall(r"周([一二三四五六日])", v)
    if days:
        today = WEEK[now_dt.isoweekday()]
        if today not in ["周" + d for d in days]:
            return False, "限" + "、".join("周" + d for d in days)

    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:~|至|—|-)\s*(\d{4}-\d{2}-\d{2})", v)
    if m:
        today = now_dt.strftime("%Y-%m-%d")
        if not (m.group(1) <= today <= m.group(2)):
            return False, "不在有效期内"

    hms = re.findall(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", v)
    if hms:
        s, e = hms[-1]
        cur = now_dt.strftime("%H:%M")
        if s <= e:
            if not (s <= cur <= e):
                return False, "只在 %s-%s 能用" % (s, e)
        else:  # 跨零点
            if not (cur >= s or cur <= e):
                return False, "只在 %s-%s 能用" % (s, e)
    return True, ""


def coupon_block(store_code, order_type, be_type, be_code, now_dt):
    """券的三源：卡包 / 麦麦省可领 / 该店该时段已领可用。三个源并发拉。"""
    def _mine():
        try:
            return parse_my_coupons(mcpc.mcd_text("query-my-coupons"))
        except Exception:
            return []

    def _claim():
        try:
            return [c for c in parse_available(mcpc.mcd_text("available-coupons"))
                    if "可领取" in (c.get("state") or "")]
        except Exception:
            return []

    def _at_store():
        try:
            args = {"orderType": order_type, "beType": be_type, "storeCode": store_code}
            if be_code:
                args["beCode"] = be_code
            j = _call("query-store-coupons", **args)
            return j.get("data") or []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=3) as ex:
        f1, f2, f3 = ex.submit(_mine), ex.submit(_claim), ex.submit(_at_store)
        mine, claim, at_store = f1.result(), f2.result(), f3.result()

    ok_titles, blocked = [], {}
    for c in mine:
        good, why = coupon_usable_now(c, now_dt)
        if good:
            ok_titles.append(c.get("title"))
        else:
            blocked[why] = blocked.get(why, 0) + 1

    usable = [{"title": (x.get("title") or "").strip()} for x in at_store if x.get("title")]

    if mine:
        head = "卡包现有 %d 张券" % len(mine)
        if claim:
            head += "，麦麦省还有 %d 张可领" % len(claim)
        head += "。"
    else:
        head = "卡包没有券。"
    detail = ""
    if blocked:
        detail = "其中 " + "、".join("%d 张%s" % (v, k) for k, v in blocked.items()) + "，这会儿用不上。"
    if usable:
        detail = (detail + " " if detail else "") + "这家店这会儿能用的：%s。" % "、".join(
            u["title"] for u in usable[:3])
    if claim:
        detail += "可领券领之前看不出能不能用——先领再复查，别按它算钱。"
    if not usable:
        detail = (detail + " " if detail else "") + "所以下面这些价，一张券都没算进去。"

    return {"mine": len(mine), "claimable": len(claim),
            "at_store": [u["title"] for u in usable[:4]],
            "head": head, "detail": detail.strip()}


# ==========================================================================
# 门店定位：任意地址 → storeCode
# ==========================================================================
BAD_POI = ("甜品站", "麦咖啡", "McCafe", "MCCAFE", "汽车餐厅")


def _kw_candidates(origin, radius=4000):
    """找店用的关键词 ＋ POI 坐标表。

    麦当劳的 query-nearby-stores 只吃 city + keyword —— keyword 写街道名
    反而比写「麦当劳(XX店)」更稳：前者能带回一整片门店，后者只撞一家。
    顺带把高德给的 POI 坐标留下，后面门店经纬度全是 0 的时候要用它兜底。
    """
    kws, poi_locs = [], {}
    for k in (origin.get("street"), origin.get("district")):
        if k:
            kws.append(k)
    for p in mcpc.around(origin["loc"], "麦当劳", radius):
        nm = (p.get("name") or "").strip()
        loc = p.get("location") or ""
        if loc and not loc.startswith("0"):
            poi_locs[nm] = loc
        if nm and not any(b in nm for b in BAD_POI):
            kws.append(nm)

    out, seen = [], set()
    for k in kws:
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:5], poi_locs


def find_store(origin, be_type=1, need_menu=True):
    """任意地址 → 一家「最近的、此刻真能点单的」门店。

    三步，全部并发：
      1. 多关键词捞候选门店（街道词 ＋ POI 名）
      2. 定门店坐标 —— 先用 POI 坐标，再用门店地址反查，都失败就排后面
      3. 按步行距离排队逐个验菜单 —— 有的关门了、有的不支持到店自取
    """
    city = re.sub(r"(市|地区|自治州|盟)$", "", origin.get("city") or "")
    kws, poi_locs = _kw_candidates(origin)
    if not kws:
        return None

    def probe_kw(kw):
        r = mcpc.mcd_try("query-nearby-stores", beType=be_type, searchType=2,
                         city=city, keyword=kw)
        return (r.get("data") or []) if isinstance(r, dict) else []

    with ThreadPoolExecutor(max_workers=5) as ex:
        lists = list(ex.map(probe_kw, kws))

    pool = {}
    for stores in lists:
        for st in stores:
            code = st.get("storeCode")
            if not code or code in pool:
                continue
            pool[code] = {"code": code, "beCode": st.get("beCode"),
                          "name": st.get("storeName") or "",
                          "address": st.get("address") or "",
                          "api_dist": st.get("distance") or 9999,
                          "resv": st.get("reservationTimeOptions") or []}
    if not pool:
        return None

    # 接口距离只能用来「圈范围」，绝不能用来排序：
    # 它跟当前地址不是同一个参照系（实测：真实 6.2 km 的店，接口只报 687 米）。
    # 所以这里放宽到前 10 家，再把「名字能对上门口那家」的一律补进来。
    cands = sorted(pool.values(), key=lambda x: x["api_dist"])[:10]
    taken = {c["code"] for c in cands}
    for c in pool.values():
        if c["code"] in taken:
            continue
        if any(_sim(c["name"], nm) >= 0.6 for nm in poi_locs):
            cands.append(c)
            taken.add(c["code"])
    full_city = origin.get("city") or ""

    def place(c):
        loc = None
        # ① POI 名高度吻合 → 直接用 POI 坐标
        best_sim, best_loc = 0.0, None
        for nm, l in poi_locs.items():
            sim = _sim(c["name"], nm)
            if sim > best_sim:
                best_sim, best_loc = sim, l
        if best_sim >= 0.55:
            loc = best_loc
        # ② 门店地址按精度从高到低试几种写法
        if not loc:
            for v in _addr_variants(c["address"], full_city):
                g = mcpc.geocode(v)
                if g:
                    loc = g["loc"]
                    break
        if loc:
            w = mcpc.route_walking(origin["loc"], loc)
            c["loc"] = loc
            c["km"] = round(mcpc.haversine_m(origin["loc"], loc) / 1000.0, 2)
            c["walk_sec"] = w["sec"] if w else None
        return c

    with ThreadPoolExecutor(max_workers=8) as ex:
        placed = list(ex.map(place, cands))

    def rank(r):
        # 只认自己算出来的距离，三种情况按可信度降级：
        #   ① 有真实步行路径 → 用步行秒数
        #   ② 有坐标但没拿到路径 → 用直线距离折算（1.2 m/s 假设偏保守）
        #   ③ 连坐标都没有 → 直接排最后。**不要拿接口距离来凑**，
        #      它和当前地址不是一个参照系，混着比会把 6 公里的店排到 500 米前头。
        if r.get("walk_sec"):
            return r["walk_sec"]
        if r.get("km") is not None:
            return r["km"] * 1000.0 / 1.2
        return 9e9

    placed.sort(key=rank)
    if not need_menu:
        return placed[0]

    def verify(st):
        try:
            j = _call("query-meals", storeCode=st["code"], orderType=1, beType=be_type)
            m = (j.get("data") or {}).get("meals") or {}
            if m:
                st["meals"] = m
                return st
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        ok = [x for x in ex.map(verify, placed[:6]) if x]
    if not ok:
        return None
    ok.sort(key=rank)
    return ok[0]


def slot_ok(resv_opts, dt):
    """门店这个点儿在卖哪一档：夜市 / 早餐 / 午餐 / 下午茶。

    返回 (认不认得这一天的排班, 档口名)。认不得就返回 (False, '')，不当成「打烊」。
    """
    if not resv_opts or not dt:
        return False, ""
    day = dt.strftime("%Y-%m-%d")
    hhmm = dt.strftime("%H:%M")
    for o in resv_opts:
        if (o.get("date") or "") != day:
            continue
        txt = o.get("reservationOptionText") or ""
        for name, spans in re.findall(r"([^，,()（）]+)\(([^)]*)\)", txt):
            for s, e in re.findall(r"(\d{1,2}:\d{2})至(\d{1,2}:\d{2})", spans):
                inside = (s <= hhmm <= e) if s <= e else (hhmm >= s or hhmm <= e)
                if inside:
                    return True, name.strip()
        return True, ""
    return False, ""


def match_address(addresses, target):
    """外送只认 App 里已存的地址（建地址工具是禁用的）。"""
    if not addresses:
        return None
    t = re.sub(r"\s+", "", target or "")
    if not t:
        return None
    best, score = None, 0.0
    for a in addresses:
        full = re.sub(r"\s+", "", a.get("fullAddress") or "")
        s = 0.0
        if t and t in full:
            s = 1.0
        else:
            for n in (8, 6, 4):
                for i in range(max(len(t) - n + 1, 1)):
                    if t[i:i + n] and t[i:i + n] in full:
                        s = max(s, n / 10.0)
                        break
                if s:
                    break
        if s > score:
            best, score = a, s
    return best if score >= 0.4 else None


# ==========================================================================
# 菜单 → 候选买法
# ==========================================================================
# ==========================================================================
# 偏好 / 忌口
# 这一套必须真的进「买法」候选，不能只活在最后那句话里。
# 用户填了「想吃麦辣鸡翅」，方案里就得有麦辣鸡翅；没有就得当场说没有。
# ==========================================================================
TASTE_SEP = re.compile(r"[/／,，、;；\n]+")
# 否定词只认固定搭配。裸的「无」「别」「少」会误伤商品名：
# 「巨无霸」里的「无」、「特别」里的「别」都不是忌口。
# 一个词被错判成忌口，用户点名要的东西就会从方案里消失 —— 这是最气人的一种错。
_NEG_RE = re.compile(
    r"(?:不(?:吃|喝|想|要|能|用|放|加|来|配)|别(?:放|加|给|要|太|整)|忌口|忌|过敏|"
    r"无(?:糖|冰|辣|奶|芝|肉|油|盐|咖啡|麦麸|麸))")
_TASTE_NOISE = re.compile(
    r"(想吃|想要|想喝|想点|要一份|来一份|来点|来一个|点一份|点个|一份|一个|一点|"
    r"特别想|很想|超想|好想|特别|有点|比较|稍微|"
    r"控卡|低卡|少油|减脂|减肥|那种|这种|就行|就好|可以|谢谢|的|了|吧|呀|啊)")

# 「无糖」「去冰」是点单规格，不是忌口 —— 判否定前先摘掉，不然「无糖可乐」会被当成不吃可乐
_SPEC_WORDS = re.compile(r"(无糖|少糖|无冰|少冰|去冰|常温|不加糖|少盐|少油)")
_LEAD_NEG = re.compile(r"^(?:不能|不要|不想|不吃|不喝|不|别(?:放|加|给|要)|勿|忌|无(?:糖|冰|辣|奶|芝|肉|油|盐|咖啡))+")
# 「特别想吃板烧鸡腿堡」剥掉「特别想」后还剩个动词「吃」
_LEAD_VERB = re.compile(r"^(?:吃|喝|要|来|点|整|搞)+")

# 忌口常说的属性词：从「不想吃冰的冷的」里拆出「冰」「冷」
_TASTE_ATTR = ("冰", "冷", "凉", "辣", "甜", "油炸", "炸", "牛肉", "猪肉", "鸡肉",
               "奶", "芝士", "咖啡", "碳酸", "花生", "虾", "蟹", "香菜", "洋葱")

# 商品名里的规格噪音：去掉才比得准（麦辣鸡翅(2块) ≡ 麦辣鸡翅）
_NAME_NOISE = re.compile(
    r"[（(][^)）]*[)）]|[×xX]\d+|[\d一二三四五六七八九十]+块装|[\d一二三四五六七八九十]+块装?"
    r"|大份|中份|小份|套餐价")


def parse_taste(taste):
    """「想吃麦辣鸡翅 / 不想吃冰的冷的」→ {'want': [...], 'avoid': [...]}

    宁可少解析也不要解析错：抽不出东西的段直接丢掉，
    方案那边就当「没提偏好」算，不会拿半截词去乱匹配。
    """
    out = {"want": [], "avoid": [], "diet": []}
    for seg in TASTE_SEP.split(taste or ""):
        seg = seg.strip()
        if not seg:
            continue
        if re.search(r"控卡|低卡|少油|减脂|减肥", seg):
            out["diet"].append("控卡")
        if "素" in seg:
            out["diet"].append("素食")
        neg = bool(_NEG_RE.search(_SPEC_WORDS.sub("", seg)))
        body = _LEAD_VERB.sub("", _LEAD_NEG.sub("", _TASTE_NOISE.sub("", _SPEC_WORDS.sub("", seg))))
        body = re.sub(r"[\s。!！?？]+", "", body)
        if neg:
            for w in _TASTE_ATTR:
                if w in seg:
                    out["avoid"].append(w)
            # 「冰冷」这种纯属性拼接不用再单列，「冰」「冷」已经进去了
            _attr_chars = set("".join(_TASTE_ATTR))
            if len(body) >= 2 and (body in _TASTE_ATTR or not set(body) <= _attr_chars):
                out["avoid"].append(body)
        elif len(body) >= 2:
            out["want"].append(body)
    for k in out:
        seen, uniq = set(), []
        for w in out[k]:
            if w and w not in seen:
                seen.add(w)
                uniq.append(w)
        out[k] = uniq
    return out


def _base_name(n):
    return _NAME_NOISE.sub("", n or "").replace(" ", "").replace("·", "").strip()


def taste_hit(name, word):
    """商品名里有没有这个东西。

    关键：**末尾两字必须对上**。「麦辣鸡翅」和「麦辣鸡腿汉堡」共用「麦辣」「辣鸡」，
    只按 2-gram 相似度会误判成命中 —— 而用户点名要鸡翅时给个汉堡是最气人的。
    中文菜名的尾巴才是品类词（鸡翅 / 汉堡 / 可乐），前置的都是修饰。
    """
    a, b = _base_name(name), _base_name(word)
    if not a or not b:
        return False
    if b in a or a in b:
        return True
    if len(b) < 2:
        return False
    if b[-2:] not in a:
        return False
    grams = [b[i:i + 2] for i in range(len(b) - 1)]
    return sum(1 for g in grams if g in a) / float(len(grams)) >= 0.6


def _all_items(meals):
    out = []
    for code, m in (meals or {}).items():
        nm = (m or {}).get("name") or ""
        pr = _num((m or {}).get("currentPrice"))
        if pr > 0 and is_food(nm):
            out.append({"code": code, "name": nm, "price": pr, "kind": classify(nm)})
    return out


def _savory_side(name):
    """咸口小食（鸡翅、鸡块、薯条…）—— 一人一份也只是打个底，不算一顿饭。"""
    n = name or ""
    if not is_side(n):
        return False
    return not (any(k in n for k in SWEET_HINT) or any(k in n for k in _DRINK_HINT))


def pref_candidates(meals, people, pref):
    """按「想吃」挑商品，产出 1–2 个真含这个的买法。

    顺序有讲究：**先给「就点你要的」那个单品，再给含它的套餐**。
    反过来会出大问题 —— 用户说想吃巨无霸，却先端上一个四件套（人均 ¥59），
    他只会觉得这工具在替他花钱。单品永远是最像他原话的那个。

    但**小食不能这么排**：4 个人点 4 份麦辣鸡翅，就是 8 块鸡翅，
    那不叫一顿饭，叫没吃。这种命中把「加一份主食」放前面，单品退成备选。
    """
    want = (pref or {}).get("want") or []
    if not want:
        return []
    hits, seen = [], set()
    for code, m in (meals or {}).items():
        nm = (m or {}).get("name") or ""
        pr = _num((m or {}).get("currentPrice"))
        if pr <= 0 or not is_food(nm) or nm in seen:
            continue
        got = [w for w in want if taste_hit(nm, w)]
        if not got:
            continue
        seen.add(nm)
        hits.append({"code": code, "name": nm, "price": pr,
                     "kind": classify(nm), "hit": got})
    if not hits:
        return []
    hits.sort(key=lambda x: x["price"])

    # 单品（名字基本就是用户说的那个）和「含它的套餐」分开放
    singles = [h for h in hits if h["kind"] not in ("combo", "qxp", "family")]
    combos = [h for h in hits if h["kind"] in ("combo", "qxp", "family")]

    out = []
    if singles:
        h = singles[0]
        bare = {"label": "%s ×%d" % (h["name"], people), "kind": "pref_single",
                "note": "就点你要的，不加别的",
                "items": [{"productCode": h["code"], "quantity": people}],
                "pref": True, "hit": h["hit"]}
        fill = None
        if combos:
            # 多给一个备选：菜单标价最便宜的套餐，试价后可能反而最贵
            # （四件套标 ¥32、试出人均 ¥59），只有它被剔掉时第二个才顶上来
            c = combos[0]
            fill = {"label": "%s ×%d" % (c["name"], people), "kind": c["kind"],
                    "note": "含%s，一份配齐" % "、".join(c["hit"]),
                    "items": [{"productCode": c["code"], "quantity": people}],
                    "pref": True, "hit": c["hit"]}
        else:
            # 只有单品：补一个能当饭吃的，把「吃不饱」这条路也给出来。
            # 别补另一个甜品 —— 麦旋风配圆筒只是在凑单，不是一顿饭
            pool = [x for x in _all_items(meals)
                    if x["code"] != h["code"] and (is_side(x["name"]) or x["kind"] == "burger")]
            if _savory_side(h["name"]):
                # 命中是小食 → 配主食（堡）比再配一份小食更像一顿饭
                pool.sort(key=lambda x: (0 if x["kind"] == "burger" else 1,
                                         side_rank(x["name"]), x["price"]))
            else:
                pool.sort(key=lambda x: (side_rank(x["name"]), x["price"]))
            mate = pool[0] if pool else None
            if mate:
                fill = {"label": "%s ×%d ＋ %s ×%d" % (h["name"], people, mate["name"], people),
                        "kind": "pref_full", "note": "要的那口 ＋ 一份配的，吃得饱",
                        "items": [{"productCode": h["code"], "quantity": people},
                                  {"productCode": mate["code"], "quantity": people}],
                        "pref": True, "hit": h["hit"]}
        if fill and _savory_side(h["name"]):
            bare["note"] = "只要这口，不加别的"
            out = [fill, bare]
        else:
            out = [bare] + ([fill] if fill else [])
    else:
        for c in combos[:2]:
            out.append({"label": "%s ×%d" % (c["name"], people), "kind": c["kind"],
                        "note": "含%s，一份配齐" % "、".join(c["hit"]),
                        "items": [{"productCode": c["code"], "quantity": people}],
                        "pref": True, "hit": c["hit"]})
    return out


def _dedup_plans(plans):
    """同一个买法只留一份；偏好版排前面，所以留下的是它。"""
    seen, out = set(), []
    for p in plans:
        key = tuple(sorted((it["productCode"], it["quantity"]) for it in (p.get("items") or [])))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def pref_report(pref, plans):
    """把「想要 / 要避开」的兑现情况摊开：命中了什么、没命中什么、哪些方案踩了忌口。"""
    want = (pref or {}).get("want") or []
    avoid = (pref or {}).get("avoid") or []
    rep = {"want": want, "avoid": avoid, "hit": [], "missing": [],
           "avoid_in": [], "all_avoid": False, "empty": not (want or avoid)}
    if rep["empty"]:
        return rep

    def names_of(p):
        return [n for n in (p.get("items_text") or "").split("、") if n]

    all_names = [n for p in plans for n in names_of(p)]
    for w in want:
        if any(taste_hit(n, w) for n in all_names):
            rep["hit"].append(w)
        else:
            rep["missing"].append(w)
    for w in avoid:
        if any(taste_hit(n, w) for n in all_names):
            rep["avoid_in"].append(w)
    if avoid and plans:
        rep["all_avoid"] = all(
            any(any(taste_hit(n, w) for n in names_of(p)) for w in avoid)
            for p in plans)
    return rep


def pref_later(store_code, order_type, be_type, be_code, pref, now_dt):
    """菜单里没命中时，判断是「这个点不供应」还是「这家店就没有」。

    拿明天中午的菜单探一下：有 → 换个时段能吃上；没有 → 这家店没有。
    探不出来（接口失败）就返回 None，不编原因。
    """
    want = (pref or {}).get("want") or []
    if not want:
        return None
    try:
        import datetime as _ddt
        base = now_dt or _ddt.datetime.now()
        probe = (base + _ddt.timedelta(days=1)).strftime("%Y-%m-%d") + " 12:00"
        args = {"storeCode": store_code, "orderType": order_type,
                "beType": be_type, "reservationDate": probe}
        if be_code:
            args["beCode"] = be_code
        j = mcpc.mcd_call("query-meals", **args)
        full = (j.get("data") or {}).get("meals") or {}
    except Exception:
        return None
    names = [(m or {}).get("name") or "" for m in full.values()]
    return any(taste_hit(n, w) for n in names for w in want)


def _trim_pref(plans):
    """偏好方案里，「配齐」那套只该是稍微加点钱。

    菜单标价和试价后的实际价不是一回事：巨无霸单品 ¥26.5，四件套标 ¥32，
    但试出来人均 ¥59 —— 差了 2 倍多，这不叫配齐，这叫另一个单。
    留最便宜的那套，其余超过 1.8 倍的不摆，免得用户以为工具在替他花钱。

    例外：`pref_full`（自己按最小加成配的「单品＋一份咸口」）不参与这个限制，
    它本来就是最小可有的一套，砍了就只剩光秃秃一个单品。
    """
    got = [p for p in plans if p.get("pref")]
    if len(got) <= 1:
        return got
    base = min(p["price"] for p in got)
    return [p for p in got
            if p["price"] <= base * 1.8 or p.get("kind") == "pref_full"]


def tag_plans(plans, people, pref, meals, way="dinein"):
    """给每个买法打标签。有偏好时，偏好的那套排最前、叫「你要的」，
    最便宜的兜底那套叫「兜底」，不再抢「全场最低」的名头。"""
    if not plans:
        return plans
    lowest_plan = min(plans, key=lambda x: x["price"])
    lowest = lowest_plan["price"]
    has_pref = any(p.get("pref") for p in plans)
    want = (pref or {}).get("want") or []
    missing = bool(want) and not has_pref

    for p in plans:
        p["per"] = round(p["price"] / people, 2)
        p["tags"] = []
        p["items_text"] = "、".join(_item_name(meals, it) for it in p["items"])
        if p.get("pref"):
            p["tags"].append({"t": "含" + "、".join(p.get("hit") or want), "k": "best"})
            p["tags"].append(temp_tag(p["items_text"].split("、"), way))
        elif missing and p is lowest_plan:
            p["tags"].append({"t": "兜底 · 没你要的那口", "k": "gray"})
        elif p is lowest_plan:
            p["tags"].append({"t": "最便宜", "k": "best"})
        elif p["kind"] == "family":
            p["tags"].append({"t": "适合分食", "k": "gray"})
        else:
            p["tags"].append({"t": "贵 %s" % _money(p["price"] - lowest), "k": "gray"})
    return plans


def classify(name):
    n = name or ""
    if "件套" in n:
        return "combo"
    if re.search(r"随心配|1\s*[+＋]\s*1", n):
        return "qxp"
    if re.search(r"[双两二三四五六七]人餐|全家|欢聚|聚会|分享|桶", n):
        return "family"
    if "堡" in n:
        return "burger"
    return "other"


# 不是「能当饭吃」的东西：酱料、周边、玩具、卡券、包装
NOT_FOOD = ("酱", "周边", "玩具", "手办", "毛绒", "抱枕", "盲盒", "贴纸",
            "礼盒", "购物袋", "环保袋", "券", "卡", "赠品", "集卡", "徽章", "杯套")
# 能算「小食/饮料」的：咸口优先，甜品排后面 —— 派对一个人不顶饱
SIDE_HINT = ("薯条", "鸡块", "鸡翅", "鸡柳", "鸡排", "玉米", "沙拉", "蔬菜",
             "可乐", "雪碧", "红茶", "咖啡", "奶茶", "果汁", "水", "饮",
             "冰淇淋", "新地", "圣代", "麦旋风", "圆筒", "薯饼", "洋葱圈", "爆米花", "派")
SWEET_HINT = ("冰淇淋", "新地", "圣代", "麦旋风", "圆筒", "派", "甜")

# ---------------------------------------------------------------- 温度口径
# 「现做 · 到嘴还是烫的」只对热食成立。麦旋风、新地是冰品，说「烫」不是热情是撒谎。
# 一句话和标签一旦被用户抓到一次假的，整份方案的可信度就没了。
_COLD_HINT = ("冰淇淋", "麦旋风", "新地", "圣代", "圆筒", "冰沙", "雪冰", "冰饮",
              "冰红茶", "冰可乐", "冰咖啡", "奶昔", "冻", "冰")
_HOT_HINT = ("堡", "鸡翅", "鸡块", "鸡柳", "鸡排", "薯条", "薯饼", "派", "油条",
             "咖啡", "红茶", "粥", "汤", "玉米", "洋葱圈", "薯角", "麦满分")
# 饮料单独拎出来：可乐雪碧既不是热的也不是冰的（看加不加冰），不参与温度判断
_DRINK_HINT = ("可乐", "雪碧", "红茶", "咖啡", "奶茶", "果汁", "矿泉水", "饮用水", "饮")


def temp_of(names):
    """这一单主要是冰的还是热的 → 'cold' / 'hot' / 'mix' / ''（拿不准就不写）

    「巨无霸」这个名字里没有「堡」字，光靠关键词会漏 —— 所以补一条兜底：
    只要是能吃的、又不是饮料，就当热食。真正会凉的是炸物和堡，不是常温饮料。
    """
    cold = hot = 0
    for n in (names or []):
        n = n or ""
        if not n:
            continue
        if any(k in n for k in _COLD_HINT):
            cold += 1
        elif any(k in n for k in _HOT_HINT):
            hot += 1
        elif is_food(n) and not any(k in n for k in _DRINK_HINT):
            hot += 1
    if cold and hot:
        return "mix"
    if cold:
        return "cold"
    if hot:
        return "hot"
    return ""


def temp_tag(names, way="dinein"):
    """「现做」在这一单上到底兑现成什么。堂食说口感，外卖说折损。"""
    t = temp_of(names)
    if way == "dinein":
        if t == "hot":
            return {"t": "现做 · 到嘴还是烫的", "k": "hot"}
        if t == "cold":
            return {"t": "现做 · 到手还没化", "k": "hot"}
        if t == "mix":
            return {"t": "现做 · 一冷一热", "k": "hot"}
        return {"t": "现做", "k": "hot"}
    if t == "hot":
        return {"t": "现做 · 脆的送到会软", "k": "gap"}
    if t == "cold":
        return {"t": "现做 · 冰的送到会化", "k": "gap"}
    if t == "mix":
        return {"t": "现做 · 冰的会化", "k": "gap"}
    return {"t": "现做", "k": "gap"}


def is_food(name):
    n = name or ""
    return not any(k in n for k in NOT_FOOD)


def is_side(name):
    n = name or ""
    return is_food(n) and any(k in n for k in SIDE_HINT) and "堡" not in n


def side_rank(name):
    """越小越优先：咸口小食 → 饮料 → 甜品。"""
    n = name or ""
    if any(k in n for k in SWEET_HINT):
        return 2
    if any(k in n for k in ("可乐", "雪碧", "红茶", "咖啡", "奶茶", "果汁", "饮")):
        return 1
    return 0


def people_in(name):
    if re.search(r"全家|欢聚|聚会|分享|桶", name):
        return 3
    m = re.search(r"([双两二三四五六七])人", name)
    if not m:
        return 2
    return {"双": 2, "两": 2, "二": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7}.get(m.group(1), 2)


def build_candidates(meals, people, order_type):
    pool = []
    for code, m in (meals or {}).items():
        nm = m.get("name") or ""
        pr = _num(m.get("currentPrice"))
        if pr <= 0 or not is_food(nm):
            continue
        pool.append({"code": code, "name": nm, "price": pr, "kind": classify(nm)})
    by = {}
    for it in pool:
        by.setdefault(it["kind"], []).append(it)
    for k in by:
        by[k].sort(key=lambda x: x["price"])

    out = []
    if by.get("qxp"):
        it = by["qxp"][0]
        out.append({"label": "%s ×%d" % (it["name"], people), "kind": "qxp", "note": "各点各的一份，最便宜的入口",
                    "items": [{"productCode": it["code"], "quantity": people}]})
    if by.get("combo"):
        it = by["combo"][0]
        out.append({"label": "%s ×%d" % (it["name"], people), "kind": "combo", "note": "堡 ＋ 小食 ＋ 饮料，一人一套",
                    "items": [{"productCode": it["code"], "quantity": people}]})
    if by.get("family"):
        pick, pn = None, 0
        for it in by["family"]:
            n = people_in(it["name"])
            if n >= people:
                pick, pn = it, n
                break
        if pick is None:
            pick = by["family"][0]
            pn = max(people_in(pick["name"]), 2)
        q = 1 if pn >= people else max(1, -(-people // pn))
        out.append({"label": pick["name"] + ("" if q == 1 else " ×%d" % q), "kind": "family",
                    "note": "一份够 %d 人，摆一桌分着吃" % pn,
                    "items": [{"productCode": pick["code"], "quantity": q}]})
    if by.get("burger"):
        b = by["burger"][0]
        sides = sorted([x for x in pool if is_side(x["name"])],
                       key=lambda x: (side_rank(x["name"]), x["price"]))
        items = [{"productCode": b["code"], "quantity": people}]
        label, note = "%s ×%d" % (b["name"], people), "只点堡，最省"
        if sides:
            s = sides[0]
            items.append({"productCode": s["code"], "quantity": people})
            label = "%s ＋ %s ×%d" % (b["name"], s["name"], people)
            note = "堡 ＋ 小食，各点各的"
        out.append({"label": label, "kind": "single", "note": note, "items": items})
    if len(by.get("combo") or []) > 1:
        it = by["combo"][1]
        out.append({"label": "%s ×%d" % (it["name"], people), "kind": "combo", "note": "换一种口味，贵一点",
                    "items": [{"productCode": it["code"], "quantity": people}]})
    return out[:5]


def _price_one(store_code, order_type, be_type, be_code, items, with_card=False):
    args = {"storeCode": store_code, "orderType": order_type, "beType": be_type, "items": items}
    if be_code:
        args["beCode"] = be_code
    if with_card:
        args["withOrder"] = CARD_WITH
    j = _call("calculate-price", **args)
    d = j.get("data") or {}
    return _num(d.get("price")) / 100.0, _num(d.get("originalPrice")) / 100.0, d


def price_plans(store_code, order_type, be_type, be_code, cands):
    def one(c):
        try:
            price, orig, _ = _price_one(store_code, order_type, be_type, be_code, c["items"])
            c["price"] = price
            c["original"] = orig
            c["ok"] = price > 0
        except Exception as e:
            c["ok"], c["price"] = False, 0.0
            c["err"] = str(e)[:90]
        return c

    with ThreadPoolExecutor(max_workers=6) as ex:
        return list(ex.map(one, cands))


def card_check(store_code, order_type, be_type, be_code, plans):
    """麦金卡到底值不值：必须两次试价取差，不能拿划线价当省额。"""
    if not plans:
        return None
    base = plans[0]
    try:
        with_card, _, _ = _price_one(store_code, order_type, be_type, be_code,
                                     base["items"], with_card=True)
    except Exception:
        return None
    net = round(base["price"] - with_card, 2)
    return {"no_card": base["price"], "with_card": with_card,
            "net": net, "fee": CARD_FEE, "worth": net > 0.5}


# ==========================================================================
# 交通
# ==========================================================================
def leg_options(o_loc, d_loc, city, cityd, people, want_bus=True):
    """一段路，四种走法。四个查询并发发出去，别一段一段等。"""
    out = {}
    jobs = {"walk": lambda: mcpc.route_walking(o_loc, d_loc),
            "bike": lambda: mcpc.route_bicycling(o_loc, d_loc),
            "taxi": lambda: mcpc.route_driving(o_loc, d_loc)}
    if want_bus:
        jobs["bus"] = lambda: mcpc.route_transit(o_loc, d_loc, city, cityd)

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {k: ex.submit(f) for k, f in jobs.items()}
        for k, fu in futs.items():
            try:
                out[k] = fu.result()
            except Exception:
                out[k] = None

    res = {}
    if out.get("walk"):
        res["walk"] = {"sec": out["walk"]["sec"], "m": out["walk"]["m"], "cost": 0.0}
    if out.get("bike"):
        res["bike"] = {"sec": out["bike"]["sec"], "m": out["bike"]["m"],
                       "cost": BIKE_PER_LEG * people}
    if out.get("taxi"):
        res["taxi"] = {"sec": out["taxi"]["sec"], "m": out["taxi"]["m"],
                       "cost": out["taxi"].get("taxi") or 0.0}
    if out.get("bus"):
        res["bus"] = {"sec": out["bus"]["sec"], "m": out["bus"]["m"],
                      "cost": out["bus"]["cost"] * people, "line": out["bus"].get("line") or ""}
    return res


def build_ways(legs, people, has_subway=False):
    """legs: [(label, opts)] —— 每种方式把所有段加起来（同一方式走完全程）。"""
    modes = [("步行", "walk", "免费"), ("骑行 / 共享电瓶车", "bike", "按 ¥1.5 / 人 / 段估"),
             ("公交", "bus", ""), ("打车", "taxi", "一车装得下就按一车算")]
    # 一句话里塞「骑行 / 共享电瓶车」太长，念不出来；清单里仍用全称
    short = {"walk": "步行", "bike": "骑行", "bus": "公交", "taxi": "打车"}
    rows = []

    for name, key, note in modes:
        secs, cost, ok, segs = [], 0.0, True, []
        for _label, opts in legs:
            o = opts.get(key)
            if not o:
                ok = False
                break
            secs.append(o["sec"])
            segs.append({"label": _label, "min": _min(o["sec"])})
            cost += o.get("cost") or 0.0
        if not ok or not secs:
            continue
        total = sum(secs)
        # 交通方式只摆事实：总时长 ＋ 全队花费。走不走、坐不坐，用户自己定。
        # 这里没有 30 分门槛 —— 曾经的「步行超 30 分标不推荐」已于 v0.9.4 废弃，
        # 理由：这是推荐交通方式，不是替用户否决选项。
        per_leg = " ＋ ".join("%d 分" % _min(s) for s in secs)
        rows.append({
            "name": name, "key": key, "short": short.get(key, name),
            "min": _min(total),
            "legs": segs,          # 分段耗时：「X 分钟就到」说的是到店那一段，不是全程
            "per_leg": per_leg if len(secs) > 1 else "",
            "cost": round(cost, 2),
            "cost_text": ("免费" if cost <= 0 else
                          ("%s / 车" % _money(cost) if key == "taxi" else
                           ("%d 人 约 %s" % (people, _money(cost))))),
            "note": note,
            "off": False,
            "warn": "",
            "tag": "",
        })

    # 谁快谁进推荐位 —— 不因门槛踢人，只按耗时比
    if rows:
        fastest = min(rows, key=lambda r: r["min"])
        fastest["best"] = True
        fastest["tag"] = "最快"
    for r in rows:
        r.setdefault("best", False)

    order = {"walk": 0, "bike": 1, "bus": 2, "taxi": 3}
    rows.sort(key=lambda r: order.get(r["key"], 9))
    return {"rows": rows, "best": next((r for r in rows if r.get("best")), None)}


# ==========================================================================
# 着重点：后台判断，只决定那一句话怎么写，不上界面
# ==========================================================================
def focus_of(req, best_per):
    taste = req.get("taste") or ""
    tired = req.get("tired") or ""
    budget = _num(req.get("budget"))
    hhmm = (req.get("when") or "")[-5:]

    neg_cold = re.search(r"(不|忌|别|不能|不想)[^/／]*(冰|冷|凉|雪)", taste) or \
               re.search(r"(冰|冷|凉)[^/／]*(不|别)", taste)
    want_hot = re.search(r"(热|暖|汤|粥|咖啡)", taste)

    if neg_cold or want_hot:
        return {"k": "temp", "want": "想喝口热的",
                "note": "把「不能吃冰」翻成「想喝口热的」，说的是他想要什么，不是他不要什么"}
    if re.search(r"辣|鸡翅|麦辣|巨无霸|板烧", taste):
        return {"k": "want", "want": "要的就是这口" + (re.search(r"辣|鸡翅|麦辣|巨无霸|板烧", taste).group(0))}
    if tired == "疲劳":
        return {"k": "tired", "want": "不想再动了"}
    if budget and best_per and best_per > budget:
        return {"k": "price", "want": "别超预算"}
    if hhmm and hhmm >= "21:00":
        return {"k": "time", "want": "想早点吃上"}
    return {"k": "tired" if tired == "疲劳" else "time", "want": "吃口热的就行"}


# ==========================================================================
# 主流程
# ==========================================================================
def compute(req):
    t0 = time.time()
    try:
        calls0 = mcpc.mcd().calls
    except Exception:
        calls0 = 0
    people = max(1, min(10, int(_num(req.get("people"), 1))))
    form = req.get("form") or "dinein"
    when = (req.get("when") or "").strip()

    out = {"ok": False, "panes": {}, "warnings": [], "echo": {
        "people": people, "form": form, "when": when,
        "from_addr": (req.get("from_addr") or "").strip(),
        "to_addr": (req.get("to_addr") or "").strip(),
        "budget": _num(req.get("budget")), "taste": req.get("taste") or "",
        "rel": (req.get("rel") or "").replace("free:", ""), "tired": req.get("tired") or "",
    }}

    # ---- 时间 ----
    # 散场时间在未来 → 按那一刻查（菜单、价格、券都支持 reservationDate）；
    # 已经过去 → 按现在算，并明说一句，别让用户以为算的是他填的时间。
    import datetime as _dt
    now_dt, _ = _server_time(when)
    plan_dt = _parse_dt(when) or now_dt
    if plan_dt > now_dt + _dt.timedelta(minutes=2):
        set_resv(plan_dt.strftime("%Y-%m-%d %H:%M"))
        eff_dt = plan_dt
        out["mode"] = "reserve"
    else:
        set_resv(None)
        eff_dt = now_dt
        out["mode"] = "now"
        if plan_dt < now_dt - _dt.timedelta(minutes=20):
            out["warnings"].append(
                "你填的散场时间（%s）已经过去了，下面按现在 %s 算。"
                % (plan_dt.strftime("%m-%d %H:%M"), now_dt.strftime("%H:%M")))

    out["now"] = {"date": now_dt.strftime("%Y-%m-%d"), "weekday": WEEK[now_dt.isoweekday()],
                  "hhmm": now_dt.strftime("%H:%M")}
    out["plan"] = {"date": eff_dt.strftime("%Y-%m-%d"), "weekday": WEEK[eff_dt.isoweekday()],
                   "hhmm": eff_dt.strftime("%H:%M"),
                   "label": "预约" if out["mode"] == "reserve" else "现在"}
    out["echo"]["weekday"] = _weekday_of(when) or WEEK[eff_dt.isoweekday()]

    # 偏好 / 忌口先解析出来，挂到 req 上给堂食、外卖两条分支共用
    req["_pref"] = parse_taste(req.get("taste"))
    out["echo"]["pref"] = req["_pref"]

    # ---- 地理编码（两个地址并发查）----
    from_addr = out["echo"]["from_addr"]
    to_addr = out["echo"]["to_addr"]
    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(mcpc.geocode, from_addr)
        fb = ex.submit(mcpc.geocode, to_addr) if to_addr else None
        A = fa.result()
        B = fb.result() if fb else None

    # 第二轮：落脚点只写了地名、没带城市，或者只查到「某某市」这一级时，
    # 借出发地的城市名再查一次。不修这一步，目的地会落在市中心，距离凭空几十公里。
    city_hint = (A or {}).get("city") or ""
    if to_addr and city_hint and mcpc.coarse(B):
        B2 = mcpc.geocode(to_addr, city=city_hint)
        if B2 and not mcpc.coarse(B2):
            B = B2

    if not A:
        out["warnings"].append("这个地址高德没查准，换个写法试试（带上城市名和门牌号最稳）。")
        out["took_ms"] = int((time.time() - t0) * 1000)
        return out
    if to_addr and not B:
        out["warnings"].append("落脚点地址没查到，堂食的回程和外卖的送达点算不准。")
    elif to_addr and mcpc.coarse(B):
        out["warnings"].append(
            "落脚点只查到「%s」这一级，不是一个具体地点 —— 所以距离和耗时都只能看个大概。"
            "补个门牌号或店名会准很多。" % (B.get("formatted") or to_addr))
    out["geo"] = {"from": A.get("formatted"), "to": (B or {}).get("formatted")}

    # ---- 门店 ----
    store = find_store(A, 1)
    if not store:
        out["warnings"].append("附近没找到此刻能到店点餐的麦当劳（可能都打烊了）。换个散场时间或走外卖试试。")
        out["took_ms"] = int((time.time() - t0) * 1000)
        return out
    out["store"] = store

    # 门口那几家如果都点不了（打烊 / 不支持到店 / 不在供餐时段），最近的一家可能在几公里外。
    # 这个必须说，否则「步行 40 分钟」看起来像系统算错了。
    try:
        km = store.get("km")
        if km is None and store.get("loc"):
            km = mcpc.haversine_m(A["loc"], store["loc"]) / 1000.0
        if km is not None and km > 2.5:
            out["warnings"].append(
                "跟前这几家这会儿点不了（打烊或不到店），最近能点的是「%s」，%.1f 公里外。"
                % (store["name"].replace("麦当劳", ""), km))
    except Exception:
        pass

    # ---- 两条腿并发算 ----
    city = re.sub(r"(市|地区|自治州|盟)$", "", A.get("city") or "")
    cityd = re.sub(r"(市|地区|自治州|盟)$", "", (B or A).get("city") or "") or city

    legs = []
    store_loc = store.get("loc")
    if not store_loc:
        out["warnings"].append("这家门店的坐标没定准（地址里只有楼层铺位），所以「去门店」这段路没算。")
    with ThreadPoolExecutor(max_workers=3) as ex:
        f1 = ex.submit(leg_options, A["loc"], store_loc, city, cityd, people) if store_loc else None
        f2 = ex.submit(leg_options, store_loc, B["loc"], city, cityd, people) if (store_loc and B) else None
        f3 = ex.submit(leg_options, A["loc"], B["loc"], city, cityd, people) if B else None
        l1 = f1.result() if f1 else None
        l2 = f2.result() if f2 else None
        l_ab = f3.result() if f3 else None
    # 步行不到 1.2 公里的段，公交没意义，去掉
    for opt in (l1, l2, l_ab):
        if opt and "bus" in opt and (opt.get("walk") or {}).get("m", 9999) < 1200:
            opt.pop("bus", None)
    if l1:
        legs.append(("去门店", l1))
    if l2:
        legs.append(("回落脚点", l2))
    ways = build_ways(legs, people) if legs else {"rows": [], "best": None}

    if form == "undecided":
        # 两条分支互不依赖，并发跑；价差等两套都出来再补
        with ThreadPoolExecutor(max_workers=2) as ex:
            fd = ex.submit(_dinein, req, people, store, ways, eff_dt, A, B, city, cityd)
            fe = ex.submit(_delivery, req, people, eff_dt, A, B, city, cityd,
                           store, l_ab, None)
            out["panes"]["dinein"] = fd.result()
            out["panes"]["delivery"] = fe.result()
        db = (out["panes"]["dinein"] or {}).get("best")
        dpane = out["panes"]["delivery"] or {}
        if db and dpane.get("ok"):
            dpane["close"] = _close_delivery(dpane["best"], people, req,
                                             dpane.get("ways"), db)
    else:
        if form == "dinein":
            out["panes"]["dinein"] = _dinein(req, people, store, ways, eff_dt, A, B, city, cityd)
        else:
            out["panes"]["delivery"] = _delivery(req, people, eff_dt, A, B, city, cityd,
                                                 store, l_ab, None)

    ok_any = any(p.get("ok") for p in out["panes"].values())
    out["ok"] = ok_any
    for p in out["panes"].values():
        for w in (p.get("warnings") or []):
            if w not in out["warnings"]:
                out["warnings"].append(w)
    out["took_ms"] = int((time.time() - t0) * 1000)
    out["calls"] = max(0, mcpc.mcd().calls - calls0)
    return out


def _server_time(when):
    try:
        j = mcpc.mcd_call("now-time-info")
        d = j.get("data") or j
        s = d.get("formatted") or d.get("datetime") or ""
        dt = _parse_dt(s) or _parse_dt(when)
        if dt:
            return dt, WEEK[dt.isoweekday()]
    except Exception:
        pass
    dt = _parse_dt(when)
    if dt:
        return dt, WEEK[dt.isoweekday()]
    import datetime as _d
    n = _d.datetime.now()
    return n, WEEK[n.isoweekday()]


def _parse_dt(s):
    import datetime as _d
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return _d.datetime.strptime((s or "").strip()[:26], fmt)
        except Exception:
            continue
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", s or "")
    if m:
        return _d.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                           int(m.group(4) or 0), int(m.group(5) or 0))
    return None


def _weekday_of(s):
    dt = _parse_dt(s)
    return WEEK[dt.isoweekday()] if dt else ""


# --------------------------------------------------------------------------
# 堂食
# --------------------------------------------------------------------------
def _dinein(req, people, store, ways, now_dt, A, B, city, cityd):
    pane = {"ok": False, "store": store}
    meals = store.get("meals")
    if not meals:
        try:
            j = _call("query-meals", storeCode=store["code"], orderType=1, beType=1)
            meals = (j.get("data") or {}).get("meals") or {}
        except Exception as e:
            pane["error"] = "菜单没拉到：%s" % str(e)[:80]
            return pane

    pref = req.get("_pref") or {}
    # 偏好方案（真含用户点名要的东西）排在候选最前，让试价把它算出来
    cands = pref_candidates(meals, people, pref) + build_candidates(meals, people, 1)
    plans = _dedup_plans([p for p in price_plans(store["code"], 1, 1, None, cands) if p.get("ok")])
    if not plans:
        pane["error"] = "这家店这会儿试不出价（可能已过营业时段）。"
        return pane

    # 有偏好的那套无条件保留（点名要的贵也得出），其余按价格，贵出三倍的不列
    pref_plans = _trim_pref(plans)
    floor = min(p["price"] for p in plans)
    other = sorted([p for p in plans if not p.get("pref") and p["price"] <= floor * 3],
                   key=lambda p: p["price"])
    plans = tag_plans(pref_plans + other, people, pref, meals)[:5]
    best = plans[0]

    # 偏好兑现情况：命中的已经在最前面；没命中的必须当场说，不能装作没看见
    if pref.get("want") or pref.get("avoid"):
        rep = pref_report(pref, plans)
        if rep.get("missing"):
            rep["later"] = pref_later(store["code"], 1, 1, None, pref, now_dt)
        pane["pref"] = rep
    pane["plans"] = plans
    pane["best"] = best
    pane["ok"] = True

    # 这个点门店在卖哪一档 —— 用门店自己的排班表校验，别推荐一个点不出来的时段
    known, slot = slot_ok(store.get("resv"), now_dt)
    pane["slot"] = slot
    if known and not slot:
        pane["warnings"] = ["这个点位不在门店的营业时段里（门店按预约时间给的排班）。"
                            "换个散场时间，或者走外卖那条。"]

    # 券
    pane["coupons"] = coupon_block(store["code"], 1, 1, None, now_dt)

    # 麦金卡
    pane["card"] = card_check(store["code"], 1, 1, None, plans)
    pane["card"] = _card_line(pane["card"], best)

    # 交通
    pane["ways"] = ways
    pane["ways"]["head"] = _ways_head(A, store, B)
    pane["ways"]["note"] = _ways_note(ways, people, B, store)

    # 一句话
    focus = focus_of(req, best["per"])
    fast = ways.get("best") or {}
    pane["focus"] = focus["k"]
    pane["verdict"] = _verdict_dinein(focus, fast, best, people, store, ways, B, pane.get("pref"))
    pane["close"] = _close_dinein(best, people, req, ways, B)
    pane["action"] = _action_dinein(best, store, ways)
    pane["rel"] = _rel_note(req, people, meals)
    return pane


def _item_name(meals, it):
    m = (meals or {}).get(it["productCode"]) or {}
    return (m.get("name") or it["productCode"]) + ("×%d" % it["quantity"] if it["quantity"] > 1 else "")


def _card_line(card, best):
    if not card:
        return None
    if card["worth"]:
        return {"text": "这单买卡省 <b class='yes'>%s</b>（卡费 %s / 15 天，含在价里）。"
                        % (_money(card["net"]), _money(card["fee"])), "worth": True}
    return {"text": "这单用不上——不买 <b>%s</b>，买了 <b class='no'>%s</b>，反而多花。"
                    % (_money(card["no_card"]), _money(card["with_card"])), "worth": False,
            "hint": "点餐页加购的是 15 天体验卡 ¥9.90；App 会员页是月卡 ¥19，别混"}


def _ways_head(A, store, B):
    if store.get("km") is not None:
        d1 = "%.1f km" % store["km"]
    elif store.get("walk_sec"):
        d1 = "约 %.1f km" % (store["walk_sec"] * 1.2 / 1000)
    else:
        d1 = "路况没定准"
    if B and store.get("loc"):
        d2 = "%.1f km" % (mcpc.haversine_m(store["loc"], B["loc"]) / 1000.0)
        return "当前地址 → 门店（%s）→ <b>落脚点</b>（%s）" % (d1, d2)
    if B:
        return "当前地址 → 门店（%s）→ <b>落脚点</b>" % d1
    return "当前地址 → 门店（%s）" % d1


def _ways_note(ways, people, B, store):
    bits = []
    if B:
        bits.append("<b>默认按吃完回你填的落脚点算</b>，回程这段的费用和时间都含在合计里")
    else:
        bits.append("没填落脚点，所以只算了去门店这一段")
    bus = next((r for r in ways["rows"] if r["key"] == "bus"), None)
    if not bus:
        bits.append("这一段地铁公交不划算，没列")
    bits.append("换地址会自动重算")
    return " · ".join(bits)


def _verdict_dinein(focus, fast, best, people, store, ways, B, pref_rep=None):
    mode = fast.get("short") or fast.get("name") or "过去"
    # 「X 分钟就到」说的是**到店**那一段。拿全程去门店＋回落脚点的时间说「就到」，
    # 门店明明 120 米也会被写成「步行 19 分钟就到」，用户到店一看就知道是假的。
    to_store = next((s["min"] for s in (fast.get("legs") or []) if s.get("label") == "去门店"), None)
    mins = to_store if to_store is not None else (fast.get("min") or 0)
    miss = (pref_rep or {}).get("missing") or []
    hitlist = (pref_rep or {}).get("hit") or []
    # 这一单是冰的还是热的，决定「现做」能不能兑现成「烫」。
    # 点麦旋风却说「到嘴还是烫的」，一句话就废了。
    tt = temp_tag((best.get("items_text") or "").split("、"))
    if miss:
        # 用户点名要的东西没有 —— 这句话必须先认这件事，不能绕过去谈别的优点
        later = (pref_rep or {}).get("later")
        lead = "这会儿没有<b class='em'>%s</b>%s。下面是没这口的兜底。" % (
            "、".join(miss), "，换个时段才有" if later else "")
        t1 = {"t": "你要的没有", "k": "gap"}
    elif hitlist:
        # 点名要的东西有，就先说这个。「不想吃冰的」是排除项，不该抢主位
        lead = "有<b class='em'>%s</b>，%s %d 分钟就到。" % ("、".join(hitlist), mode, mins)
        t1 = tt
    elif focus["k"] == "temp" and temp_of((best.get("items_text") or "").split("、")) == "hot":
        lead = "想喝口<b class='em'>热的</b>，%s %d 分钟就到。" % (mode, mins)
        t1 = {"t": "现做 · 到嘴还是烫的", "k": "hot"}
    elif focus["k"] == "want":
        # 走到这里说明「想要」既没命中、pref_rep 也没报缺 —— 不能说成「就是这口」
        lead = "%s %d 分钟就到，%d 人 %s。" % (mode, mins, people, _money(best["price"]))
        t1 = tt
    elif focus["k"] == "tired":
        lead = "不想再动，%s %d 分钟坐下吃。" % (mode, mins)
        t1 = {"t": "%d 分钟到店" % mins, "k": ""}
    elif focus["k"] == "price":
        lead = "%d 个人 %s 就能吃，%s %d 分钟。" % (people, _money(best["price"]), mode, mins)
        t1 = {"t": "人均 %s" % _money(best["per"]), "k": "hot"}
    else:
        lead = "%s %d 分钟就到，%d 人 %s。" % (mode, mins, people, _money(best["price"]))
        t1 = tt

    tags = [t1, {"t": "%d 人 · 人均 %s" % (people, _money(best["per"])), "k": ""}]
    if ways.get("best"):
        tags.append({"t": "交通 %s" % ways["best"]["cost_text"], "k": ""})
    return {"lead": lead, "lead_text": re.sub(r"<[^>]+>", "", lead),
            "price": best["price"], "per": best["per"], "tags": tags[:3]}


def _close_dinein(best, people, req, ways, B):
    budget = _num(req.get("budget"))
    detail = []
    if budget:
        gap = budget - best["per"]
        if gap >= 0:
            detail.append("比人均预算 %s 省 %s / 人" % (_money(budget), _money(gap)))
        else:
            detail.append("人均比预算 %s 超 %s" % (_money(budget), _money(-gap)))
    if ways.get("best"):
        detail.append("交通另计：%s" % ways["best"]["cost_text"])
    return {"main": "%s，<b>%s</b>，人均 %s。" % (best["label"], _money(best["price"]), _money(best["per"])),
            "detail": "　·　".join(detail)}


def _action_dinein(best, store, ways):
    steps = ["去：%s" % store["name"]]
    steps.append(best["label"])
    if ways.get("best"):
        steps.append("%s %d 分钟（%s）" % (ways["best"]["name"], ways["best"]["min"],
                                        ways["best"]["cost_text"]))
    steps.append("合计 %s" % _money(best["price"]))
    return {"title": "到店点单，照着说", "steps": steps, "foot": "到店说，或截屏给同伴"}


def _rel_note(req, people, meals):
    """关系三红线：不推测、不性别化、不做人群画像。1 人整块不出现。"""
    rel = (req.get("rel") or "").replace("free:", "").strip()
    if people < 2 or not rel:
        return None
    half = []
    for code, m in (meals or {}).items():
        nm = m.get("name") or ""
        if "第二份半价" in (m.get("discountType") or "") or nm in ("经典麦旋风", "新地", "圆筒冰淇淋"):
            half.append(nm)
    if half:
        return "甜品有<b>第二份半价</b>——「%s」这类，两个人点两份只花一份半的钱。" % half[0]
    return None


# --------------------------------------------------------------------------
# 外卖
# --------------------------------------------------------------------------
def _delivery(req, people, now_dt, A, B, city, cityd, store_dinein, l_ab, dinein_best=None):
    pane = {"ok": False}
    to_addr = (req.get("to_addr") or "").strip()
    try:
        ads = (mcpc.mcd_call("delivery-query-addresses").get("data") or {}).get("addresses") or []
    except Exception as e:
        pane["error"] = "地址读不到：%s" % str(e)[:80]
        return pane

    hit = match_address(ads, to_addr)
    if not hit:
        pane["error"] = ("外送只认麦当劳 App 里已经存过的地址（本工具只读，不代建）。"
                         "你填的「%s」没在列表里，先去 App 加一个再回来。" % (to_addr or "落脚点"))
        pane["addresses"] = [a.get("fullAddress") for a in ads]
        return pane

    try:
        sj = mcpc.mcd_call("delivery-query-stores", beType=2, addressId=hit["addressId"])
        stores = sj.get("data") or []
    except Exception as e:
        pane["error"] = "这家地址附近的外送门店没查到：%s" % str(e)[:80]
        return pane
    stores = [s for s in stores if s.get("businessStatus")] or stores
    if not stores:
        pane["error"] = "这个送达点附近没有在营业的外送门店。"
        return pane
    st = stores[0]
    sc, bc = st.get("storeCode"), st.get("beCode")

    try:
        j = _call("query-meals", storeCode=sc, orderType=2, beType=2, beCode=bc)
        meals = (j.get("data") or {}).get("meals") or {}
    except Exception as e:
        pane["error"] = "外送菜单没拉到：%s" % str(e)[:80]
        return pane

    pref = req.get("_pref") or {}
    cands = pref_candidates(meals, people, pref) + build_candidates(meals, people, 2)
    plans = _dedup_plans([p for p in price_plans(sc, 2, 2, bc, cands) if p.get("ok")])
    if not plans:
        pane["error"] = "这家店这会儿试不出外送价。"
        return pane
    pref_plans = _trim_pref(plans)
    floor = min(p["price"] for p in plans)
    other = sorted([p for p in plans if not p.get("pref") and p["price"] <= floor * 3],
                   key=lambda p: p["price"])
    plans = tag_plans(pref_plans + other, people, pref, meals, "delivery")[:5]
    best = plans[0]

    if pref.get("want") or pref.get("avoid"):
        rep = pref_report(pref, plans)
        if rep.get("missing"):
            rep["later"] = pref_later(sc, 2, 2, bc, pref, now_dt)
        pane["pref"] = rep

    hit_full = (hit.get("fullAddress") or "").strip()
    dstore = {"code": sc, "beCode": bc, "name": st.get("storeName"),
              "address": "送达：" + hit_full,
              "businessStartTime": st.get("businessStartTime"), "businessEndTime": st.get("businessEndTime")}

    pane.update({"ok": True, "store": dstore, "plans": plans, "best": best,
                 "coupons": coupon_block(sc, 2, 2, bc, now_dt)})
    pane["card"] = _card_line(card_check(sc, 2, 2, bc, plans), best)

    # 回程：散场点 → 落脚点。点外卖时你真正要走的就是这一段
    ways = build_ways([("回落脚点", l_ab)], people) if l_ab else {"rows": [], "best": None}
    pane["ways"] = ways
    if ways.get("rows") and B:
        ways["head"] = "当前地址 → <b>落脚点</b>（%.1f km）" % (
            mcpc.haversine_m(A["loc"], B["loc"]) / 1000.0)
        ways["note"] = "外送不用你跑这一段 —— 这里算的是你自己过去要花的时间和钱，方便跟堂食比"

    # 送达信息：MCP 不返回 ETA，别编
    pane["delivery"] = {
        "to": _short_addr(hit_full, 14),
        "store": st.get("storeName"),
        "hours": "%s-%s" % (st.get("businessStartTime") or "?", st.get("businessEndTime") or "?"),
        "note": "这单由「%s」外送 · 营业 %s-%s · 送达时刻以下单页为准（MCP 不返回 ETA）" % (
            st.get("storeName") or "该店", st.get("businessStartTime") or "?",
            st.get("businessEndTime") or "?"),
    }

    focus = focus_of(req, best["per"])
    pane["focus"] = focus["k"]
    pane["verdict"] = _verdict_delivery(focus, best, people, pane["delivery"], store_dinein, pane.get("pref"))
    pane["close"] = _close_delivery(best, people, req, ways, dinein_best)
    # 行动卡是给同伴看的订单确认书：只留「点什么 / 送哪 / 多少钱」，不写解释
    pane["action"] = {"title": "外卖下单，照着说",
                      "steps": [best["label"],
                                "送到 %s" % _short_addr(hit_full, 40),
                                "合计 %s" % _money(best["price"])],
                      "foot": "送达时刻以下单页为准"}
    pane["rel"] = _rel_note(req, people, meals)
    return pane


def _verdict_delivery(focus, best, people, delivery, store_dinein, pref_rep=None):
    mins = delivery.get("ride_min")
    miss = (pref_rep or {}).get("missing") or []
    hitlist = (pref_rep or {}).get("hit") or []
    if miss:
        later = (pref_rep or {}).get("later")
        lead = "这会儿没有<b class='em'>%s</b>%s。下面是没这口的兜底。" % (
            "、".join(miss), "，换个时段才有" if later else "")
        tags = [{"t": "你要的没有", "k": "gap"}]
    elif hitlist:
        lead = "有<b class='em'>%s</b>，送到%s。" % (
            "、".join(hitlist), _poi_of(delivery.get("to") or "落脚点"))
        tags = [temp_tag((best.get("items_text") or "").split("、"), "delivery")]
    elif focus["k"] == "temp":
        lead = "点了<b class='em'>热的</b>，送到只剩温的。"
        tags = [{"t": "温度打问号", "k": "gap"}]
    elif focus["k"] == "tired":
        lead = "一步不用走，送到%s。" % _poi_of(delivery.get("to") or "落脚点")
        tags = [{"t": "不用出门", "k": ""}]
    elif focus["k"] == "price":
        lead = "%d 个人 %s，送到门口。" % (people, _money(best["price"]))
        tags = [{"t": "人均 %s" % _money(best["per"]), "k": "gap"}]
    else:
        lead = "送到%s，%d 人 %s。" % (_poi_of(delivery.get("to") or "落脚点"), people, _money(best["price"]))
        tags = [{"t": "等送到", "k": ""}]
    tags.append({"t": "%d 人 · 人均 %s" % (people, _money(best["per"])), "k": ""})
    if delivery.get("km"):
        tags.append({"t": "门店离这 %s km" % delivery["km"], "k": ""})
    return {"lead": lead, "lead_text": re.sub(r"<[^>]+>", "", lead),
            "price": best["price"], "per": best["per"], "tags": tags[:3]}


def _close_delivery(best, people, req, ways, dinein_best):
    detail = ["价格已含运费和打包费（外送按件收，封顶 ¥3）"]
    # 外卖最常被问的一句就是「人均 X 到底够不够」—— 不能只在堂食那边算预算，
    # 外卖这条路上预算没影，等于把用户填的 30 块钱当没看见。
    budget = _num(req.get("budget"))
    if budget:
        gap = budget - best["per"]
        if gap >= 0:
            detail.insert(0, "比人均预算 %s 省 %s / 人" % (_money(budget), _money(gap)))
        else:
            detail.insert(0, "人均比预算 %s 超 %s" % (_money(budget), _money(-gap)))
    if dinein_best:
        detail.insert(0, "比堂食贵 %s" % _money(best["price"] - dinein_best["price"]))
    return {"main": "%s，<b>%s</b>，人均 %s。" % (best["label"], _money(best["price"]), _money(best["per"])),
            "detail": "　·　".join(detail)}
