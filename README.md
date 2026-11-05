# NVDA เรดาร์ข่าวและความเสี่ยง

เว็บรายงานข่าว Nvidia (NVDA) อัตโนมัติ เฉพาะแหล่งข่าวที่น่าเชื่อถือ + ระดับความเสี่ยง 5 ขั้น + แจ้งเตือนกราฟผิดปกติเข้ามือถือ (iPhone / iPad / Android)

> ⚠️ ไม่ใช่คำแนะนำการลงทุน ระดับความเสี่ยงคำนวณจากกฎทางเทคนิคและโทนข่าวแบบง่าย

## ทำงานอย่างไร
- **สดจริง (ไม่ต้องรอ):** ราคา, กราฟ และสัญญาณเทคนิค มาจาก widget ของ TradingView บนหน้าเว็บโดยตรง
- **ความเห็นนักลงทุน:** StockTwits (สัดส่วนกระทิง/หมี + กราฟแนวโน้ม), CNN Fear & Greed, คำแนะนำนักวิเคราะห์ (Finnhub)
- **GitHub Actions** รัน `scripts/update.py` ทุก 10 นาทีช่วงตลาดสหรัฐฯ เปิด (จ.–ศ.) และส่งสรุปรายวัน 19:30 น. เวลาไทย
- ดึงราคา (Yahoo Finance, สำรอง Stooq) และข่าว (Google News RSS ที่กรองเฉพาะ Reuters, Bloomberg, AP, WSJ, FT, CNBC, Barron's ฯลฯ + NVIDIA Newsroom + SEC EDGAR)
- คำนวณคะแนน → `docs/data.json` → หน้าเว็บ `docs/index.html` (GitHub Pages) ดึงข้อมูลใหม่เองทุก 2 นาที
- กราฟสดจาก TradingView เปิดดูได้ตลอด

## ติดตั้ง (ทำครั้งเดียว ~10 นาที)
1. สร้าง repo ใหม่บน GitHub (Public ถ้าใช้ GitHub Pages ฟรี) แล้ว push โฟลเดอร์นี้ขึ้นไป
2. **Settings → Pages** → Source: *Deploy from a branch* → Branch `main` / โฟลเดอร์ `/docs`
3. **Settings → Actions → General** → Workflow permissions: *Read and write*
4. ตั้งการแจ้งเตือนมือถือ (เลือกอย่างน้อย 1 แบบ) ที่ **Settings → Secrets and variables → Actions → New repository secret**

### แบบ A: แอป ntfy (แนะนำ ง่ายสุด ไม่ต้องสมัคร)
1. ติดตั้งแอป **ntfy** จาก App Store (iPhone/iPad) หรือ Google Play
2. ในแอปกด **+** → Subscribe to topic → ตั้งชื่อ topic ยาว ๆ เดายาก เช่น `nvda-bii-8f3k2q9x` (ใครรู้ชื่อนี้จะเห็นข้อความได้)
3. เพิ่ม secret `NTFY_TOPIC` = ชื่อ topic เดียวกัน
4. อนุญาต Notifications ให้แอป ntfy ใน iOS Settings

### แบบ B: Telegram
1. คุยกับ @BotFather → `/newbot` → ได้ token → secret `TELEGRAM_BOT_TOKEN`
2. ส่งข้อความหา bot ของคุณ 1 ครั้ง แล้วเปิด `https://api.telegram.org/bot<TOKEN>/getUpdates` ดู `chat.id` → secret `TELEGRAM_CHAT_ID`

### (ไม่บังคับ) คะแนนนักวิเคราะห์ Wall Street
- สมัครฟรีที่ finnhub.io → copy API key → secret `FINNHUB_API_KEY` → การ์ด "นักวิเคราะห์" จะแสดงจำนวน ซื้อ/ถือ/ขาย

### (ไม่บังคับ) สรุปข่าวเป็นภาษาไทยด้วย Claude
- secret `ANTHROPIC_API_KEY` → หัวข้อข่าวจะมีสรุปภาษาไทยและคะแนนโทนข่าวที่แม่นขึ้น (มีค่าใช้จ่าย API เล็กน้อย)

## ทดสอบว่าแจ้งเตือนเข้ามือถือจริง
**Actions → Update NVDA dashboard → Run workflow → mode: `test`** → ภายใน ~1 นาทีมือถือจะเด้ง "ทดสอบแจ้งเตือน NVDA"
จากนั้นรันอีกครั้งด้วย mode `daily` เพื่อดึงข้อมูลจริงครั้งแรกและส่งสรุป

## การแจ้งเตือนที่จะได้รับ
- ⚠️ **กราฟผิดปกติ** (ส่งครั้งเดียวต่อเหตุการณ์): ราคาเคลื่อนไหว |z| ≥ 2.5, volume ≥ 2 เท่า, gap ≥ 3%, หลุด Bollinger, Death Cross, drawdown ≥ 20%, ผันผวนสูงผิดปกติ, ระหว่างวัน ≥ 4% (ระดับรุนแรงจะเด้งแบบ priority สูงสุด)
- 📰 **สรุปประจำวัน** จ.–ศ. 19:30 น.: ราคา, ระดับความเสี่ยง, แนวโน้ม Buy/Sell, ข่าวเด่น 6 ข่าว

## ข้อจำกัดที่ควรรู้
- ข่าวและความเห็นนักลงทุนอัปเดตเป็นรอบ (ทุก ~10 นาที) ไม่ใช่วินาทีต่อวินาที และตาราง cron ของ GitHub อาจช้ากว่ากำหนด 5–15 นาทีในช่วงคนใช้เยอะ จึงไม่เหมาะกับการเทรดรายนาที
- ข้อมูลราคาจาก Yahoo เป็นแหล่งไม่เป็นทางการ ถ้าใช้ไม่ได้จะสลับไป Stooq (ข้อมูลรายวัน)
- repo ที่ไม่มีความเคลื่อนไหว 60 วัน GitHub จะหยุด schedule แต่ bot นี้ commit ข้อมูลทุกวันจึงไม่โดนหยุด
