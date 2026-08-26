"""
fetch_financials.py
--------------------
ดึงงบการเงินหุ้นไทยจาก Yahoo Finance (ผ่าน yfinance) แล้วคำนวณตัวชี้วัด 16 ข้อ
จากนั้นบันทึก (upsert) ลง Supabase ตาราง `financials`

ใช้ได้ 2 แบบ:
  python fetch_financials.py --ticker PTT.BK        -> อัปเดตหุ้นตัวเดียว
  python fetch_financials.py --all                  -> อัปเดตทุกตัวใน watchlist

ต้องตั้งค่า environment variables ก่อนรัน:
  SUPABASE_URL
  SUPABASE_SERVICE_KEY   (service_role key เท่านั้น ห้ามใช้ anon key)
"""

import os
import sys
import argparse
import time
import math
import requests
import yfinance as yf
import pandas as pd

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _clean(v):
    """แปลงค่า NaN / None / inf ให้เป็น None (JSON-safe)"""
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)) and (math.isnan(v) or math.isinf(v)):
            return None
    except TypeError:
        return None
    if isinstance(v, (pd.Timestamp,)):
        return v.date().isoformat()
    return float(v) if isinstance(v, (int, float)) else v


def normalize_ticker(raw: str) -> str:
    """เติม .BK ให้อัตโนมัติถ้าผู้ใช้พิมพ์แค่ชื่อย่อ เช่น 'ptt' -> 'PTT.BK'"""
    t = raw.strip().upper()
    if not t:
        return t
    if "." not in t:
        t = f"{t}.BK"
    return t


def _get_interest_bearing_debt(balance_df, col):
    """
    คำนวณหนี้สินที่มีภาระดอกเบี้ย (IBD) ให้ครบถ้วนที่สุดเท่าที่ yfinance มีข้อมูลให้:
    - เงินกู้ยืม/หุ้นกู้ระยะสั้น + ระยะยาว
    - หนี้สินตามสัญญาเช่า (lease liabilities) ทั้งระยะสั้นและระยะยาว
    คืนค่า (total_ibd, short_term_portion)
    """
    # yfinance บางเวอร์ชันให้ field ที่รวมหนี้กู้ยืม+สัญญาเช่าไว้ให้แล้วในตัวเดียว ลองหาก่อน
    combined_short = _row_get(balance_df, ["Current Debt And Capital Lease Obligation"], col)
    combined_long = _row_get(balance_df, ["Long Term Debt And Capital Lease Obligation"], col)
    if combined_short is not None or combined_long is not None:
        short_total = combined_short or 0
        long_total = combined_long or 0
        return (short_total + long_total, short_total)

    # ถ้าไม่มี field รวม ให้ดึงหนี้กู้ยืม/หุ้นกู้ และหนี้สินตามสัญญาเช่าแยกกัน แล้วบวกเอง
    short_debt = _row_get(balance_df, ["Current Debt", "Short Long Term Debt", "CurrentDebt"], col) or 0
    long_debt = _row_get(balance_df, ["Long Term Debt", "LongTermDebt"], col) or 0
    lease_short = _row_get(balance_df, ["Current Capital Lease Obligation", "Capital Lease Obligations"], col) or 0
    lease_long = _row_get(balance_df, ["Long Term Capital Lease Obligation"], col) or 0

    if not (short_debt or long_debt or lease_short or lease_long):
        # ทางเลือกสุดท้าย: field รวมของ yfinance เอง (มักรวมสัญญาเช่าไว้แล้วในหลายกรณี)
        total_debt = _row_get(balance_df, ["Total Debt", "TotalDebt"], col)
        return (total_debt, None)

    total_short = short_debt + lease_short
    total_ibd = short_debt + long_debt + lease_short + lease_long
    return (total_ibd, total_short)


def _row_get(df: pd.DataFrame, keys, col):
    """ดึงค่าจาก DataFrame งบการเงินของ yfinance โดยลองหลายชื่อ field เผื่อ Yahoo เปลี่ยนชื่อ"""
    if df is None or df.empty or col not in df.columns:
        return None
    for k in keys:
        if k in df.index:
            val = df.loc[k, col]
            if pd.notna(val):
                return float(val)
    return None


