import os
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client

app = FastAPI()

# 1. Load Config
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 2. Setup Gemini
genai.configure(api_key=GEMINI_API_KEY)

# --- 🔍 DEBUG MODE: ปริ้นท์รายชื่อโมเดลออกมาดูใน Log Render ---
print("\n--- AVAILABLE GEMINI MODELS ---")
try:
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(f"- {m.name}")
except Exception as e:
    print(f"Error listing models: {e}")
print("-------------------------------\n")
# -----------------------------------------------------------

# 🔥 แก้ชื่อโมเดลตามข้อมูลที่คุณหามา (หรือลอง 'gemini-pro' ถ้าอันนี้ไม่ได้)
model = genai.GenerativeModel('gemini-flash-latest') 

# 3. Setup Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Error: {e}")

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
    
    if user_msg.lower().startswith(("เพิ่ม:", "add:")):
        word = user_msg.split(":", 1)[1].strip()
        if not word:
            reply_text = "อย่าลืมใส่คำศัพท์หลัง : นะครับ"
        else:
            try:
                # Prompt
                prompt = (f"The user input is '{word}'. "
                          f"1. If English, translate to Thai. If Thai, translate to English. "
                          f"2. Provide Meaning and one Example sentence in English. "
                          f"Format:\nMeaning: ...\nExample: ...")
                
                response = model.generate_content(prompt)
                ai_text = response.text.strip()
                
                # Parsing
                meaning = "No meaning found"
                example = "No example found"
                for line in ai_text.split('\n'):
                    if line.startswith("Meaning:"): meaning = line.replace("Meaning:", "").strip()
                    elif line.startswith("Example:"): example = line.replace("Example:", "").strip()

                # Save to DB
                data = {"word": word, "meaning": meaning, "example_sentence": example}
                supabase.table("vocab").insert(data).execute()

                reply_text = f"✅ บันทึก '{word}' แล้ว!\n📍 {meaning}\n📝 {example}"
                
            except Exception as e:
                print(f"Error during process: {e}") # ดู Log ตรงนี้ถ้าพัง
                reply_text = "ขอโทษครับ ระบบมีปัญหา (เช็ค Log Render หน่อย)"
    else:
        reply_text = "พิมพ์ 'เพิ่ม: [ศัพท์]' เพื่อจดศัพท์นะ"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))