#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QMS 纯 HTTP 探针 —— 不开浏览器，直接复刻 Check Availability 的 ASP.NET AJAX 回发。

只做只读查询：唯一会按的按钮是 btnCheckAvailablity，唯一会触发的回发目标是出口国下拉。
绝不提交 / 付款 / 改期 / 取消。

用法：
  python qms_probe.py --check                 # 只验登录态 + 配置，不查任何日期（1~2 个请求）
  python qms_probe.py --countries             # 列出出口国下拉的全部选项（填 config.txt 用）
  python qms_probe.py --probe                 # 跑通性验证：选国家 + 查两个日期
  python qms_probe.py --query 15/01/2027 --room cattery_ac   # 查单个入境日
  python qms_probe.py --keepalive 10          # 每 10 分钟摸一次，实测会话寿命
"""
import argparse
import copy
import datetime as dt
import html as htmllib
import os
import random
import re
import sys
import time
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

BASE = "https://qms.avs.gov.sg"
PAGE = "/Pages/Reservation/QuarantineReservation.aspx"
URL = BASE + PAGE
SEC = "ctl00$MainContent$secReservationInitialSection$ctl00$"
PANEL = "ctl00$MainContent$updatePanel"
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "_cookie.txt")
CONFIG_FILE = os.path.join(HERE, "config.txt")
LOG_FILE = os.path.join(HERE, "_probe.log")

# 房型 -> (宠物数量字段, 房间数量字段)
ROOMS = {
    "cattery_fan": ("txtNoOfCats", "txtNoOfFanCaterries"),
    "cattery_ac": ("txtNoOfCats", "txtNoOfACCaterries"),
    "kennel_fan": ("txtNoOfDogs", "txtNoOfFanKennels"),
    "kennel_ac": ("txtNoOfDogs", "txtNoOfACKennels"),
}
ALL_COUNT_FIELDS = ["txtNoOfCats", "txtNoOfDogs",
                    "txtNoOfFanCaterries", "txtNoOfACCaterries",
                    "txtNoOfFanKennels", "txtNoOfACKennels"]

DEFAULTS = {
    "country": "China",      # 下拉里的显示名（前缀匹配，不分大小写）或代码，如 45,7
    "cats": "1",
    "dogs": "0",
    "cattery_rooms": "1",
    "kennel_rooms": "1",
    "room_types": "fan,ac",  # 风扇 / 空调，可只填一个
    "months_ahead": "8",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


class SessionDead(RuntimeError):
    pass


class TransientError(RuntimeError):
    """服务端 5xx / 网络抖动，重试耗尽后抛出。不是登录态问题，别打开 cookie 教程。"""


def log(msg):
    line = "%s  %s" % (dt.datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class ConfigError(RuntimeError):
    pass


def load_config(path=CONFIG_FILE):
    """config.txt：每行 key = value，# 后面是注释。缺的键用 DEFAULTS。"""
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        for n, line in enumerate(open(path, encoding="utf-8-sig"), 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" not in line:
                raise ConfigError("config.txt 第 %d 行看不懂：%r（应写成 key = value）" % (n, line))
            k, v = (x.strip() for x in line.split("=", 1))
            if k not in DEFAULTS:
                raise ConfigError("config.txt 第 %d 行：没有 %r 这个设置项，可选的有 %s"
                                  % (n, k, "、".join(DEFAULTS)))
            cfg[k] = v
    try:
        for k in ("cats", "dogs", "cattery_rooms", "kennel_rooms", "months_ahead"):
            cfg[k] = int(cfg[k])
    except ValueError:
        raise ConfigError("config.txt 里 %s 必须是整数，现在是 %r" % (k, cfg[k]))
    types = [t.strip().lower() for t in cfg["room_types"].split(",") if t.strip()]
    bad = [t for t in types if t not in ("fan", "ac")]
    if bad or not types:
        raise ConfigError("room_types 只能填 fan、ac 或 fan,ac，现在是 %r" % cfg["room_types"])
    rooms = []
    if cfg["cats"] > 0:
        rooms += ["cattery_" + t for t in types]
    if cfg["dogs"] > 0:
        rooms += ["kennel_" + t for t in types]
    if not rooms:
        raise ConfigError("config.txt 里 cats 和 dogs 都是 0，没东西可查")
    cfg["rooms"] = rooms
    return cfg


def counts_for(cfg, room):
    """某个房型查询时填进表单的 (宠物数, 房间数)。"""
    if room.startswith("cattery"):
        return cfg["cats"], cfg["cattery_rooms"]
    return cfg["dogs"], cfg["kennel_rooms"]


def load_cookies(path=COOKIE_FILE):
    """支持每行 name=value、name<TAB>value，或一整行 'a=1; b=2'。"""
    if not os.path.exists(path):
        raise SessionDead("找不到 _cookie.txt —— 第一次用要先按教程登录、把 cookie 粘进去")
    raw = open(path, encoding="utf-8-sig", errors="replace").read()
    jar = {}
    parts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts.extend(p for p in line.split("; ") if p) if "; " in line else parts.append(line)
    for p in parts:
        if "\t" in p and "=" not in p.split("\t")[0]:
            k, v = p.split("\t", 1)
        elif "=" in p:
            k, v = p.split("=", 1)
        else:
            continue
        jar[k.strip()] = v.strip()
    sid = jar.get("ASP.NET_SessionId", "")
    if not sid or not re.fullmatch(r"[A-Za-z0-9]{16,}", sid):
        raise SessionDead("_cookie.txt 里没有有效的 ASP.NET_SessionId（还是示例占位符，或者复制漏了）")
    return jar


class Qms:
    def __init__(self, jitter=(3.0, 7.0), cfg=None):
        self.cfg = cfg or load_config()
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA,
                               "Accept-Language": "en-US,en;q=0.9"})
        for k, v in load_cookies().items():
            self.s.cookies.set(k, v, domain="qms.avs.gov.sg")
        self.fields = {}
        self.clean = None          # 选完出口国的「干净态」快照
        self.countries = []        # [(代码, 显示名)]，bootstrap 时从下拉里读
        self.country = None        # 选中的 (代码, 显示名)
        self.jitter = jitter
        self.req_count = 0

    # ---------- 底层 ----------
    def _sleep(self):
        if self.req_count:
            time.sleep(random.uniform(*self.jitter))
        self.req_count += 1

    @staticmethod
    def _harvest(soup, into):
        """把一段 HTML 里的表单控件值收进 into（按钮/文件框不收）。"""
        for el in soup.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name:
                continue
            t = (el.get("type") or "").lower()
            if t in ("submit", "image", "button", "file", "reset"):
                continue
            if el.name == "input":
                if t in ("checkbox", "radio"):
                    if el.has_attr("checked"):
                        into[name] = el.get("value", "on")
                else:
                    into[name] = el.get("value", "")
            elif el.name == "select":
                opt = el.find("option", selected=True) or el.find("option")
                into[name] = opt.get("value", "") if opt else ""
            else:
                into[name] = el.text or ""

    def _send(self, method, **kw):
        """发请求；网关 5xx / 超时 / 断连按 30s、60s、120s 退避重试，4xx 不重试。

        2026-10-09 实测：扫到第 4 个房型时 AVS 网关回了一次 502，旧代码直接崩，
        前 3 个房型的结果在内存里全丢。重试是安全的：失败时 self.fields 没被改动，
        check() 每次又从干净态快照重建，同一个请求重发等价于没发过。
        """
        waits = (30, 60, 120)
        for k in range(len(waits) + 1):
            self._sleep()
            try:
                r = self.s.request(method, URL, **kw)
                if r.status_code < 500:
                    return r
                err = "HTTP %d" % r.status_code
            except (requests.ConnectionError, requests.Timeout) as e:
                err = type(e).__name__
            if k == len(waits):
                raise TransientError("%s，重试 %d 次仍失败" % (err, len(waits)))
            log("   ⚠ %s，%ds 后重试（第 %d/%d 次）" % (err, waits[k], k + 1, len(waits)))
            time.sleep(waits[k])

    def bootstrap(self):
        r = self._send("GET", allow_redirects=False, timeout=45)
        if r.status_code in (301, 302) or "Login.aspx" in r.text[:2000]:
            raise SessionDead("GET 预约页被重定向到 %s" % r.headers.get("Location"))
        soup = BeautifulSoup(r.text, "html.parser")
        sel = soup.find(attrs={"name": SEC + "ddlCountryOfExport"})
        if not sel:
            raise SessionDead("页面上没有出口国下拉，可能不是预约页或已掉登录")
        self.countries = [(o.get("value", ""), o.get_text(strip=True))
                          for o in sel.find_all("option") if o.get("value", "0") != "0"]
        self.fields = {}
        self._harvest(soup, self.fields)
        log("bootstrap OK：%d 个表单字段，__VIEWSTATE %d 字节"
            % (len(self.fields), len(self.fields.get("__VIEWSTATE", ""))))
        return self

    def _post(self, target, overrides, button=None):
        if button and button != SEC + "btnCheckAvailablity":
            raise RuntimeError("只允许按 Check Availability，拒绝：%s" % button)
        if target and target != SEC + "ddlCountryOfExport":
            raise RuntimeError("只允许出口国回发，拒绝：%s" % target)
        body = dict(self.fields)
        body.update({
            "ctl00$smMaster": PANEL + "|" + (button or target),
            "__EVENTTARGET": target or "",
            "__EVENTARGUMENT": "",
            "__LASTFOCUS": "",
            "__ASYNCPOST": "true",
        })
        body.update(overrides)
        if button:
            body[button] = "Check Availability"
        r = self._send("POST", data=body, timeout=90, headers={
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE,
            "Referer": URL,
            "Cache-Control": "no-cache",
        })
        r.raise_for_status()
        return self._apply_delta(r.text)

    def _apply_delta(self, text):
        """解析 ASP.NET AJAX 增量响应；回填新的 __VIEWSTATE/__EVENTVALIDATION。"""
        segs, i = [], 0
        while i < len(text):
            j = text.find("|", i)
            if j < 0:
                break
            try:
                ln = int(text[i:j])
            except ValueError:
                break
            k = text.find("|", j + 1)
            m = text.find("|", k + 1)
            segs.append((text[j + 1:k], text[k + 1:m], text[m + 1:m + 1 + ln]))
            i = m + 1 + ln + 1
        if not segs:
            raise RuntimeError("响应不是 delta 格式：%r" % text[:200])
        panels = []
        for typ, name, body in segs:
            if typ == "hiddenField":
                self.fields[name] = body
            elif typ == "updatePanel":
                panels.append(body)
            elif typ == "pageRedirect":
                dest = unquote(body)
                if "Login" in dest or "SessionExpired" in dest:
                    raise SessionDead("服务端把我重定向到 %s" % dest)
                raise RuntimeError("意外跳转：%s" % dest)
            elif typ == "error":
                raise RuntimeError("服务端报错：%s" % body[:300])
        panel_html = "\n".join(panels)
        if panel_html:
            self._harvest(BeautifulSoup(panel_html, "html.parser"), self.fields)
        return panel_html

    # ---------- 业务 ----------
    def resolve_country(self, want=None):
        """config 里的 country 可以是代码（45,7）或显示名前缀（China）。"""
        want = (want or self.cfg["country"]).strip()
        if re.fullmatch(r"\d+,\d+", want):
            hit = [c for c in self.countries if c[0] == want]
        else:
            w = want.lower()
            hit = [c for c in self.countries if c[1].lower() == w]
            hit = hit or [c for c in self.countries if c[1].lower().startswith(w)]
        if len(hit) != 1:
            names = "、".join(c[1] for c in hit[:8]) if hit else "没有匹配项"
            raise ConfigError("config.txt 的 country = %r 对不上下拉选项（%s）。"
                              "跑 python qms_probe.py --countries 看完整清单，把那一行的代码填进去"
                              % (want, names))
        return hit[0]

    def select_country(self):
        code, name = self.country = self.resolve_country()
        ov = {SEC + "ddlCountryOfExport": code,
              SEC + "ddlQuarantineDays": "0"}
        for f in ALL_COUNT_FIELDS:
            ov[SEC + f] = ""
        self._post(SEC + "ddlCountryOfExport", ov)
        self.clean = copy.deepcopy(self.fields)
        # 隔离时长和检疫类别由服务端按国家回填（中国 = Schedule III → 30 天、类别 7），
        # 这里直接沿用，不在代码里写死。
        days = self.fields.get(SEC + "ddlQuarantineDays")
        log("选出口国 %s（%s）完成 → 隔离时长回填为 %r" % (name, code, days))
        if not days or days == "0":
            raise ConfigError("%s 选完后服务端没有给出 AQC 隔离时长。Schedule I/II 的国家多半"
                              "不需要（或不能）订 AQC 隔离房，这个工具对它没用" % name)
        return days

    def check(self, date_ddmmyyyy, room):
        # ⚠️ 每次查询前必须还原「干净态」的 ViewState。页面一旦进入结果态，
        # 再点 Check Availability 拿到的是上一次的旧结果（真机和无头都已复现）。
        # 还原快照比页面自带的 Clear 便宜：不用多发一个回发请求。
        if self.clean is None:
            raise RuntimeError("还没选出口国，没有干净态快照")
        self.fields = copy.deepcopy(self.clean)
        pet_f, room_f = ROOMS[room]
        pets, nrooms = counts_for(self.cfg, room)
        ov = {SEC + "ddlCountryOfExport": self.country[0],
              SEC + "ddlQuarantineDays": self.clean[SEC + "ddlQuarantineDays"],
              SEC + "hdnCatSchId": self.clean.get(SEC + "hdnCatSchId")
                                   or self.country[0].split(",")[1],
              SEC + "rdbLstImportType": "1",          # Personal
              SEC + "txtDateOfArrival": date_ddmmyyyy}
        for f in ALL_COUNT_FIELDS:
            ov[SEC + f] = ""
        ov[SEC + pet_f] = str(pets)
        ov[SEC + room_f] = str(nrooms)
        panel = self._post(None, ov, button=SEC + "btnCheckAvailablity")
        return classify(panel)