def _calc_period_metrics(income_df, balance_df, cashflow_df, col, prev_col=None):
    """คำนวณตัวชี้วัดทั้งหมดสำหรับ 1 คอลัมน์ (1 งวดบัญชี)"""

    revenue = _row_get(income_df, ["Total Revenue", "TotalRevenue"], col)
    net_income = _row_get(income_df, ["Net Income", "NetIncome", "Net Income Common Stockholders"], col)
    ebit = _row_get(income_df, ["EBIT", "Operating Income", "OperatingIncome"], col)
    ebitda = _row_get(income_df, ["EBITDA", "Normalized EBITDA"], col)
    interest_expense = _row_get(income_df, ["Interest Expense", "InterestExpense", "Interest Expense Non Operating"], col)
    depreciation = _row_get(cashflow_df, ["Depreciation And Amortization", "Depreciation", "DepreciationAndAmortization"], col)

    if ebitda is None and ebit is not None and depreciation is not None:
        ebitda = ebit + depreciation

    if interest_expense is not None:
        interest_expense = abs(interest_expense)

    total_liabilities = _row_get(balance_df, ["Total Liabilities Net Minority Interest", "Total Liab", "TotalLiabilitiesNetMinorityInterest"], col)
    total_equity = _row_get(balance_df, ["Stockholders Equity", "Total Stockholder Equity", "TotalEquityGrossMinorityInterest"], col)
    total_assets = _row_get(balance_df, ["Total Assets", "TotalAssets"], col)
    cash = _row_get(balance_df, ["Cash And Cash Equivalents", "Cash", "CashAndCashEquivalents", "Cash Cash Equivalents And Short Term Investments"], col)
    current_assets = _row_get(balance_df, ["Current Assets", "Total Current Assets", "CurrentAssets"], col)
    current_liabilities = _row_get(balance_df, ["Current Liabilities", "Total Current Liabilities", "CurrentLiabilities"], col)

    interest_bearing_debt, short_debt = _get_interest_bearing_debt(balance_df, col)
    short_debt = short_debt or 0

    cfo = _row_get(cashflow_df, ["Operating Cash Flow", "Total Cash From Operating Activities", "OperatingCashFlow"], col)
    capex = _row_get(cashflow_df, ["Capital Expenditure", "CapitalExpenditure"], col)
    fcf = None
    if cfo is not None and capex is not None:
        fcf = cfo + capex  # capex ใน yfinance มักเป็นค่าติดลบอยู่แล้ว
    elif cfo is not None:
        fcf = cfo

    de_ratio = (total_liabilities / total_equity) if (total_liabilities is not None and total_equity) else None
    current_ratio = (current_assets / current_liabilities) if (current_assets is not None and current_liabilities) else None
    interest_coverage = (ebit / interest_expense) if (ebit is not None and interest_expense) else None
    net_debt_ebitda = None
    if interest_bearing_debt is not None and cash is not None and ebitda:
        net_debt_ebitda = (interest_bearing_debt - cash) / ebitda
    roe = (net_income / total_equity * 100) if (net_income is not None and total_equity) else None
    roa = (net_income / total_assets * 100) if (net_income is not None and total_assets) else None

    revenue_growth = None
    earnings_growth = None
    ebitda_growth = None
    prev_interest_bearing_debt = None
    prev_interest_expense = None
    prev_fcf = None
    prev_current_ratio = None
    prev_ebitda = None

    if prev_col is not None:
        prev_revenue = _row_get(income_df, ["Total Revenue", "TotalRevenue"], prev_col)
        prev_net_income = _row_get(income_df, ["Net Income", "NetIncome", "Net Income Common Stockholders"], prev_col)
        if revenue is not None and prev_revenue:
            revenue_growth = (revenue - prev_revenue) / abs(prev_revenue) * 100
        if net_income is not None and prev_net_income:
            earnings_growth = (net_income - prev_net_income) / abs(prev_net_income) * 100

        prev_ebit = _row_get(income_df, ["EBIT", "Operating Income", "OperatingIncome"], prev_col)
        prev_depreciation = _row_get(cashflow_df, ["Depreciation And Amortization", "Depreciation", "DepreciationAndAmortization"], prev_col)
        prev_ebitda = _row_get(income_df, ["EBITDA", "Normalized EBITDA"], prev_col)
        if prev_ebitda is None and prev_ebit is not None and prev_depreciation is not None:
            prev_ebitda = prev_ebit + prev_depreciation
        if ebitda is not None and prev_ebitda:
            ebitda_growth = (ebitda - prev_ebitda) / abs(prev_ebitda) * 100

        prev_interest_expense = _row_get(income_df, ["Interest Expense", "InterestExpense", "Interest Expense Non Operating"], prev_col)
        if prev_interest_expense is not None:
            prev_interest_expense = abs(prev_interest_expense)

        prev_interest_bearing_debt, _prev_short_debt = _get_interest_bearing_debt(balance_df, prev_col)

        prev_cfo = _row_get(cashflow_df, ["Operating Cash Flow", "Total Cash From Operating Activities", "OperatingCashFlow"], prev_col)
        prev_capex = _row_get(cashflow_df, ["Capital Expenditure", "CapitalExpenditure"], prev_col)
        if prev_cfo is not None and prev_capex is not None:
            prev_fcf = prev_cfo + prev_capex
        elif prev_cfo is not None:
            prev_fcf = prev_cfo

        prev_current_assets = _row_get(balance_df, ["Current Assets", "Total Current Assets", "CurrentAssets"], prev_col)
        prev_current_liabilities = _row_get(balance_df, ["Current Liabilities", "Total Current Liabilities", "CurrentLiabilities"], prev_col)
        if prev_current_assets is not None and prev_current_liabilities:
            prev_current_ratio = prev_current_assets / prev_current_liabilities

    def _trend(cur, prev, higher_is_better=True):
        if cur is None or prev is None or prev == 0:
            return None
        change = (cur - prev) / abs(prev)
        if abs(change) < 0.02:  # เปลี่ยนแปลงน้อยกว่า 2% ถือว่าทรงตัว
            return "flat"
        improving = change > 0 if higher_is_better else change < 0
        return "up" if (change > 0) else "down"

    debt_trend = _trend(interest_bearing_debt, prev_interest_bearing_debt, higher_is_better=False)
    interest_expense_trend = _trend(interest_expense, prev_interest_expense, higher_is_better=False)
    fcf_trend = _trend(fcf, prev_fcf, higher_is_better=True)
    current_ratio_trend = _trend(current_ratio, prev_current_ratio, higher_is_better=True)
    net_profit_trend = _trend(net_income, prev_net_income if prev_col is not None else None, higher_is_better=True)

    raw = {
        "total_liabilities": total_liabilities,
        "interest_bearing_debt": interest_bearing_debt,
        "short_term_debt": short_debt,
        "de_ratio": de_ratio,
        "net_debt_to_ebitda": net_debt_ebitda,
        "ebitda": ebitda,
        "ebit": ebit,
        "interest_expense": interest_expense,
        "interest_coverage_ratio": interest_coverage,
        "cfo": cfo,
        "fcf": fcf,
        "cash_and_equivalents": cash,
        "current_ratio": current_ratio,
        "net_income": net_income,
        "roe": roe,
        "roa": roa,
        "revenue_growth": revenue_growth,
        "earnings_growth": earnings_growth,
        "ebitda_growth": ebitda_growth,
        "total_equity": total_equity,
        "total_assets": total_assets,
        "revenue": revenue,
        "interest_bearing_debt_prev": prev_interest_bearing_debt,
        "interest_expense_prev": prev_interest_expense,
        "fcf_prev": prev_fcf,
        "current_ratio_prev": prev_current_ratio,
        "ebitda_prev": prev_ebitda,
        "debt_trend": debt_trend,
        "interest_expense_trend": interest_expense_trend,
        "fcf_trend": fcf_trend,
        "current_ratio_trend": current_ratio_trend,
        "net_profit_trend": net_profit_trend,
        "ib_de_ratio": None,       # จะถูกเติมค่าจริงหลังเรียก _calc_risk_score ด้านล่าง
        "ib_debt_to_asset": None,
    }

    risk = _calc_risk_score(raw)
    raw.update(risk)

    return {k: _clean(v) if not isinstance(v, str) else v for k, v in raw.items()}


