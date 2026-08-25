-- ============================================================
-- Thai Stock Financials — Supabase schema
-- วิธีใช้: เปิด Supabase Dashboard > SQL Editor > New query
-- วางโค้ดทั้งหมดนี้ แล้วกด RUN (ครั้งเดียวพอ)
-- ============================================================

create table if not exists public.financials (
  id                        bigint generated always as identity primary key,
  ticker                    text not null,                 -- เช่น PTT.BK
  company_name              text,
  period_type               text not null check (period_type in ('annual','quarterly')),
  period_end                date,
  fiscal_label              text,                           -- เช่น "FY2025" หรือ "Q2/2026"

  -- 1-16 ตัวชี้วัดทางการเงิน
  total_liabilities         numeric,   -- 1. หนี้สินรวม
  interest_bearing_debt     numeric,   -- 2. หนี้สินที่มีภาระดอกเบี้ย
  de_ratio                  numeric,   -- 3. D/E
  net_debt_to_ebitda        numeric,   -- 4. Net Debt / EBITDA
  ebitda                    numeric,   -- 5. EBITDA
  ebit                      numeric,   -- 6. EBIT
  interest_expense          numeric,   -- 7. ดอกเบี้ยจ่าย
  interest_coverage_ratio   numeric,   -- 8. Interest Coverage Ratio
  cfo                       numeric,   -- 9. กระแสเงินสดจากการดำเนินงาน
  fcf                       numeric,   -- 10. Free Cash Flow
  cash_and_equivalents      numeric,   -- 11. เงินสดและรายการเทียบเท่าเงินสด
  current_ratio             numeric,   -- 12. Current Ratio
  net_income                numeric,   -- 13. กำไรสุทธิ
  roe                       numeric,   -- 14a. ROE (%)
  roa                       numeric,   -- 14b. ROA (%)
  revenue_growth            numeric,   -- 15. อัตราการเติบโตของรายได้ (%)
  earnings_growth           numeric,   -- 16. อัตราการเติบโตของกำไร (%)

  total_equity              numeric,   -- ใช้ประกอบการคำนวณ/แสดงผล
  total_assets              numeric,
  revenue                   numeric,

  updated_at                timestamptz not null default now(),

  unique (ticker, period_type, period_end)
);

create index if not exists idx_financials_ticker on public.financials (ticker);

-- ตารางบันทึกว่ามีใครเคยค้นหาหุ้นตัวไหนบ้าง (ใช้เป็นลิสต์สำหรับอัปเดตรายวัน)
create table if not exists public.watchlist (
  ticker      text primary key,
  requested_at timestamptz not null default now(),
  last_synced  timestamptz
);

-- เปิด Row Level Security แล้วอนุญาตให้ "อ่าน" ได้แบบสาธารณะ (จำเป็น เพราะหน้าเว็บใช้ anon key)
alter table public.financials enable row level security;
alter table public.watchlist enable row level security;

drop policy if exists "public read financials" on public.financials;
create policy "public read financials" on public.financials
  for select using (true);

drop policy if exists "public read watchlist" on public.watchlist;
create policy "public read watchlist" on public.watchlist
  for select using (true);

drop policy if exists "public insert watchlist" on public.watchlist;
create policy "public insert watchlist" on public.watchlist
  for insert with check (true);

-- หมายเหตุ: การ "เขียน/แก้ไข" ตาราง financials จะทำผ่าน service_role key เท่านั้น
-- (รันจาก GitHub Actions / Colab ซึ่งเก็บ key เป็นความลับ ไม่ใช่จากหน้าเว็บ)
