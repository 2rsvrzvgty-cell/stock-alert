# -*- coding: utf-8 -*-
"""
GitHub Actions 免费版每日扫描（零依赖 stdlib，海外 runner 可直接跑）：
  1. 读 gha_config.json 的 watchlist
  2. fetch_daily 拉日线（新浪为主、腾讯兜底，海外 IP 可用）
  3. strategy_core 算回调/破位/风险信号
  4. 命中经 Server酱(微信) 推送，未命中默认静默

用法：
  python gha_scan.py              # 正式跑，SCKEY 从环境变量读取
  python gha_scan.py --dry-run    # 本地测试，不推送只打印
"""
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import strategy_core as sc  # noqa: E402

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

DRY = "--dry-run" in sys.argv


def _sig_text(s):
    return s.get("text") if isinstance(s, dict) else str(s)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


# ============ 数据源（新浪为主、腾讯兜底，零依赖） ============
def _norm_sym(code):
    c = code.lower().replace(".", "")
    if c.startswith("sh") or c.startswith("sz"):
        return c
    return ("sh" + c) if c and c[0] in ("5", "6") else ("sz" + c)


def _fetch_sina(sym, last_n):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, last_n))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                   "Referer": "https://finance.sina.com.cn"})
        txt = urllib.request.urlopen(req, timeout=15, context=ctx).read().decode("gbk", "ignore")
        data = json.loads(txt)
        out = []
        for b in data:
            o = float(b["open"]); c = float(b["close"]); h = float(b["high"]); l = float(b["low"]); v = float(b["volume"])
            out.append({"date": b["day"], "open": o, "close": c, "high": h, "low": l, "volume": v, "amount": v * c})
        return out
    except Exception:
        return []


def _fetch_tencent(sym, last_n):
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (sym, last_n)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=15, context=ctx).read().decode("utf-8", "ignore"))
        node = d.get(sym, {})
        key = "qfqday" if "qfqday" in node else ("day" if "day" in node else None)
        if not key:
            return []
        out = []
        for b in node[key]:
            o = float(b["open"]); c = float(b["close"]); h = float(b["high"]); l = float(b["low"]); v = float(b["volume"])
            out.append({"date": b["date"], "open": o, "close": c, "high": h, "low": l, "volume": v, "amount": v * c})
        return out
    except Exception:
        return []


def fetch_daily(code, last_n=320):
    sym = _norm_sym(code)
    bars = _fetch_sina(sym, last_n)
    if not bars:
        bars = _fetch_tencent(sym, last_n)
    return bars


# ============ 推送（Server酱 → 微信） ============
def push_serverchan(sckey, title, desp):
    url = "https://sctapi.ftqq.com/%s.send" % sckey
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0"})
    r = urllib.request.urlopen(req, timeout=20, context=ctx).read().decode()
    return "OK" if ('"code":0' in r or '"errno":0' in r) else r[:80]


