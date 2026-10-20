#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcpc.py —— 麦当劳 MCP + 高德 的最小客户端（纯标准库，不装依赖）

为什么要自己写：
  MCP 是 JSON-RPC 2.0 over HTTP，响应是 SSE 帧（`data: {...}`），
  Python 标准库就能说，不需要装 mcp-sdk / requests。

凭证从哪来：
  麦当劳 token 只从 ~/.workbuddy/mcp.json 读，不落盘、不进日志、不进返回值。
  高德 key 优先环境变量 AMAP_KEY，其次 ~/.zcode/amap.key，最后从 mcp.json 的 url 里抠。
"""

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from math import radians, sin, cos, asin, sqrt


# --------------------------------------------------------------------------
# 凭证
# --------------------------------------------------------------------------
def _load_json(path):
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _mcd_conf():
    cfg = _load_json("~/.workbuddy/mcp.json").get("mcpServers", {}).get("mcd-mcp") or {}
    url = os.environ.get("MCD_URL") or cfg.get("url") or "https://mcp.mcd.cn"
    auth = os.environ.get("MCD_AUTH") or (cfg.get("headers") or {}).get("Authorization") or ""
    return url, auth


def amap_key():
    k = (os.environ.get("AMAP_KEY") or "").strip()
    if k:
        return k
    p = os.path.expanduser("~/.zcode/amap.key")
    if os.path.exists(p):
        k = open(p, "r", encoding="utf-8").read().strip()
        if k:
            return k
    for name, cfg in (_load_json("~/.workbuddy/mcp.json").get("mcpServers") or {}).items():
        if "amap" in name:
            m = re.search(r"key=([0-9a-fA-F]{16,})", cfg.get("url") or "")
            if m:
                return m.group(1)
    return ""


# --------------------------------------------------------------------------
# MCP 客户端
# --------------------------------------------------------------------------
class McpError(Exception):
    pass


class Mcp(object):
    """一个极简 MCP Streamable-HTTP 客户端。一次 initialize，后续复用 session。"""

    def __init__(self, url, auth=None, label="mcp"):
        self.url = url
        self.auth = auth
        self.label = label
        self._sid = None
        self._seq = 0
        self._lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._ready = False
        self.calls = 0

    # -- 内部 ---------------------------------------------------------------
    def _post(self, payload):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.auth:
            headers["Authorization"] = self.auth
        if self._sid:
            headers["Mcp-Session-Id"] = self._sid
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=50) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._sid = sid
            return resp.read().decode("utf-8", "replace")

    @staticmethod
    def _frames(raw):
        """SSE 可能一帧一条、也可能多条；统一拆成 dict 列表。"""
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                out.append(json.loads(body))
            except Exception:
                pass
        return out

    def rpc(self, method, params, timeout_note=None):
        with self._lock:
            self._seq += 1
            rid = self._seq
        raw = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        frames = self._frames(raw)
        for f in frames:
            if f.get("id") == rid:
                return f
        if frames:
            return frames[-1]
        try:
            return json.loads(raw)
        except Exception:
            raise McpError("%s: 响应无法解析" % self.label)

    def initialize(self):
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            self.rpc("initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "AfterM", "version": "1.0"},
            })
            self._ready = True

    # -- 对外 ---------------------------------------------------------------
    def tools(self):
        self.initialize()
        d = self.rpc("tools/list", {})
        return (d.get("result") or {}).get("tools") or []

    def call_text(self, name, args=None):
        """调工具，返回 content[0].text 原文（麦当劳的券类工具返回的是 markdown）。"""
        self.initialize()
        self.calls += 1
        d = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        if "error" in d and "result" not in d:
            err = d["error"] or {}
            raise McpError("%s/%s 失败：%s" % (self.label, name, err.get("message") or err))
        res = d.get("result") or {}
        content = res.get("content") or []
        if not content:
            return ""
        return content[0].get("text") or ""

    def call_json(self, name, args=None, default=None):
        """调工具，把文本里的第一个合法 JSON 对象扫出来。

        麦当劳的响应前半段是字段说明文字，后面才是 JSON；没有稳定分隔符，
        所以从第一个 `{` 开始 raw_decode 逐个试。
        """
        text = self.call_text(name, args)
        obj = scan_json(text)
        if obj is None:
            raise McpError("%s/%s 没返回 JSON：%s" % (self.label, name, text[:120]))
        return obj


def scan_json(text):
    """从文本里扫出第一个「像业务响应」的 JSON 对象。"""
    if not text:
        return None
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except Exception:
            continue
        if isinstance(obj, dict) and ("success" in obj or "data" in obj or "code" in obj):
            return obj
    return None


# --------------------------------------------------------------------------
# 麦当劳
# --------------------------------------------------------------------------
_MCD = None


def mcd():
    global _MCD
    if _MCD is None:
        url, auth = _mcd_conf()
        _MCD = Mcp(url, auth, label="mcd")
    return _MCD


def ok(resp):
    """麦当劳统一信封：success / code 200。"""
    if not isinstance(resp, dict):
        return False
    if "success" in resp:
        return bool(resp.get("success"))
    return str(resp.get("code")) == "200"


def mcd_call(name, **args):
    """只要 JSON：失败抛异常，成功返回 data（或整个信封）。"""
    resp = mcd().call_json(name, args)
    if not ok(resp):
        raise McpError("%s 失败：%s" % (name, resp.get("message") or resp.get("code")))
    return resp


def mcd_text(name, **args):
    return mcd().call_text(name, args)


def mcd_try(name, **args):
    """不要异常：失败返回 {'_error': 文案}。用于可有可无的补充信息。"""
    try:
        return mcd_call(name, **args)
    except Exception as e:
        return {"_error": str(e)}


# --------------------------------------------------------------------------
# 高德（走 REST，不走 MCP —— MCP 不暴露 extensions，拿不到 taxi_cost）
# --------------------------------------------------------------------------
def amap(path, **params):
    key = amap_key()
    if not key:
        return {"_error": "没有高德 key"}
    params["key"] = key
    url = "https://restapi.amap.com" + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"_error": "高德请求失败：%s" % e}
    # v3 用 status=="1"；v4 用 errcode==0 且不带 status —— 两种都要认
    if "status" in j:
        if str(j.get("status")) != "1":
            return {"_error": j.get("info") or "高德返回异常", "_raw": j}
    elif str(j.get("errcode", 0)) != "0":
        return {"_error": j.get("errmsg") or "高德返回异常", "_raw": j}
    return j


# 高德 geocode 的 level：越靠前越粗。粗到这个程度就等于「只知道在哪个城」，
# 拿它的坐标当目的地会算出一条几十公里的假路。
# 「公交地铁站点 / 停车场」这类也算不可用 —— 它是具体地点，但不是「要去的地方」：
# 实测「泰州万达广场」会被解析成兴化市的「万达广场(公交站)」，跨了一个县级市。
_COARSE_LEVELS = {"国家", "省", "市", "区县", "开发区", "乡镇", "村庄", "未知", "",
                  "公交地铁站点", "公交站台", "地铁站", "停车场", "道路交叉路口"}

# POI 名字里带这些的，位置经常是「门口的公交站 / 停车场」，不是那个地方本身
_POI_NOISE = ("公交站", "停车场", "地铁站", "站台", "充电站", "公共厕所", "售票处",
              "出入口", "地铁站出口", "停车场入口", "公交车站")


def _kw_clean(addr, city=""):
    """去掉省市前缀，只留地名本体。「武汉市中南财经政法大学」→「中南财经政法大学」。

    POI 搜索用整串查命中率很低，去掉前缀反而稳。
    """
    s = (addr or "").strip()
    s = re.sub(r"^[\u4e00-\u9fa5]{2,6}(?:省|自治区|特别行政区)", "", s)
    if city:
        c = re.sub(r"(市|地区|自治州|盟)$", "", city)
        if c:
            s = re.sub(r"^" + re.escape(c) + r"(?:市|地区|自治州|盟)?", "", s)
    return s.strip()


def poi_search(addr, city=""):
    """POI 搜索：专治「大学 / 商场 / 小区 / 医院」这类名字。

    geocode/geo 对这类串经常只返回到「市」一级（返回的坐标是市中心），
    place/text 才认。但它也不能拿来就用，两个坑都会算错几十公里：
      ① 名字像但不是那个地方 —— 「万达广场」会匹配到「万达广场(公交站)」；
      ② 同城不同县 —— 泰州下辖兴化市，兴化的万达会顶掉海陵的万达。
    所以按「同城优先 + 名字包含」打分，分数不够就不采纳，宁可退回粗结果。
    """
    kw = _kw_clean(addr, city)
    if len(kw) < 3:
        return None
    params = {"keywords": kw, "offset": "10", "page": "1", "extensions": "base"}
    c = re.sub(r"(市|地区|自治州|盟)$", "", city or "")
    if c:
        params["city"] = c
        params["citylimit"] = "true"
    j = amap("/v3/place/text", **params)

    best, best_score = None, 0.0
    for p in (j.get("pois") or []):
        loc = p.get("location") or ""
        if not loc or loc.startswith("0"):
            continue
        try:
            lng, lat = [float(x) for x in loc.split(",")]
        except Exception:
            continue
        nm = (p.get("name") or "").strip()
        if any(b in nm for b in _POI_NOISE):
            continue
        cityname = p.get("cityname") or ""
        if isinstance(cityname, list):
            cityname = cityname[0] if cityname else ""
        pcity = re.sub(r"(市|地区|自治州|盟)$", "", cityname or "")
        adname = p.get("adname") or ""
        if isinstance(adname, list):
            adname = adname[0] if adname else ""

        score = 0.0
        if c and pcity == c:            # 同城：这是硬条件，不同城基本就是找错了
            score += 3.0
        if kw and kw in nm:             # 名字对得上
            score += 2.0
        elif kw and nm and nm in kw:
            score += 1.5
        if score > best_score:
            best_score = score
            best = {
                "loc": loc, "lng": lng, "lat": lat,
                "city": cityname or city, "district": adname,
                "adcode": p.get("adcode") or "",
                "formatted": ((p.get("address") or "") + nm) or addr,
                "level": "兴趣点",
            }
    # 给了城市就要求至少同城；没给城市也至少要求名字对得上
    if best and best_score >= (3.0 if c else 1.5):
        return best
    return None


def geocode(addr, city=""):
    """地址 → {loc,lng,lat,city,district,adcode,formatted,level}

    关键：**必须校验解析精度**。高德对「某某大学」这类串会返回「湖北省武汉市」
    这种市级结果，坐标是市中心 —— 不拦住的话，目的地会凭空跑到几十公里外。
    """
    if not addr:
        return None
    j = amap("/v3/geocode/geo", address=addr)
    g = None
    for item in (j.get("geocodes") or []):
        loc = item.get("location") or ""
        if not loc or loc.startswith("0"):
            continue
        try:
            lng, lat = [float(x) for x in loc.split(",")]
        except Exception:
            continue
        cityname = item.get("city") or item.get("province") or ""
        if isinstance(cityname, list):
            cityname = cityname[0] if cityname else ""
        g = {
            "loc": loc, "lng": lng, "lat": lat,
            "city": cityname, "district": item.get("district") or "",
            "adcode": item.get("adcode") or "",
            "formatted": item.get("formatted_address") or addr,
            "level": item.get("level") or "",
        }
        break

    # 解析够细（门牌号 / 兴趣点 / 道路）就直接用
    if g and g["level"] not in _COARSE_LEVELS:
        return g

    # 太粗或没查到 → 换 POI 搜索再试一次
    hint = city or (g or {}).get("city", "")
    p = poi_search(addr, hint)
    return p or g


def coarse(g):
    """这个解析结果是不是「只知道在哪个城」，拿来当目的地会算错距离。"""
    return (not g) or ((g.get("level") or "") in _COARSE_LEVELS)


def around(loc, keywords="麦当劳", radius=3000):
    j = amap("/v3/place/around", keywords=keywords, location=loc,
             radius=str(radius), offset="25", page="1", extensions="base")
    return j.get("pois") or []


def _first_path(j, key="route"):
    route = j.get(key) or {}
    paths = route.get("paths") or []
    return route, (paths[0] if paths else None)


def route_walking(o, d):
    route, p = _first_path(amap("/v3/direction/walking", origin=o, destination=d))
    if not p:
        return None
    return {"sec": int(p.get("duration") or 0), "m": int(p.get("distance") or 0), "cost": 0}


def route_bicycling(o, d):
    j = amap("/v4/direction/bicycling", origin=o, destination=d)
    data = j.get("data") or {}
    paths = data.get("paths") or []
    if not paths:
        return None
    p = paths[0]
    return {"sec": int(float(p.get("duration") or 0)), "m": int(float(p.get("distance") or 0)), "cost": 0}


def route_driving(o, d):
    route, p = _first_path(amap("/v3/direction/driving", origin=o, destination=d, extensions="all"))
    if not p:
        return None
    taxi = route.get("taxi_cost")
    try:
        taxi = float(taxi)
    except Exception:
        taxi = None
    return {"sec": int(p.get("duration") or 0), "m": int(p.get("distance") or 0), "taxi": taxi}


def route_transit(o, d, city, cityd):
    """公交/地铁：票价取 transits[0].cost，耗时取 duration，末班车看 missed。"""
    j = amap("/v3/direction/transit/integrated", origin=o, destination=d,
             city=city, cityd=cityd, extensions="all")
    route = j.get("route") or {}
    transits = route.get("transits") or []
    if not transits:
        return None
    p = transits[0]
    cost = p.get("cost")
    if isinstance(cost, list):
        cost = cost[0] if cost else None
    try:
        cost = float(cost)
    except Exception:
        cost = 0.0
    return {"sec": int(float(p.get("duration") or 0)), "m": int(float(p.get("distance") or 0)),
            "cost": cost, "line": _transit_line(p),
            "missed": str(p.get("missed")) == "1"}


def _transit_line(p):
    segs = p.get("segments") or []
    names = []
    for s in segs:
        b = s.get("bus") or {}
        bl = b.get("buslines") or []
        if bl:
            names.append(bl[0].get("name") or "")
        rail = s.get("railway") or {}
        if rail.get("name"):
            names.append(rail["name"])
    return " / ".join([n for n in names if n][:2])


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def haversine_m(loc_a, loc_b):
    try:
        lng1, lat1 = [float(x) for x in loc_a.split(",")]
        lng2, lat2 = [float(x) for x in loc_b.split(",")]
    except Exception:
        return 9e9
    r = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * r * asin(sqrt(a))