def _text(html):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


def classify(panel_html):
    """返回 {'s': 'y'|'n'|'?', 'kind':..., 'dates':[iso], 'days':int, 'text':...}"""
    txt = _text(panel_html)
    if "AVAILABILITY DETAILS" in txt:
        blk = re.search(r"Available Dates(.{0,600}?)(Payment Mode|$)", txt, re.S)
        raw = blk.group(1) if blk else txt
        dates = []
        for d in re.findall(r"\b(\d{2}/\d{2}/\d{4})\b", raw):
            iso = dt.datetime.strptime(d, "%d/%m/%Y").date().isoformat()
            if iso not in dates:
                dates.append(iso)
        if dates:
            return {"s": "y", "kind": "list", "dates": dates, "text": raw[:200]}
    m = re.search(r"[Nn]o (?:slots?|spaces?|vacanc\w*) (?:are |is )?available "
                  r"for the (?:next|following|coming) (\d+) days", txt)
    if m:
        return {"s": "n", "kind": "window", "days": int(m.group(1)), "dates": [],
                "text": txt[m.start():m.start() + 120]}
    for pat in (r"should be a future date", r"Please select", r"is required",
                r"Select Quarantine Duration"):
        mm = re.search(pat, txt, re.I)
        if mm:
            return {"s": "?", "kind": "error", "dates": [],
                    "text": txt[max(0, mm.start() - 80):mm.start() + 120]}
    return {"s": "?", "kind": "unknown", "dates": [], "text": txt[:300]}