def main():
    cfg = load_json(os.path.join(HERE, "gha_config.json"), {})
    wl = cfg.get("watchlist", {})
    sckey = os.environ.get("SCKEY", "").strip()

    if DRY:
        print("[dry-run] watchlist 数量:", len(wl))

    # 大盘过滤：上证指数收盘是否站上 MA200（risk_on 才报买入区，风险信号不受限）
    market_on = True
    try:
        ib = fetch_daily("sh000001", 260)
        if ib and len(ib) >= 200:
            closes = [b["close"] for b in ib]
            ma200 = sum(closes[-200:]) / 200
            market_on = closes[-1] > ma200
            print("大盘: 上证 %.2f vs MA200 %.2f -> %s" % (closes[-1], ma200,
                  "risk_on(可报买点)" if market_on else "risk_off(只报风险)"))
        else:
            print("大盘: 指数数据不足，买入区默认放行")
    except Exception as e:
        print("大盘: 获取失败 %s，买入区默认放行" % repr(e)[:50])

    hits = []
    for code, meta in wl.items():
        name = meta.get("name", code)
        try:
            bars = fetch_daily(code)
            if not bars:
                print("  无数据:", code, name)
                continue
            risk = sc.risk_factors(bars)
            sigs = sc.callback_signals(bars, risk)
            px = bars[-1]["close"]
            # 买入区提醒：回踩 MA50±3% + 缩量(量比<1) + 大盘 risk_on 三重共振才报
            buy_on = cfg.get("buy_zone", True)
            ma50 = risk.get("ma50") if risk else None
            vols = [b.get("volume", 0) for b in bars[-20:]]
            vol_ratio = (sum(vols[-5:]) / 5) / (sum(vols) / 20) if vols and sum(vols) else 1
            if buy_on and ma50 and ma50 * 0.97 <= px <= ma50 * 1.03:
                if vol_ratio < 1.0 and market_on:
                    sigs.append({"type": "buy_zone", "level": "买入提示",
                                 "text": "缩量回踩买点区 MA50 %.2f（现价 %.2f，量比 %.2f）" % (ma50, px, vol_ratio)})
                elif DRY:
                    print("    [买入区被过滤] %s 量比=%.2f market=%s" % (name, vol_ratio, market_on))
            # 做T提醒（仅 level=core 持仓股）：近5日平均振幅推定当日低吸/高抛区，触及才报
            if meta.get("level") == "core" and len(bars) >= 6:
                amps = [(b["high"] - b["low"]) / b["close"] for b in bars[-5:] if b.get("close")]
                avg_amp = (sum(amps) / len(amps)) if amps else 0.02
                prev_close = bars[-2]["close"]
                buy_ref = prev_close * (1 - avg_amp)
                sell_ref = prev_close * (1 + avg_amp)
                if px <= buy_ref * 1.01:
                    sigs.append({"type": "t0_buy", "level": "做T买点",
                                 "text": "做T低吸区 %.2f（昨收 %.2f，振幅 %.1f%%），现价 %.2f 已到低吸位" %
                                 (buy_ref, prev_close, avg_amp * 100, px)})
                elif px >= sell_ref * 0.99:
                    sigs.append({"type": "t0_sell", "level": "做T卖点",
                                 "text": "做T高抛区 %.2f（昨收 %.2f，振幅 %.1f%%），现价 %.2f 已到高抛位" %
                                 (sell_ref, prev_close, avg_amp * 100, px)})
            if sigs:
                hits.append({"code": code, "name": name, "px": px,
                             "sigs": sigs, "stop": risk.get("stop") if risk else None,
                             "ma50": risk.get("ma50") if risk else None})
            print("  %s %s 现价=%.2f 信号=%s" % (code, name, px,
                  "；".join(s["text"] for s in sigs) if sigs else "无"))
        except Exception as e:
            print("  异常:", code, name, repr(e)[:80])

    # 拼推送内容
    lines = []
    for h in hits:
        sig_desc = "；".join(_sig_text(s) for s in h["sigs"])
        line = "**%s(%s)** 现价 %.2f\n信号：%s" % (h["name"], h["code"], h["px"], sig_desc)
        if h.get("stop"):
            line += "\n硬止损：%.2f" % h["stop"]
        lines.append(line)

    if hits:
        title = "预警 %d 只" % len(hits)
        desp = "\n\n".join(lines)
    else:
        title = "多倍策略预警"
        desp = "今日无回调/破位信号（watchlist %d 只）" % len(wl)

    if DRY:
        print("\n=== 推送内容预览 ===")
        print(title)
        print(desp)
        print("=== 结束（dry-run 不推送） ===")
        return 0

    if not sckey:
        print("SCKEY 未设置，跳过推送")
        return 1

    if hits or cfg.get("push_on_clear", False):
        print("推送标题:", title)
        print("推送内容:\n" + desp)
        r = push_serverchan(sckey, title, desp)
        print("推送结果:", r)
    else:
        print("无信号且 push_on_clear=false，静默结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