def _scale(value, bad, good):
    """แปลงค่าดิบเป็นคะแนน 0-100 แบบเชิงเส้น (higher-is-better ถ้า good>bad, lower-is-better ถ้า good<bad)"""
    if value is None:
        return None
    if good == bad:
        return 50.0
    t = (value - bad) / (good - bad)
    t = max(0.0, min(1.0, t))
    return t * 100.0


def _trend_score(trend, good_dir="down"):
    """ให้คะแนน trend: ไปทางที่ดี=100, ทรงตัว=50, ไปทางที่แย่=0, ไม่มีข้อมูล=None (จะไม่นับรวม)"""
    if trend is None:
        return None
    if trend == "flat":
        return 50.0
    return 100.0 if trend == good_dir else 0.0


def _weighted_avg(items):
    """items = [(score_or_None, weight), ...] คืนค่าเฉลี่ยถ่วงน้ำหนักเฉพาะตัวที่มีข้อมูลจริง (re-normalize น้ำหนัก)"""
    valid = [(s, w) for s, w in items if s is not None]
    if not valid:
        return None
    total_w = sum(w for _, w in valid)
    return sum(s * w for s, w in valid) / total_w


def _calc_risk_score(m):
    """
    คำนวณ Bond Risk Score (0-100, ยิ่งสูงยิ่งความเสี่ยงต่ำ) ตามน้ำหนัก 6 หมวด:
    Leverage 30% / Interest Coverage 25% / Cash Flow 22% / Liquidity 10% / Profitability 8% / Growth 5%
    เกณฑ์ตัวเลข (good/bad) เป็นเกณฑ์อย่างง่ายที่ปรับได้ ใช้อ้างอิงเชิงเปรียบเทียบ ไม่ใช่มาตรฐานสถาบันจัดอันดับ
    """
    # ทางเลือกที่ 1: ใช้ "หนี้สินที่มีภาระดอกเบี้ย" แทน "หนี้สินรวม" ในการให้คะแนน D/E และ Debt/Asset
    # เพราะหนี้สินรวมของบางธุรกิจ (สถาบันการเงิน/ประกัน/รับเงินล่วงหน้าลูกค้า) ไม่ได้สะท้อนความเสี่ยงทางการเงินจริง
    ib_de_ratio = (m["interest_bearing_debt"] / m["total_equity"]) if (m["interest_bearing_debt"] is not None and m["total_equity"]) else None
    ib_debt_to_asset = (m["interest_bearing_debt"] / m["total_assets"]) if (m["interest_bearing_debt"] is not None and m["total_assets"]) else None

    ib_debt_share = (m["interest_bearing_debt"] / m["total_liabilities"]) if (m["interest_bearing_debt"] is not None and m["total_liabilities"]) else None
    ebitda_interest = (m["ebitda"] / m["interest_expense"]) if (m["ebitda"] is not None and m["interest_expense"]) else None
    cfo_margin = (m["cfo"] / m["revenue"]) if (m["cfo"] is not None and m["revenue"]) else None
    fcf_margin = (m["fcf"] / m["revenue"]) if (m["fcf"] is not None and m["revenue"]) else None
    cfo_to_debt = (m["cfo"] / m["interest_bearing_debt"]) if (m["cfo"] is not None and m["interest_bearing_debt"]) else (100.0 if m["cfo"] is not None else None)
    cash_to_short_debt = None
    if m["short_term_debt"] is not None and m["short_term_debt"] > 0 and m["cash_and_equivalents"] is not None:
        cash_to_short_debt = m["cash_and_equivalents"] / m["short_term_debt"]
    net_profit_margin = (m["net_income"] / m["revenue"]) if (m["net_income"] is not None and m["revenue"]) else None

    # ---- เงื่อนไขบังคับ: ถ้า EBITDA หรือ กำไรสุทธิ ติดลบ ห้ามนำไปคำนวณอัตราส่วนต่อ ----
    # ให้คะแนนช่องที่เกี่ยวข้องเป็น 0 (แย่ที่สุด) ทันที แทนการปล่อยให้เครื่องหมายลบไปพลิกทิศทางของอัตราส่วน
    ebitda_is_negative = m["ebitda"] is not None and m["ebitda"] < 0
    net_income_is_negative = m["net_income"] is not None and m["net_income"] < 0

    # ---- 1) Leverage 30% ----
    leverage = _weighted_avg([
        (_scale(ib_de_ratio, bad=2.0, good=0.3), 10),
        (0.0 if ebitda_is_negative else _scale(m["net_debt_to_ebitda"], bad=6.0, good=0.0), 8),
        (_scale(ib_debt_share, bad=0.9, good=0.2), 6),
        (_trend_score(m["debt_trend"], good_dir="down"), 4),
        (_scale(ib_debt_to_asset, bad=0.6, good=0.15), 2),
    ])

    # ---- 2) Interest Coverage 25% ----
    interest_cov = _weighted_avg([
        (_scale(m["interest_coverage_ratio"], bad=1.0, good=10.0), 12),
        (0.0 if ebitda_is_negative else _scale(ebitda_interest, bad=2.0, good=15.0), 6),
        (_scale(m["interest_coverage_ratio"], bad=1.0, good=10.0), 4),  # EBIT/Interest = สูตรเดียวกับ Interest Coverage Ratio
        (_trend_score(m["interest_expense_trend"], good_dir="down"), 3),
    ])

    # ---- 3) Cash Flow 22% ----
    cashflow = _weighted_avg([
        (_scale(cfo_margin, bad=0.0, good=0.15), 8),
        (_scale(fcf_margin, bad=-0.05, good=0.08), 8),
        (_scale(cfo_to_debt, bad=0.05, good=0.30), 4),
        (_trend_score(m["fcf_trend"], good_dir="up"), 2),
    ])

    # ---- 4) Liquidity 10% ----
    liquidity = _weighted_avg([
        (_scale(m["current_ratio"], bad=0.8, good=2.0), 5),
        (_scale(cash_to_short_debt, bad=0.2, good=1.5), 3),
        (_trend_score(m["current_ratio_trend"], good_dir="up"), 2),
    ])

    # ---- 5) Profitability 8% ----
    profitability = _weighted_avg([
        (_scale(net_profit_margin, bad=0.0, good=0.15), 2.5),
        (0.0 if net_income_is_negative else _scale(m["roa"], bad=0.0, good=10.0), 2),
        (0.0 if net_income_is_negative else _scale(m["roe"], bad=0.0, good=15.0), 1.5),
        (_trend_score(m["net_profit_trend"], good_dir="up"), 2),
    ])

    # ---- 6) Growth 5% ----
    growth = _weighted_avg([
        (_scale(m["revenue_growth"], bad=-10.0, good=15.0), 2),
        (_scale(m["ebitda_growth"], bad=-10.0, good=15.0), 2),
        (_scale(m["earnings_growth"], bad=-20.0, good=20.0), 1),
    ])

    overall = _weighted_avg([
        (leverage, 30), (interest_cov, 25), (cashflow, 22),
        (liquidity, 10), (profitability, 8), (growth, 5),
    ])

    return {
        "risk_score": round(overall, 1) if overall is not None else None,
        "risk_leverage": round(leverage, 1) if leverage is not None else None,
        "risk_interest": round(interest_cov, 1) if interest_cov is not None else None,
        "risk_cashflow": round(cashflow, 1) if cashflow is not None else None,
        "risk_liquidity": round(liquidity, 1) if liquidity is not None else None,
        "risk_profitability": round(profitability, 1) if profitability is not None else None,
        "risk_growth": round(growth, 1) if growth is not None else None,
        "ib_de_ratio": ib_de_ratio,
        "ib_debt_to_asset": ib_debt_to_asset,
    }


