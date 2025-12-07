import os
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client
from dotenv import load_dotenv

# โหลดตัวแปรสภาพแวดล้อม (สำหรับ Local Run)
load_dotenv()

app = FastAPI()

# 1. Load Config (ดึงค่าจาก Render Environment)
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# ตรวจสอบว่า Key มาครบไหม (กันพลาด)
if not all([LINE_ACCESS_TOKEN, LINE_SECRET, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("⚠️ Warning: Environment variables are missing!")

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 2. Setup Gemini
genai.configure(api_key=GEMINI_API_KEY)
# ใช้ Model ตัวล่าสุดที่แก้บั๊กแล้ว
model = genai.GenerativeModel('gemini-flash-latest')

# 3. Setup Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Error: {e}")

@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Bot is alive!"}

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    # --- Logic: การเพิ่มคำศัพท์ ---
    if user_msg.lower().startswith(("เพิ่ม:", "add:")):
        # ตัดคำว่า "เพิ่ม:" ออก เอาแค่ศัพท์
        try:
            word = user_msg.split(":", 1)[1].strip()
        except IndexError:
            word = ""

        if not word:
            reply_text = "อย่าลืมพิมพ์คำศัพท์หลังเครื่องหมาย : นะครับ\nเช่น 'เพิ่ม: Resilience'"
        else:
            try:
                # 🔥 Prompt สั่งงาน Gemini (เน้นภาษาไทย + กระชับ)
                prompt = (f"คำศัพท์คือ '{word}' "
                          f"1. ถ้าเป็นภาษาอังกฤษ ให้แปลเป็นไทย (เอาความหมายหลัก สั้นๆ กระชับ) "
                          f"2. ถ้าเป็นภาษาไทย ให้แปลเป็นอังกฤษ "
                          f"3. ขอตัวอย่างประโยคภาษาอังกฤษ 1 ประโยค (เอาแบบสั้นๆ ง่ายๆ เข้าใจง่าย) "
                          f"4. ตอบกลับโดยใช้ Format นี้เท่านั้น:\n"
                          f"Meaning: [คำแปล]\n"
                          f"Example: [ประโยคตัวอย่าง]")
                
                response = model.generate_content(prompt)
                ai_text = response.text.strip()
                
                # Parsing (แยกข้อมูล)
                meaning = "-"
                example = "-"
                
                for line in ai_text.split('\n'):
                    if line.startswith("Meaning:"): 
                        meaning = line.replace("Meaning:", "").strip()
                    elif line.startswith("Example:"): 
                        example = line.replace("Example:", "").strip()

                # Save ลง Database
                data = {
                    "word": word,
                    "meaning": meaning,
                    "example_sentence": example
                }
                supabase.table("vocab").insert(data).execute()

                # ✨ จัดข้อความตอบกลับ (เว้นบรรทัดสวยงาม)
                reply_text = (f"✅ จดเรียบร้อย!\n"
                              f"🔤 ศัพท์: {word}\n\n"
                              f"📖 แปล: {meaning}\n"
                              f"🗣️ ตย: {example}")
                
            except Exception as e:
                print(f"Error process: {e}")
                reply_text = "ระบบรวนนิดหน่อย ลองใหม่อีกทีนะครับ"

    # --- Logic: คุยเล่นทั่วไป ---
    else:
        reply_text = "พิมพ์ 'เพิ่ม: [คำศัพท์]' เพื่อเริ่มจดศัพท์นะครับ"

    # ส่งข้อความกลับหา User
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )