#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QMS 空位扫描（纯 HTTP，不开浏览器）。

双击「扫描.bat」（Mac 双击「扫描.command」）即可：
  1. 用 _cookie.txt 里的登录态，按 config.txt 扫要查的房型
  2. 存一份 qms_<UTC时间>.json（历史快照，放房事件表靠它们两两对比）
  3. 生成 report.html 并自动打开：2×2 象限日历 + 各房型摘要 + 与上一份快照的差异
  4. 登录态失效 → 不发任何扫描请求，直接打开 cookie 更新教程
"""
import datetime as dt
import json
import os
import re
import sys
import webbrowser

import qms_probe as P
from qms_probe import HERE, ConfigError, Qms, SessionDead, log

MAX_REQ_PER_ROOM = 40
SAT_WINDOW = 21            # 近 N 天里
SAT_HITS = 10              # 可约 ≥M 天 → 判定「普遍可约」，停止该房型
SAT_MIN_LEAD = 30          # 但今天 +30 天以内不许触发（否则近期块会把远端数据挡掉）
ROOM_CN = {"cattery_fan": "猫·风扇", "cattery_ac": "猫·空调",
           "kennel_fan": "狗·风扇", "kennel_ac": "狗·空调"}
ROOM_ORDER = ["kennel_fan", "kennel_ac", "cattery_fan", "cattery_ac"]  # 象限：上狗下猫，左扇右空调


def iso(d):
    return d.isoformat()


def ddmm(d):
    return d.strftime("%d/%m/%Y")


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1, day=min(d.day, 28))


def scan_room(q, room, start, end, res):
    cur, reqs, sat_at = start, 0, None
    while cur <= end and reqs < MAX_REQ_PER_ROOM:
        r = q.check(ddmm(cur), room)
        reqs += 1
        if r["kind"] == "list":
            got = [dt.date.fromisoformat(x) for x in r["dates"]]
            lo, hi = min(got), max(got)
            if lo > cur:                                    # 清单起点晚于查询日 → 中间是空的
                d = cur
                while d < lo:
                    res.setdefault(iso(d), {"s": "n", "src": "inferred"})
                    d += dt.timedelta(days=1)
            d = lo
            while d <= hi:
                res[iso(d)] = {"s": "y" if d in got else "n", "src": "listed"}
                d += dt.timedelta(days=1)
            if iso(cur) in res and res[iso(cur)]["s"] == "n":
                res[iso(cur)]["src"] = "confirmed"
                res[iso(cur)]["msg"] = r["text"][:300]
            cur = hi + dt.timedelta(days=1)
        elif r["kind"] == "window":
            n = r.get("days", 30)
            res[iso(cur)] = {"s": "n", "src": "confirmed", "msg": r["text"][:300]}
            for k in range(1, n):
                res.setdefault(iso(cur + dt.timedelta(days=k)), {"s": "n", "src": "window"})
            cur += dt.timedelta(days=n)
        elif r["kind"] == "error":
            log("   %s %s 被校验挡回：%s" % (room, iso(cur), r["text"][:80]))
            cur += dt.timedelta(days=1)
        else:
            res[iso(cur)] = {"s": "?", "src": "unread", "msg": r["text"][:300]}
            log("   ⚠ %s %s 没读懂，原文：%s" % (room, iso(cur), r["text"][:120]))
            cur += dt.timedelta(days=1)

        # 饱和早停（近期块不算数，否则远端边界永远扫不到）
        if cur > dt.date.today() + dt.timedelta(days=SAT_MIN_LEAD):
            win = [iso(cur - dt.timedelta(days=k)) for k in range(1, SAT_WINDOW + 1)]
            hits = sum(1 for k in win if res.get(k, {}).get("s") == "y")
            if hits >= SAT_HITS:
                sat_at = iso(cur)
                log("   %s 在 %s 判定普遍可约，停止扫描" % (room, sat_at))
                break

    if sat_at:                                              # 饱和之后按工作日推定
        d = dt.date.fromisoformat(sat_at)
        while d <= end:
            if iso(d) not in res:
                res[iso(d)] = {"s": "y" if d.weekday() < 5 else "n", "src": "assumed"}
            d += dt.timedelta(days=1)
    return reqs, sat_at


def run_scan(rooms=None):
    q = Qms().bootstrap()
    q.select_country()
    rooms = rooms or q.cfg["rooms"]
    start = dt.date.today() + dt.timedelta(days=1)
    end = add_months(dt.date.today(), q.cfg["months_ahead"])
    results, saturated = {}, {}
    dead = None
    for room in rooms:
        results[room] = {}
        try:
            n, sat = scan_room(q, room, start, end, results[room])
        except Exception as e:
            # 中途出事（掉线 / 网关 502 重试耗尽 / 解析异常）：已扫完的房型照样存档，
            # 别把半小时的活扔了。当前房型只扫了一半，数据不完整，整个丢掉不进存档。
            dead = e
            why = "登录态失效" if isinstance(e, SessionDead) else "出错"
            log("⚠ 扫 %s 时%s：%r（已扫完的房型会照常存档）" % (ROOM_CN[room], why, e))
            results.pop(room)
            break
        if sat:
            saturated[room] = sat
        ys = sum(1 for v in results[room].values() if v["s"] == "y" and v["src"] != "assumed")
        log("%s 扫完：%d 个请求，实查可约 %d 天" % (ROOM_CN[room], n, ys))
    done = [r for r in rooms if r in results]
    now = dt.datetime.now(dt.timezone.utc)
    snap = {
        "source": "qms.avs.gov.sg",
        "scannedAt": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "cfg": {"country": q.country[0], "duration": "0", "from": iso(start),
                "to": iso(end), "rooms": done, "planned": list(rooms),
                "cats": q.cfg["cats"], "dogs": q.cfg["dogs"],
                "catteryRooms": q.cfg["cattery_rooms"], "kennelRooms": q.cfg["kennel_rooms"]},
        "countryText": q.country[1],
        "durationText": "%s days" % q.clean.get(P.SEC + "ddlQuarantineDays"),
        "results": results,
        "saturated": saturated,
        "stopReason": ("全部房型扫描完成" if not dead
                       else "%s，只扫完 %s" % (
                           "登录态中途失效" if isinstance(dead, SessionDead) else "中途出错（%r）" % dead,
                           "、".join(ROOM_CN[r] for r in done))),
        "reqCount": q.req_count,
        "scanner": "qms_scan.py",
    }
    return snap, dead


# ---------------------------------------------------------------- 报告

def same_setup(a, b):
    """国家或宠物/房间数改过的两份快照不能比：查的根本不是同一个问题，会冒出假的放房/被订走。"""
    keys = ("country", "cats", "dogs", "catteryRooms", "kennelRooms")
    return all(a["cfg"].get(k) == b["cfg"].get(k) for k in keys)


def prev_snapshot(exclude, snap):
    """最近一份、设置相同的历史快照。"""
    for f, s in reversed(load_snaps()):
        if f != exclude and s["scannedAt"] < snap["scannedAt"] and same_setup(s, snap):
            return f, s
    return None, None


REAL_SRC = ("listed", "confirmed", "inferred", "window")


def observed(snap, room):
    """真正查出来的日子（不含饱和后推定），差异只在两份快照都看过的日子上比。"""
    m = (snap.get("results") or {}).get(room, {})
    return set(k for k, v in m.items() if v["src"] in REAL_SRC)


def listed_y(snap, room):
    m = (snap.get("results") or {}).get(room, {})
    return set(k for k, v in m.items() if v["s"] == "y" and v["src"] in ("listed", "confirmed"))


def summarize(snap, prev):
    rows = []
    planned = snap["cfg"].get("planned") or ROOM_ORDER
    for room in [r for r in ROOM_ORDER if r in planned]:
        m = (snap["results"] or {}).get(room)
        if not m:
            rows.append({"room": room, "cn": ROOM_CN[room], "first": None,
                         "blk": None, "n_real": 0, "n_assumed": 0, "sat": None,
                         "unread": [], "new": None, "gone": None, "missing": True})
            continue
        ys = sorted(k for k, v in m.items() if v["s"] == "y" and v["src"] != "assumed")
        # 最近的一块可约区间（不设 30 天窗口 —— 经常整整一个月一天都没有）
        blk = merge_blocks(ys)[0] if ys else None
        asm = sum(1 for v in m.values() if v["s"] == "y" and v["src"] == "assumed")
        unread = sorted(k for k, v in m.items() if v["s"] == "?")
        new = gone = None
        if prev and (prev.get("results") or {}).get(room):
            common = observed(snap, room) & observed(prev, room)
            cur_y, old_y = listed_y(snap, room) & common, listed_y(prev, room) & common
            new, gone = sorted(cur_y - old_y), sorted(old_y - cur_y)
        rows.append({"room": room, "cn": ROOM_CN[room], "first": ys[0] if ys else None,
                     "blk": blk, "n_real": len(ys), "n_assumed": asm,
                     "sat": snap.get("saturated", {}).get(room),
                     "unread": unread, "new": new, "gone": gone, "missing": False})
    return rows


# ---------------------------------------------------------------- 放房事件

WD = "一二三四五六日"


def sgt(snap):
    return dt.datetime.strptime(snap["scannedAt"][:19], "%Y-%m-%dT%H:%M:%S") + dt.timedelta(hours=8)


def load_snaps():
    out = []
    for f in sorted(f for f in os.listdir(HERE) if re.match(r"qms_\d{12}\.json$", f)):
        try:
            out.append((f, json.load(open(os.path.join(HERE, f), encoding="utf-8"))))
        except Exception as e:
            log("跳过坏快照 %s：%r" % (f, e))
    out.sort(key=lambda x: x[1]["scannedAt"])
    return out


def _adjacent(prev, d):
    gap = (d - prev).days
    if gap == 1:
        return True
    # 只隔着周末也算同一块 —— 周末本来就不收，不该把一周切成两段
    return 1 < gap <= 3 and all((prev + dt.timedelta(days=k)).weekday() >= 5
                                for k in range(1, gap))


def merge_blocks(dates):
    out = []
    for d in sorted(dt.date.fromisoformat(x) for x in dates):
        if out and _adjacent(out[-1][1], d):
            out[-1][1], out[-1][2] = d, out[-1][2] + 1
        else:
            out.append([d, d, 1])
    return out


def release_events():
    """把所有历史快照两两相邻对比，抽出「整块放出 / 整块被订走」事件。

    只在两份快照都实查过的日期上比：推定的、上次没扫到的房型一律不算，
    否则会冒出一堆假事件（中途掉线的那份快照，没扫到的房型整个没数据）。
    """
    snaps = load_snaps()
    evs, windows = [], []
    for (fa, a), (fb, b) in zip(snaps, snaps[1:]):
        if not same_setup(a, b):
            continue
        ta, tb = sgt(a), sgt(b)
        days = (tb.date() - ta.date()).days
        cover = sorted({(ta.date() + dt.timedelta(days=k)).weekday() for k in range(days + 1)})
        w = {"from": ta, "to": tb, "cover": cover, "n_rel": 0}
        windows.append(w)
        for room in ROOM_ORDER:
            common = observed(a, room) & observed(b, room)
            if not common:
                continue
            ya, yb = listed_y(a, room) & common, listed_y(b, room) & common
            for kind, ds in (("放出", yb - ya), ("被订走", ya - yb)):
                for s, e, n in merge_blocks(ds):
                    evs.append({"w": w, "room": room, "kind": kind,
                                "start": s, "end": e, "n": n,
                                "lead": (s - tb.date()).days})
                    if kind == "放出":
                        w["n_rel"] += 1
    return evs, windows


def events_html():
    evs, windows = release_events()
    if not evs:
        return ""
    h = ['<div class="card"><h2>放房事件时间表</h2>',
         '<p style="color:var(--sub);font-size:13px;margin:0 0 12px">'
         '把历史快照两两相邻对比，抽出成块出现/消失的入境日。'
         '<b>只在两份快照都实查过的日期上比</b>，推定的和上次没扫到的房型不参与，所以不会有假事件。'
         '时间窗越宽，能定位的时刻越模糊——想缩窄就固定时间多扫几轮。</p>',
         '<table><tr><th>时间窗（新加坡时间）</th><th>覆盖</th><th>房型</th>'
         '<th>事件</th><th>入境日区块</th><th>提前量</th></tr>']
    for e in sorted(evs, key=lambda x: (x["w"]["to"], x["room"]), reverse=True):
        w = e["w"]
        blk = ("%s ~ %s" % (e["start"].isoformat()[5:], e["end"].isoformat()[5:])
               if e["start"] != e["end"] else e["start"].isoformat()[5:])
        cls = "new" if e["kind"] == "放出" else "gone"
        h.append('<tr><td>%s 周%s %s → %s 周%s %s</td><td>%s</td><td>%s</td>'
                 '<td><span class="tag %s">%s</span></td><td>%s（%d 天）</td><td>%d 天后</td></tr>'
                 % (w["from"].strftime("%m-%d"), WD[w["from"].weekday()], w["from"].strftime("%H:%M"),
                    w["to"].strftime("%m-%d"), WD[w["to"].weekday()], w["to"].strftime("%H:%M"),
                    "".join(WD[c] for c in w["cover"]),
                    ROOM_CN[e["room"]], cls, e["kind"], blk, e["n"], e["lead"]))
    h.append("</table>")

    hit = [w for w in windows if w["n_rel"]]
    miss = [w for w in windows if not w["n_rel"]]
    tal = lambda ws: "　".join("周%s %d" % (WD[d], sum(1 for w in ws if d in w["cover"]))
                              for d in range(7))
    h.append('<div class="legend"><b>星期覆盖统计</b>（判断「几号放房」用这个，别只看单次）'
             '<br>有放房的时间窗 %d 个，各星期出现次数：%s'
             '<br>没放房的时间窗 %d 个，各星期出现次数：%s'
             '<br>某个星期在上行多、下行少 → 它是嫌疑；两行都多 → 只是被扫得频繁，不能当证据。</div></div>'
             % (len(hit), tal(hit), len(miss), tal(miss)))
    return "".join(h)


CSS = """
:root{--bg:#f2f2f7;--card:#fff;--ink:#1d1d1f;--sub:#6e6e73;--line:#e5e5ea;
      --ok:#34c759;--soft:#a9e6b8;--no:#d1d1d6;--warn:#ff9f0a}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px 60px;background:var(--bg);color:var(--ink);
     font:15px/1.5 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1040px;margin:0 auto}
h1{font-size:24px;margin:0 0 4px}.meta{color:var(--sub);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border-radius:14px;padding:16px 18px;margin-bottom:16px;
      box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card h2{font-size:16px;margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--sub);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.big{font-size:17px;font-weight:600}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:12px;margin:1px 3px 1px 0}
.tag.new{background:#e4f8e9;color:#1a7f37}.tag.gone{background:#fdecec;color:#c1121f}
.tag.warn{background:#fff4e0;color:#9a6100}
.months{display:flex;flex-wrap:wrap;gap:14px}
.mon{background:var(--card);border-radius:12px;padding:10px 12px 12px;
     box-shadow:0 1px 3px rgba(0,0,0,.06)}
.mon h3{font-size:13px;margin:0 0 6px;color:var(--sub)}
.grid{display:grid;grid-template-columns:repeat(7,26px);gap:3px}
.dow{font-size:10px;color:var(--sub);text-align:center}
.cell{width:26px;height:26px;border-radius:6px;background:#fafafa;border:1px solid var(--line);
      display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;overflow:hidden}
.cell.empty{background:transparent;border:0}
.q{background:var(--no)}.q.y{background:var(--ok)}.q.a{background:var(--soft)}
.q.u{background:var(--warn)}.q.blank{background:#f7f7f7}
.legend{font-size:12px;color:var(--sub);margin-top:10px;line-height:1.9}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:-1px;margin:0 3px}
.dead{background:#fdecec;border:1px solid #f5c2c7;color:#842029}
code{background:#f2f2f7;padding:1px 5px;border-radius:4px;font-size:13px}
"""


def cell_html(snap, d):
    qs = []
    for room in ROOM_ORDER:
        v = (snap["results"].get(room) or {}).get(iso(d))
        if not v:
            qs.append('<div class="q blank"></div>')
        elif v["s"] == "?":
            qs.append('<div class="q u"></div>')
        elif v["s"] == "y":
            qs.append('<div class="q %s"></div>' % ("a" if v["src"] == "assumed" else "y"))
        else:
            qs.append('<div class="q"></div>')
    tip = iso(d) + " " + "　".join(
        "%s:%s" % (ROOM_CN[r], {"y": "可约", "n": "无位", "?": "没读懂"}.get(
            ((snap["results"].get(r) or {}).get(iso(d)) or {}).get("s"), "未扫"))
        for r in ROOM_ORDER)
    return '<div class="cell" title="%s">%s</div>' % (tip, "".join(qs))


def calendar_html(snap):
    start = dt.date.fromisoformat(snap["cfg"]["from"])
    end = dt.date.fromisoformat(snap["cfg"]["to"])
    out = ['<div class="months">']
    cur = start.replace(day=1)
    while cur <= end:
        nxt = add_months(cur, 1).replace(day=1)
        out.append('<div class="mon"><h3>%s</h3><div class="grid">' % cur.strftime("%Y年%m月"))
        out += ['<div class="dow">%s</div>' % x for x in "一二三四五六日"]
        d = cur
        out += ['<div class="cell empty"></div>'] * d.weekday()
        while d < nxt:
            out.append(cell_html(snap, d) if start <= d <= end
                       else '<div class="cell empty"></div>')
            d += dt.timedelta(days=1)
        out.append("</div></div>")
        cur = nxt
    out.append("</div>")
    return "".join(out)


def build_report(snap, prev_name, prev, json_name):
    rows = summarize(snap, prev)
    sgt = dt.datetime.strptime(snap["scannedAt"][:19], "%Y-%m-%dT%H:%M:%S") + dt.timedelta(hours=8)
    h = ['<!doctype html><html lang="zh"><head><meta charset="utf-8">',
         '<title>QMS 空位看板</title><style>%s</style></head><body><div class="wrap">' % CSS,
         '<h1>新加坡宠物隔离 · 空位看板</h1>',
         '<div class="meta">扫描时间 %s（新加坡时间）　·　出口国 %s，隔离 %s　·　%d 个请求　·　'
         '数据存档 <code>%s</code>　·　对比基准 <code>%s</code></div>'
         % (sgt.strftime("%Y-%m-%d %H:%M"), snap.get("countryText", "?"),
            snap.get("durationText", "?"), snap["reqCount"], json_name, prev_name or "无")]

    h.append('<div class="card"><h2>各房型摘要</h2><table>'
             '<tr><th>房型</th><th>最早可约</th><th>最近一块可约区间</th>'
             '<th>实查可约</th><th>较上次</th></tr>')
    for r in rows:
        if r["missing"]:
            h.append('<tr><td class="big">%s</td><td colspan="4" style="color:var(--sub)">'
                     '本轮没扫到（中途掉线或出错，见 stopReason）——这不是"没有空位"，是没有数据</td></tr>'
                     % r["cn"])
            continue
        chg = ""
        if r["new"] is None:
            chg = '<span class="tag">首次扫描</span>'
        else:
            for d in r["new"][:6]:
                chg += '<span class="tag new">+%s</span>' % d[5:]
            for d in r["gone"][:6]:
                chg += '<span class="tag gone">−%s</span>' % d[5:]
            if not chg:
                chg = '<span style="color:var(--sub)">无变化</span>'
        if r["blk"]:
            bs, be, bn = r["blk"]
            near = ("%s ~ %s（%d 天）" % (bs.isoformat()[5:], be.isoformat()[5:], bn)
                    if bs != be else "%s（1 天）" % bs.isoformat()[5:])
        else:
            near = '<span style="color:var(--sub)">整个扫描范围内都没有</span>'
        extra = ("，另推定 %d 天" % r["n_assumed"]) if r["n_assumed"] else ""
        warn = ('<span class="tag warn">%d 天没读懂</span>' % len(r["unread"])) if r["unread"] else ""
        if r["first"]:
            fd = dt.date.fromisoformat(r["first"])
            first = "%s 周%s<br><span style='color:var(--sub);font-size:12px'>%d 天后</span>" % (
                r["first"], WD[fd.weekday()], (fd - dt.date.today()).days)
        else:
            first = "—"
        h.append('<tr><td class="big">%s</td><td class="big">%s</td><td>%s</td>'
                 '<td>%d 天%s %s</td><td>%s</td></tr>'
                 % (r["cn"], first, near, r["n_real"], extra, warn, chg))
    h.append('</table></div>')

    h.append(events_html())
    h.append('<div class="card"><h2>日历</h2>' + calendar_html(snap) +
             '<div class="legend">每格是 2×2 象限，方位固定：<b>上排狗／下排猫，左列风扇／右列空调</b>。'
             '<br><i style="background:var(--ok)"></i>实查可约　'
             '<i style="background:var(--soft)"></i>饱和后推定（未实查）　'
             '<i style="background:var(--no)"></i>无位　'
             '<i style="background:var(--warn)"></i>没读懂　'
             '<i style="background:#f7f7f7;border:1px solid #e5e5ea"></i>未扫描'
             '<br>口径：绿=可作为<b>入境日</b>（从这天起 30 天档期都订得到），不是"这天有空床"。</div></div>')
    h.append('</div></body></html>')
    return "".join(h)


HOWTO = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>更新 QMS 登录态</title><style>%s
ol{padding-left:20px}li{margin:10px 0}
kbd{background:#fff;border:1px solid #d1d1d6;border-bottom-width:2px;border-radius:5px;
    padding:1px 6px;font-size:13px}
</style></head><body><div class="wrap">
<h1>需要（重新）填登录态</h1>
<div class="card dead"><b>扫描没有执行。</b>要么是第一次用、<code>_cookie.txt</code> 还没填，
要么是里面的会话已经过期。按下面四步填好，再双击「扫描」即可。
登录要过图片验证码，只能你本人来，这一步没法自动化。</div>

<div class="card"><h2>四步更新</h2><ol>
<li>用 Chrome 打开 <a href="https://qms.avs.gov.sg/" target="_blank">qms.avs.gov.sg</a>，
用<b>你自己的</b> QMS 账号正常登录（账号 + 密码 + 图片验证码），进到
<b>Reservation → Quarantine Reservation</b> 页面。</li>
<li>打开开发者工具：Windows 按 <kbd>F12</kbd>，Mac 按 <kbd>⌘</kbd><kbd>⌥</kbd><kbd>I</kbd>。
顶部切到 <b>Application</b>（中文界面叫「应用」）→ 左栏 <b>Storage → Cookies →
https://qms.avs.gov.sg</b>。</li>
<li>列表里找 <b>Name 一列是 ASP.NET_SessionId</b> 的那一行，双击它的 <b>Value</b>，
全选复制（一串 24 位左右的小写字母和数字）。
用记事本 / 文本编辑打开项目目录下的 <code>_cookie.txt</code>，整个文件只留这一行：
<div style="margin:8px 0"><code>ASP.NET_SessionId=粘在这里</code></div>
存盘。列表里其他几条（<code>_sp_id…</code>、<code>_sp_ses…</code>、<code>_toffsuid</code> 等）
是网站统计用的，扫描用不上，不用复制。</li>
<li><b>直接关掉 QMS 那个标签页</b>（别点 Logout），然后马上双击「扫描」。</li>
</ol></div>

<div class="card"><h2>几件要知道的事</h2>
<ul>
<li>QMS 页面<b>闲置约 20 分钟</b>会把会话一起作废。实测是开着不动的那个标签页触发的，
所以复制完 cookie 就关标签页；扫描脚本自己的请求反而会给会话续命。</li>
<li>点 Logout 会让 <code>_cookie.txt</code> 立刻失效，关标签页不会。</li>
<li><code>_cookie.txt</code> 等于你的 QMS 登录态，<b>别发给别人、别传到网上</b>。
反过来，也别用别人的 cookie：那是在用别人的账号查。</li>
<li>扫描器只会按 Check Availability 查询，代码里锁死了不会提交、付款或改期。</li>
</ul></div>
</div></body></html>""" % CSS


ROOMS_ARG = [a for a in sys.argv[1:] if a in P.ROOMS]


def rebuild_report():
    """不发任何请求，用已有快照重建 report.html（改了报告样式或想看事件表时用）。"""
    snaps = load_snaps()
    if not snaps:
        sys.exit("目录里没有 qms_*.json 快照")
    name, snap = snaps[-1]
    pname, prev = prev_snapshot(name, snap)
    rep = os.path.join(HERE, "report.html")
    open(rep, "w", encoding="utf-8").write(build_report(snap, pname, prev, name))
    log("已用 %s 重建 %s（未联网）" % (name, rep))
    webbrowser.open("file:///" + rep.replace("\\", "/"))
    return 0


def main():
    if "--report" in sys.argv:
        return rebuild_report()
    howto = os.path.join(HERE, "cookie_howto.html")
    open(howto, "w", encoding="utf-8").write(HOWTO)
    try:
        snap, dead = run_scan(ROOMS_ARG or None)
    except ConfigError as e:                       # config.txt 写错 / 国家对不上，没扫
        log("配置有问题：%s" % e)
        return 4
    except SessionDead as e:                       # 一开始就没登录态，一个请求都没发
        log("登录态失效：%s（没有发出任何扫描请求）" % e)
        webbrowser.open("file:///" + howto.replace("\\", "/"))
        return 2
    except P.TransientError as e:                  # 开局就连不上（网关 5xx），没扫到任何东西
        log("服务端不可用：%s（还没扫到任何房型，过半小时再跑）" % e)
        return 3

    name = "qms_%s.json" % dt.datetime.strptime(
        snap["scannedAt"][:19], "%Y-%m-%dT%H:%M:%S").strftime("%Y%m%d%H%M")
    path = os.path.join(HERE, name)
    json.dump(snap, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log("已存档 %s" % name)

    pname, prev = prev_snapshot(name, snap)
    rep = os.path.join(HERE, "report.html")
    open(rep, "w", encoding="utf-8").write(build_report(snap, pname, prev, name))
    log("报告：%s" % rep)
    webbrowser.open("file:///" + rep.replace("\\", "/"))
    if isinstance(dead, SessionDead):
        webbrowser.open("file:///" + howto.replace("\\", "/"))
        return 2
    if dead:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
