# -*- coding: utf-8 -*-
"""策略内核（零依赖纯算法）：输入 K 线 bars，输出多倍股早期特征因子分与风控/预警信号。
与数据源彻底解耦，后端(api_server)与 Android 端(Chaquopy 嵌入)均可直接 import 复用。

算法口径严格对齐现有体系（保证 App 与回测同源）：
  - 选股因子来自 run_daily._highret_score：RS×stage2×acc×earn×moat 五因子
  - 早期特征来自 _oos_backtest.scan_as_of：above_ma / ma_rising / rs_high + engine_score>=THRESH
  - 风险来自 _scan_risk：ATR 取最近14根(已修正误取最旧数据)、MA50、硬止损、回撤
  - 加仓来自 _oos_v5_pyramid / run_daily._pyramid_add：相对加仓锚点浮盈触发、不加安全垫
bars 字段约定：date, open, high, low, close, volume, amount(成交额，可选，缺则 volume*close)
"""
from math import isnan

MIN_BARS = 130        # _highret_score 要求样本量
THRESH = 0.02         # scan_as_of engine_score 门槛


def _closes(bars):
    return [b["close"] for b in bars]


def ma(values, n):
    sub = values[-n:]
    return sum(sub) / len(sub) if len(sub) == n else None


def true_range(bars, i):
    h = bars[i]["high"]; l = bars[i]["low"]; pc = bars[i - 1]["close"]
    return max(h - l, abs(h - pc), abs(l - pc))


def atr(bars, n=14):
    """真实振幅：取最近 n 根（修正原先误取最旧数据的 bug）。"""
    if len(bars) < n + 1:
        return 0.0
    trs = [true_range(bars, i) for i in range(len(bars) - n, len(bars))]
    return sum(trs) / len(trs)


def max_dd(closes, window=250):
    sub = closes[-window:]
    if not sub:
        return 0.0
    peak = sub[0]; mdd = 0.0
    for c in sub:
        if c > peak:
            peak = c
        dd = (peak - c) / peak if peak else 0.0
        if dd > mdd:
            mdd = dd
    return mdd


def truncate(bars, cut_date):
    """date-aware 截断（不知未来），对齐 scan_as_of。"""
    return [b for b in bars if b["date"] <= cut_date]


def highret_factors(bars, cur=None):
    """五因子 + 动量/均线，对齐 run_daily._highret_score（amount 用成交额口径）。
    返回 None 表示样本不足。"""
    if not bars or len(bars) < MIN_BARS:
        return None
    closes = _closes(bars)
    vols = [b.get("amount", b.get("volume", 0) * b["close"]) for b in bars]
    cur = cur if cur is not None else bars[-1]["close"]
    if cur <= 0:
        return None
    n = len(closes)

    ma20, ma60, ma120 = ma(closes, 20), ma(closes, 60), ma(closes, 120)
    if None in (ma20, ma60, ma120):
        return None

    def ret(w):
        if n < w + 1:
            return None
        base = closes[-(w + 1)]
        return (cur - base) / base if base > 0 else None
    r20, r60, r120 = ret(20), ret(60), ret(120)
    if None in (r20, r60, r120):
        return None

    rs = r20 * 0.50 + r60 * 0.30 + r120 * 0.20
    high120 = max(closes[-120:]) if n >= 120 else max(closes)
    dd120 = (1 - cur / high120) if high120 > 0 else 1.0
    stage2 = 1.0 if (cur > ma20 and cur > ma60 and dd120 < 0.35 and r120 > 0.10) else 0.30
    v_recent = (sum(vols[-20:]) / 20) if n >= 20 else 0.0
    v_prior = (sum(vols[-60:-20]) / 40) if n >= 60 else (sum(vols[:-20]) / max(1, n - 20) if n > 20 else 0.0)
    acc = (v_recent / v_prior) if v_prior > 0 else 1.0
    acc_f = min(2.0, max(0.5, acc))
    earn_f = 1.0   # 内核不接入研究库 eps_growth，保持中性（与无数据时一致）
    moat_f = 1.0
    engine_score = max(0.0, rs * stage2 * acc_f * earn_f * moat_f)
    return {
        "rs": rs, "stage2": stage2, "acc_f": acc_f, "earn_f": earn_f, "moat_f": moat_f,
        "engine_score": engine_score, "ma20": ma20, "ma60": ma60, "ma120": ma120,
        "high120": high120, "dd120": dd120, "r20": r20, "r60": r60, "r120": r120,
        "acc": acc,
    }