def fetch_one(ticker_raw: str):
    """
    ดึงและคำนวณข้อมูลของหุ้น 1 ตัว -> คืนค่า list ของ dict (พร้อมส่งเข้า Supabase)
    เก็บ "ทุกงวดย้อนหลัง" ที่ Yahoo Finance มีให้ (ปกติรายปีย้อนได้ ~4 ปี, รายไตรมาสย้อนได้ ~4-5 ไตรมาส)
    ไม่ใช่แค่งวดล่าสุด เพื่อให้กราฟแนวโน้มคะแนนย้อนหลังใช้งานได้ทันทีโดยไม่ต้องรอสะสมข้อมูลข้ามปี
    """
    ticker = normalize_ticker(ticker_raw)
    tk = yf.Ticker(ticker)

    company_name = None
    industry = None
    try:
        info = tk.info
        company_name = info.get("longName") or info.get("shortName")
        industry = info.get("industry") or info.get("sector")
    except Exception:
        pass

    rows = []

    # ---- รายปี (annual) - เก็บทุกปีที่คำนวณ trend/growth ได้ (ต้องมีปีก่อนหน้าเทียบอย่างน้อย 1 ปี) ----
    income_a, balance_a, cash_a = tk.financials, tk.balance_sheet, tk.cashflow
    if income_a is not None and not income_a.empty:
        cols = list(income_a.columns)  # เรียงจากล่าสุด -> เก่าสุด
        for i in range(len(cols)):
            col = cols[i]
            prev_col = cols[i + 1] if i + 1 < len(cols) else None
            metrics = _calc_period_metrics(income_a, balance_a, cash_a, col, prev_col)
            period_end = col.date().isoformat() if hasattr(col, "date") else str(col)
            rows.append({
                "ticker": ticker,
                "company_name": company_name,
                "industry": industry,
                "period_type": "annual",
                "period_end": period_end,
                "fiscal_label": f"FY{col.year}" if hasattr(col, "year") else "FY",
                **metrics,
            })

    # ---- รายไตรมาส - เก็บทุกไตรมาสที่คำนวณได้เช่นกัน ----
    income_q, balance_q, cash_q = tk.quarterly_financials, tk.quarterly_balance_sheet, tk.quarterly_cashflow
    if income_q is not None and not income_q.empty:
        cols = list(income_q.columns)
        for i in range(len(cols)):
            col = cols[i]
            prev_col = cols[i + 1] if i + 1 < len(cols) else None
            metrics = _calc_period_metrics(income_q, balance_q, cash_q, col, prev_col)
            period_end = col.date().isoformat() if hasattr(col, "date") else str(col)
            q_label = f"Q{((col.month - 1) // 3) + 1}/{col.year}" if hasattr(col, "month") else "Q"
            rows.append({
                "ticker": ticker,
                "company_name": company_name,
                "industry": industry,
                "period_type": "quarterly",
                "period_end": period_end,
                "fiscal_label": q_label,
                **metrics,
            })

    # ---- Hybrid: ใช้ตัวเลขจากงบดุลไตรมาสล่าสุด แทนงบปีล่าสุด สำหรับ Risk Score เฉพาะ 5 ตัวชี้วัด ----
    # ที่เป็นข้อมูล "ณ จุดเวลา" (point-in-time) เพราะไตรมาสล่าสุดใหม่กว่าปีล่าสุดเสมอ:
    # IBD/E, IBD/Assets, Current Ratio, Interest-bearing Debt/Total Debt, Cash/Short-term Debt
    # (ตัวชี้วัดที่เป็น "ตัวเลขไหล" เช่น EBITDA, CFO, กำไรสุทธิ ยังคงใช้ฐานรายปีเหมือนเดิม เพราะ 1 ไตรมาสเทียบเกณฑ์รายปีตรงๆ ไม่ได้)
    annual_list = [r for r in rows if r["period_type"] == "annual"]
    quarterly_list = [r for r in rows if r["period_type"] == "quarterly"]
    if annual_list and quarterly_list:
        latest_annual = max(annual_list, key=lambda r: r["period_end"])
        latest_quarterly = max(quarterly_list, key=lambda r: r["period_end"])
        if latest_quarterly["period_end"] > latest_annual["period_end"]:
            hybrid = dict(latest_annual)
            for k in ["ib_de_ratio", "ib_debt_to_asset", "current_ratio",
                      "interest_bearing_debt", "total_liabilities",
                      "cash_and_equivalents", "short_term_debt"]:
                hybrid[k] = latest_quarterly.get(k)
            risk = _calc_risk_score(hybrid)
            latest_annual["risk_ib_de_ratio"] = latest_quarterly.get("ib_de_ratio")
            latest_annual["risk_ib_debt_to_asset"] = latest_quarterly.get("ib_debt_to_asset")
            latest_annual["risk_current_ratio"] = latest_quarterly.get("current_ratio")
            latest_annual["risk_interest_bearing_debt"] = latest_quarterly.get("interest_bearing_debt")
            latest_annual["risk_total_liabilities"] = latest_quarterly.get("total_liabilities")
            latest_annual["risk_cash_and_equivalents"] = latest_quarterly.get("cash_and_equivalents")
            latest_annual["risk_short_term_debt"] = latest_quarterly.get("short_term_debt")
            latest_annual["risk_score_quarter_label"] = latest_quarterly.get("fiscal_label")
            latest_annual["risk_score"] = risk["risk_score"]
            latest_annual["risk_leverage"] = risk["risk_leverage"]
            latest_annual["risk_liquidity"] = risk["risk_liquidity"]
            # risk_interest / risk_cashflow / risk_profitability / risk_growth ใช้ฐานรายปีเดิม ไม่แตะต้อง

    return rows