def fmt(d):
    return d.strftime("%d/%m/%Y")


def cmd_check(args):
    """只验登录态和配置：GET 一次 + 选一次国家，不查任何日期。"""
    q = Qms().bootstrap()
    q.select_country()
    cfg = q.cfg
    log("✅ 登录态有效，配置也对得上：出口国 %s，猫 %d 只 / 狗 %d 只，要扫的房型 %s，往后扫 %d 个月"
        % (q.country[1], cfg["cats"], cfg["dogs"], "、".join(cfg["rooms"]), cfg["months_ahead"]))


def cmd_countries(args):
    q = Qms().bootstrap()
    print("\n代码        国家 / 检疫类别（把左边的代码或右边的英文名填进 config.txt 的 country）")
    for code, name in q.countries:
        print("%-10s  %s" % (code, name))


def cmd_probe(args):
    q = Qms().bootstrap()
    q.select_country()
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    far = dt.date.today() + dt.timedelta(days=210)
    r0 = q.cfg["rooms"][0]
    for room, when in [(r0, tomorrow), (q.cfg["rooms"][-1], tomorrow), (r0, far)]:
        r = q.check(fmt(when), room)
        log("查询 %-12s %s → %s/%s %s%s" % (
            room, fmt(when), r["s"], r["kind"],
            (r["dates"][:6] if r["dates"] else ""),
            (" 窗口%d天" % r["days"]) if r.get("days") else ""))
        if r["kind"] == "unknown":
            log("   原文：" + r["text"])
    log("探针完成，共 %d 个请求" % q.req_count)


