// trigger-update
// -----------------------------------------------------------------------
// Edge Function นี้ถูกเรียกจากหน้าเว็บเมื่อผู้ใช้ค้นหาหุ้นที่ยังไม่มีข้อมูล
// หน้าที่: (1) บันทึกชื่อหุ้นลงตาราง watchlist  (2) สั่ง GitHub Actions
// ให้รัน workflow "update.yml" แบบระบุ ticker ทันที (workflow_dispatch)
// -----------------------------------------------------------------------
// ต้องตั้งค่า secret ต่อไปนี้ก่อน deploy (ดูขั้นตอนในคู่มือ):
//   supabase secrets set GITHUB_TOKEN=xxxxx
//   supabase secrets set GITHUB_REPO=your-username/your-repo-name
//   supabase secrets set SUPABASE_SERVICE_KEY=xxxxx   (ใช้เขียน watchlist)
// -----------------------------------------------------------------------

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function normalizeTicker(raw: string): string {
  let t = (raw || "").trim().toUpperCase();
  if (t && !t.includes(".")) t = `${t}.BK`;
  return t;
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: CORS_HEADERS });
  }

  try {
    const { ticker: rawTicker } = await req.json();
    const ticker = normalizeTicker(rawTicker);

    if (!ticker || ticker.length < 4) {
      return new Response(JSON.stringify({ error: "ticker ไม่ถูกต้อง" }), {
        status: 400,
        headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
      });
    }

    const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
    const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_KEY")!;
    const GITHUB_TOKEN = Deno.env.get("GITHUB_TOKEN")!;
    const GITHUB_REPO = Deno.env.get("GITHUB_REPO")!; // เช่น "somchai/thai-stock-financials"

    const supabase = createClient(SUPABASE_URL, SERVICE_KEY);

    // 1) บันทึกลง watchlist (เผื่อ GitHub Actions ครั้งนี้ยิงไม่สำเร็จ พรุ่งนี้ก็จะยังถูกอัปเดตอัตโนมัติ)
    await supabase.from("watchlist").upsert({ ticker, requested_at: new Date().toISOString() });

    // 2) สั่ง GitHub Actions ให้รันทันที (workflow_dispatch)
    const ghResp = await fetch(
      `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/update.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          ref: "main",
          inputs: { ticker },
        }),
      }
    );

    if (!ghResp.ok) {
      const text = await ghResp.text();
      return new Response(
        JSON.stringify({ error: "สั่ง GitHub Actions ไม่สำเร็จ", detail: text }),
        { status: 502, headers: { ...CORS_HEADERS, "Content-Type": "application/json" } }
      );
    }

    return new Response(
      JSON.stringify({ status: "triggered", ticker }),
      { status: 200, headers: { ...CORS_HEADERS, "Content-Type": "application/json" } }
    );
  } catch (err) {
    return new Response(JSON.stringify({ error: String(err) }), {
      status: 500,
      headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
    });
  }
});