def upsert_supabase(rows):
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("ยังไม่ได้ตั้งค่า SUPABASE_URL / SUPABASE_SERVICE_KEY")

    url = f"{SUPABASE_URL}/rest/v1/financials?on_conflict=ticker,period_type,period_end"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    resp = requests.post(url, json=rows, headers=headers, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Supabase upsert failed [{resp.status_code}]: {resp.text}")


def mark_synced(ticker):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/watchlist?on_conflict=ticker"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    requests.post(url, json=[{"ticker": ticker, "last_synced": pd.Timestamp.utcnow().isoformat()}],
                   headers=headers, timeout=30)


def get_watchlist():
    """ดึงรายชื่อหุ้นทั้งหมดที่เคยถูกค้นหา/บันทึกไว้ใน Supabase"""
    url = f"{SUPABASE_URL}/rest/v1/watchlist?select=ticker"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return [r["ticker"] for r in resp.json()]


def run_one(ticker_raw):
    ticker = normalize_ticker(ticker_raw)
    print(f"[fetch] {ticker} ...")
    try:
        rows = fetch_one(ticker)
        if not rows:
            print(f"  !! ไม่พบข้อมูลงบการเงินของ {ticker} (อาจสะกดผิด หรือ Yahoo ไม่มีข้อมูล)")
            return False
        upsert_supabase(rows)
        mark_synced(ticker)
        print(f"  ok: บันทึก {len(rows)} งวดบัญชีของ {ticker} แล้ว")
        return True
    except Exception as e:
        print(f"  !! error กับ {ticker}: {e}")
        return False