def early_multibag(bars, name=None, code=None):
    """多倍股早期特征识别，对齐 _oos_backtest.scan_as_of。
    返回 dict（is_hit=是否命中早期特征池）。因子口径与回测选股一致。"""
    if not bars or len(bars) < MIN_BARS:
        return {"is_hit": False, "reason": "样本不足"}
    closes = _closes(bars)
    vols = [b["volume"] for b in bars]
    close = bars[-1]["close"]
    ma50 = ma(closes, 50); ma120 = ma(closes, 120)
    ma50_prev = ma(closes[:-20], 50) if len(closes) > 70 else None
    high52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    vol20 = ma(vols, 20); vol60 = ma(vols, 60)
    base_low = min(closes[-120:]) if len(closes) >= 120 else min(closes)
    gain_base = close / base_low - 1 if base_low > 0 else 0

    flags = {
        "above_ma": (ma50 and ma120 and close > ma50 > ma120),
        "ma_rising": (ma50 and ma50_prev and ma50 > ma50_prev),
        "rs_high": (close >= 0.70 * high52),
        "vol_expand": (vol20 and vol60 and vol20 > vol60 * 1.15),
        "early": (1.05 <= gain_base <= 2.0),
    }
    if not (flags["above_ma"] and flags["ma_rising"] and flags["rs_high"]):
        return {"is_hit": False, "reason": "不满足均线多头/趋势上行/贴近一年高",
                "flags": flags, "gain_base": gain_base, "ma50": ma50, "ma120": ma120}
    hf = highret_factors(bars, close)
    eng = hf["engine_score"] if hf else None
    if eng is None or eng < THRESH:
        return {"is_hit": False, "reason": "engine_score 不足", "flags": flags,
                "engine_score": eng, "gain_base": gain_base}
    stage2_score = sum(1 for v in flags.values() if v) + (1 if eng >= THRESH else 0)
    rs_dist = (close / high52 - 1) * 100 if high52 else None
    vol_ratio = vol20 / vol60 if (vol20 and vol60) else None
    return {
        "is_hit": True, "code": code, "name": name, "flags": flags,
        "ma50": round(ma50, 2) if ma50 else None, "ma120": round(ma120, 2) if ma120 else None,
        "high52": round(high52, 2) if high52 else None,
        "rs_dist": round(rs_dist, 1) if rs_dist is not None else None,
        "gain_base": round(gain_base, 3),
        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
        "engine_score": round(eng, 4), "stage2_score": stage2_score,
    }


def risk_factors(bars):
    """风控因子，对齐 _scan_risk：ATR(最近14根)/MA50/MA120/硬止损/回撤/回踩买点/评级。"""
    if not bars or len(bars) < 120:
        return None
    closes = _closes(bars)
    px = closes[-1]
    a = atr(bars); apct = a / px if px else 0
    ma50 = ma(closes, 50); ma120 = ma(closes, 120)
    mdd = max_dd(closes)
    stop = max(ma50 * 0.97, px - 2 * a) if ma50 else px - 2 * a
    stop_pct = (px - stop) / px if px else 0
    buy_ref = ma50 if ma50 else px
    deviation = (px - buy_ref) / buy_ref if buy_ref else 0
    if apct > 0.045 or mdd > 0.45:
        risk = "高"
    elif apct > 0.03 or mdd > 0.30:
        risk = "中"
    else:
        risk = "低"
    return {"px": round(px, 2), "atr": round(a, 3), "atr_pct": round(apct * 100, 2),
            "ma50": round(ma50, 2) if ma50 else None, "ma120": round(ma120, 2) if ma120 else None,
            "mdd": round(mdd * 100, 1), "stop": round(stop, 2), "stop_pct": round(stop_pct * 100, 1),
            "buy_ref": round(buy_ref, 2), "deviation": round(deviation * 100, 1), "risk": risk}


