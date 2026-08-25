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

    short_debt = _row_get(balance_df, ["Current Debt", "Short Long Term Debt", "CurrentDebt"], col) or 0
    long_debt = _row_get(balance_df, ["Long Term Debt", "LongTermDebt"], col) or 0
    interest_bearing_debt = None
    if short_debt or long_debt:
        interest_bearing_debt = short_debt + long_debt
    else:
        total_debt = _row_get(balance_df, ["Total Debt", "TotalDebt"], col)
        interest_bearing_debt = total_debt

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
    if prev_col is not None:
        prev_revenue = _row_get(income_df, ["Total Revenue", "TotalRevenue"], prev_col)
        prev_net_income = _row_get(income_df, ["Net Income", "NetIncome", "Net Income Common Stockholders"], prev_col)
        if revenue is not None and prev_revenue:
            revenue_growth = (revenue - prev_revenue) / abs(prev_revenue) * 100
        if net_income is not None and prev_net_income:
            earnings_growth = (net_income - prev_net_income) / abs(prev_net_income) * 100

    return {
        "total_liabilities": _clean(total_liabilities),
        "interest_bearing_debt": _clean(interest_bearing_debt),
        "de_ratio": _clean(de_ratio),
        "net_debt_to_ebitda": _clean(net_debt_ebitda),
        "ebitda": _clean(ebitda),
        "ebit": _clean(ebit),
        "interest_expense": _clean(interest_expense),
        "interest_coverage_ratio": _clean(interest_coverage),
        "cfo": _clean(cfo),
        "fcf": _clean(fcf),
        "cash_and_equivalents": _clean(cash),
        "current_ratio": _clean(current_ratio),
        "net_income": _clean(net_income),
        "roe": _clean(roe),
        "roa": _clean(roa),
        "revenue_growth": _clean(revenue_growth),
        "earnings_growth": _clean(earnings_growth),
        "total_equity": _clean(total_equity),
        "total_assets": _clean(total_assets),
        "revenue": _clean(revenue),
    }


def fetch_one(ticker_raw: str):
    """ดึงและคำนวณข้อมูลปีล่าสุด + ไตรมาสล่าสุด ของหุ้น 1 ตัว -> คืนค่า list ของ dict (พร้อมส่งเข้า Supabase)"""
    ticker = normalize_ticker(ticker_raw)
    tk = yf.Ticker(ticker)

    company_name = None
    try:
        info = tk.info
        company_name = info.get("longName") or info.get("shortName")
    except Exception:
        pass

    rows = []

    # ---- รายปี (annual) ----
    income_a, balance_a, cash_a = tk.financials, tk.balance_sheet, tk.cashflow
    if income_a is not None and not income_a.empty:
        cols = list(income_a.columns)
        latest_col = cols[0]
        prev_col = cols[1] if len(cols) > 1 else None
        metrics = _calc_period_metrics(income_a, balance_a, cash_a, latest_col, prev_col)
        period_end = latest_col.date().isoformat() if hasattr(latest_col, "date") else str(latest_col)
        rows.append({
            "ticker": ticker,
            "company_name": company_name,
            "period_type": "annual",
            "period_end": period_end,
            "fiscal_label": f"FY{latest_col.year}" if hasattr(latest_col, "year") else "FY",
            **metrics,
        })

    # ---- รายไตรมาสล่าสุด (quarterly) ----
    income_q, balance_q, cash_q = tk.quarterly_financials, tk.quarterly_balance_sheet, tk.quarterly_cashflow
    if income_q is not None and not income_q.empty:
        cols = list(income_q.columns)
        latest_col = cols[0]
        prev_col = cols[1] if len(cols) > 1 else None
        metrics = _calc_period_metrics(income_q, balance_q, cash_q, latest_col, prev_col)
        period_end = latest_col.date().isoformat() if hasattr(latest_col, "date") else str(latest_col)
        q_label = f"Q{((latest_col.month - 1) // 3) + 1}/{latest_col.year}" if hasattr(latest_col, "month") else "Q"
        rows.append({
            "ticker": ticker,
            "company_name": company_name,
            "period_type": "quarterly",
            "period_end": period_end,
            "fiscal_label": q_label,
            **metrics,
        })

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