def cmd_query(args):
    q = Qms().bootstrap()
    q.select_country()
    r = q.check(args.query, args.room)
    log("查询 %s %s → %s" % (args.room, args.query, r))


def cmd_keepalive(args):
    """每 N 分钟摸一次预约页，记录会话何时失效。"""
    start = dt.datetime.now()
    n = 0
    while True:
        n += 1
        try:
            q = Qms(jitter=(0, 0))
            q.bootstrap()
            alive = dt.datetime.now() - start
            log("保活 #%d OK，已存活 %.1f 小时" % (n, alive.total_seconds() / 3600))
        except SessionDead as e:
            log("保活 #%d 会话已死：%s（存活 %.1f 小时）"
                % (n, e, (dt.datetime.now() - start).total_seconds() / 3600))
            return 1
        except Exception as e:
            log("保活 #%d 出错（不判死）：%r" % (n, e))
        time.sleep(args.keepalive * 60 + random.uniform(-20, 20))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--countries", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--query", metavar="DD/MM/YYYY")
    ap.add_argument("--room", default="cattery_fan", choices=list(ROOMS))
    ap.add_argument("--keepalive", type=int, metavar="MINUTES")
    a = ap.parse_args()
    try:
        if a.check:
            return cmd_check(a)
        if a.countries:
            return cmd_countries(a)
        if a.keepalive:
            return cmd_keepalive(a)
        if a.query:
            return cmd_query(a)
        return cmd_probe(a)
    except SessionDead as e:
        log("会话已失效：%s —— 需要你重新登录并更新 _cookie.txt" % e)
        return 2
    except ConfigError as e:
        log("配置有问题：%s" % e)
        return 4


if __name__ == "__main__":
    sys.exit(main() or 0)