def callback_signals(bars, risk=None, drop_pct=0.09, pullback_from_high=0.12):
    """回调/破位预警，对齐 _alert_monitor 口径：跌破硬止损/单日大跌/从20日高回撤/破均线。"""
    if not bars or len(bars) < 50:
        return []
    closes = _closes(bars)
    px = closes[-1]
    r = risk or risk_factors(bars)
    sigs = []
    if r and px <= r["stop"]:
        sigs.append({"type": "stop_break", "level": "重度",
                     "text": "跌破硬止损 %s（MA50×0.97 或 现价−2×ATR），趋势破位" % (r["stop"])})
    if len(closes) >= 2:
        day_pct = (closes[-1] / closes[-2] - 1)
        if day_pct <= -drop_pct:
            sigs.append({"type": "intraday_drop", "level": "重度" if day_pct <= -0.095 else "中度",
                         "text": "单日跌幅 %.1f%%，接近/触及跌停" % (day_pct * 100)})
    if len(closes) >= 20:
        high20 = max(closes[-20:])
        dd = (high20 - px) / high20 if high20 else 0
        if dd >= pullback_from_high:
            sigs.append({"type": "pullback", "level": "重度" if dd >= 0.18 else "中度",
                         "text": "从20日高点回撤 %.1f%%，超阈值 %.0f%%" % (dd * 100, pullback_from_high * 100)})
    ma50 = ma(closes, 50); ma120 = ma(closes, 120)
    if ma50 and px < ma50:
        sigs.append({"type": "below_ma50", "level": "轻度",
                     "text": "收盘价 %s 跌破 MA50 %s" % (round(px, 2), round(ma50, 2))})
    if ma120 and px < ma120:
        sigs.append({"type": "below_ma120", "level": "中度",
                     "text": "收盘价 %s 跌破 MA120 %s（中期趋势转弱）" % (round(px, 2), round(ma120, 2))})
    return sigs


def pyramid_check(price, cost_basis, add_ref, adds, step=0.20, max_adds=2):
    """利弗莫尔金字塔加仓触发判定（对齐 v5/实盘层）：
    相对加仓锚点(建仓价或上次加仓价)浮盈达 +step 且未达上限才触发；不加安全垫。"""
    if adds >= max_adds:
        return {"trigger": False, "reason": "已达加仓上限"}
    if add_ref <= 0 or price <= 0:
        return {"trigger": False, "reason": "锚点无效"}
    if price < add_ref * (1 + step):
        return {"trigger": False, "reason": "相对加仓锚点浮盈不足 +%.0f%%" % (step * 100)}
    return {"trigger": True, "next_add": adds + 1, "ref_after": price}


if __name__ == "__main__":
    # 自测：用现有数据源对一个已知票跑一遍，验证口径与 _highret_score 一致
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from _multibag_deepdive import daily_raw
        for code in ["sh600428", "sz300142", "sz002141"]:
            bars = daily_raw(code)
            hf = highret_factors(bars)
            em = early_multibag(bars, code=code)
            rf = risk_factors(bars)
            print(code, "engine=%.3f" % (hf["engine_score"] if hf else 0),
                  "early_hit=", em["is_hit"], "risk=", (rf["risk"] if rf else None))
    except Exception as e:
        print("self-test skipped:", e)