def run_all(seed_list=None):
    tickers = set(seed_list or [])
    try:
        tickers |= set(get_watchlist())
    except Exception as e:
        print(f"อ่าน watchlist ไม่สำเร็จ: {e}")

    if not tickers:
        print("watchlist ว่างเปล่า ไม่มีอะไรให้อัปเดต")
        return

    print(f"จะอัปเดตทั้งหมด {len(tickers)} หุ้น")
    ok, fail = 0, 0
    for i, t in enumerate(sorted(tickers), 1):
        success = run_one(t)
        ok += 1 if success else 0
        fail += 0 if success else 1
        time.sleep(1.2)  # หน่วงเวลาเล็กน้อยเพื่อไม่ให้ยิง Yahoo ถี่เกินไป
    print(f"เสร็จสิ้น: สำเร็จ {ok} ตัว / ล้มเหลว {fail} ตัว")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", help="อัปเดตหุ้นตัวเดียว เช่น PTT.BK หรือ ptt")
    parser.add_argument("--all", action="store_true", help="อัปเดตทุกหุ้นใน watchlist")
    parser.add_argument("--seed-file", help="path ไฟล์ .txt รายชื่อหุ้นเริ่มต้น (1 ticker ต่อบรรทัด)")
    args = parser.parse_args()

    if args.ticker:
        success = run_one(args.ticker)
        sys.exit(0 if success else 1)
    elif args.all or args.seed_file:
        seed = []
        if args.seed_file and os.path.exists(args.seed_file):
            with open(args.seed_file, encoding="utf-8") as f:
                seed = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        run_all(seed_list=[normalize_ticker(s) for s in seed])
    else:
        parser.print_help()
