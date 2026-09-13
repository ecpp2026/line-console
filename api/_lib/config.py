import os

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
# LIFF / LINE Login channel id (ตัวเลขล้วน) — รับ LIFF id เต็มได้ ตัด -xxxx ออกให้
LIFF_CHANNEL_ID = os.environ.get("LIFF_CHANNEL_ID", "").strip().split("-")[0]

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

# ถ้า true: ใครล็อกอิน LINE ก็เข้าได้ (ยังไม่เช็คตาราง admins) — ใช้ตอน bootstrap เท่านั้น
OPEN_SIGNUP = os.environ.get("OPEN_SIGNUP", "").lower() in ("1", "true", "yes")

# secret สำหรับ endpoint /api/cron/* (เรียกโดย Supabase pg_cron)
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()

# LINE userId ที่จะรับแจ้งเตือน error ของระบบ (คั่นด้วย ,) — ว่าง = ไม่แจ้ง
ALERT_USER_IDS = [u.strip() for u in os.environ.get("ALERT_USER_IDS", "").split(",") if u.strip()]

# LINE Login channel (สำหรับ LIFF) — ดึงรายการ LIFF จาก LINE อัตโนมัติได้ 2 วิธี:
#  (ก) ใส่ LINE_LOGIN_CHANNEL_TOKEN โดยตรง (channel access token ของ Login channel)
#  (ข) ใส่ LINE_LOGIN_CHANNEL_SECRET (+ LINE_LOGIN_CHANNEL_ID ถ้าต่างจาก LIFF_CHANNEL_ID)
#      -> ระบบจะขอ token เองด้วย client_credentials แล้ว cache ไว้
LINE_LOGIN_CHANNEL_TOKEN = os.environ.get("LINE_LOGIN_CHANNEL_TOKEN", "").strip()
LINE_LOGIN_CHANNEL_ID = (os.environ.get("LINE_LOGIN_CHANNEL_ID", "").strip().split("-")[0]
                         or LIFF_CHANNEL_ID)
LINE_LOGIN_CHANNEL_SECRET = os.environ.get("LINE_LOGIN_CHANNEL_SECRET", "").strip()
# (แนะนำสำหรับ LINE Login channel) JWT assertion — ระบบเซ็น JWT ขอ channel token เอง
#   LINE console > channel > Basic settings > Assertion Signing Key > register public key -> ได้ kid
LINE_LOGIN_ASSERTION_KID = os.environ.get("LINE_LOGIN_ASSERTION_KID", "").strip()
LINE_LOGIN_ASSERTION_PRIVATE_KEY = os.environ.get("LINE_LOGIN_ASSERTION_PRIVATE_KEY", "").replace("\\n", "\n").strip()

# channel id ของ LIFF หน้าลงทะเบียนสมาชิก (member-facing) — ตรวจ id_token ของหน้า register.html
REG_LIFF_CHANNEL_ID = os.environ.get("REG_LIFF_CHANNEL_ID", "2004722900").strip().split("-")[0]

# EasySlip API token (ตรวจสลิปอัตโนมัติ) — ว่าง = ไม่ตรวจ OCR แค่แจ้งเตือน
EASYSLIP_TOKEN = os.environ.get("EASYSLIP_TOKEN", "").strip()

# พอ user ส่งสลิป (รูป/ไฟล์ที่ถูกจัดว่าเป็นสลิปโอนเงิน) ให้เปลี่ยน rich menu ของ user คนนั้นเป็นเมนูนี้อัตโนมัติ
# เว้นว่าง = ปิด (ไม่เปลี่ยนเมนูอัตโนมัติหลังส่งสลิป)
SLIP_SUCCESS_RICHMENU_ID = os.environ.get(
    "SLIP_SUCCESS_RICHMENU_ID", "richmenu-aa532223aad4fcd9d9878b219ad37714").strip()

# (ตัวเลือก) เปิดใช้ cron enforce-richmenu: บังคับ user ที่เมนูหลุด/ไม่ตรง ให้กลับไปเป็นเมนูนี้
# เว้นว่าง = ปิดใช้งาน endpoint นี้ (ไม่ทำอะไร)
ENFORCE_RICHMENU_ID = os.environ.get("ENFORCE_RICHMENU_ID", "").strip()
# richMenuId ที่ยกเว้นไม่ถูกบังคับ (คั่นด้วย ,) เช่นเมนูแคมเปญที่ตั้งใจให้ user อยู่ต่อ
ENFORCE_EXCLUDE_MENUS = [m.strip() for m in os.environ.get("ENFORCE_EXCLUDE_MENUS", "").split(",") if m.strip()]

# (ตัวเลือก) AI ตอบคำถามอิสระ — เฉพาะ user ที่ current rich menu ตรงชื่อนี้
# เว้นว่าง ANTHROPIC_API_KEY = ปิดฟีเจอร์นี้ทั้งหมด (ไม่กระทบ auto-reply/automation เดิม)
# GEMINI_API_KEY เก็บไว้เผื่อย้อนกลับ/เทียบผล — ตัวที่ใช้งานจริงตอนนี้คือ Anthropic (Claude) ตาม ANTHROPIC_*
# ชื่อ setting/คอลัมน์ในระบบ (gemini_enabled, gemini_temperature, gemini_context ฯลฯ) ยังใช้คำเดิม
# เพื่อไม่ต้อง migrate ฐานข้อมูล — แต่โมเดลที่ตอบจริงคือ Claude แล้ว
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
GEMINI_TRIGGER_MENU_NAME = os.environ.get("GEMINI_TRIGGER_MENU_NAME", "แล้วพบกัน").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5").strip()

# domain ของเว็บนี้ (สำหรับแสดง endpoint แนะนำ) — auto จาก VERCEL_URL
APP_URL = (os.environ.get("APP_URL")
           or ("https://" + os.environ["VERCEL_PROJECT_PRODUCTION_URL"] if os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") else "")
           or "https://line-console-pi.vercel.app")

LINE_API = "https://api.line.me"
LINE_DATA_API = "https://api-data.line.me"
