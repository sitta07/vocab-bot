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
model = genai.GenerativeModel('gemini-pro')
# 3. Setup Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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
    
    # --- Logic: การเพิ่มคำศัพท์ (รองรับทั้งไทยและอังกฤษ) ---
    if user_msg.lower().startswith(("เพิ่ม:", "add:")):
        word = user_msg.split(":", 1)[1].strip()
        
        if not word:
            reply_text = "กรุณาพิมพ์คำศัพท์หลังเครื่องหมาย : ด้วยครับ เช่น 'เพิ่ม: แมว' หรือ 'Add: Cat'"
        else:
            try:
                # 🔥 แก้ Prompt ให้ฉลาดขึ้น (Auto-detect Language)
                prompt = (f"The user input is '{word}'. "
                          f"1. Detect language: If it's English, translate to Thai. If it's Thai, translate to English. "
                          f"2. Provide the translation as 'Meaning'. "
                          f"3. Provide one simple example sentence in English using the English version of the word. "
                          f"Format your response exactly like this:\n"
                          f"Meaning: [Translation]\n"
                          f"Example: [English Example Sentence]")
                
                response = model.generate_content(prompt)
                ai_text = response.text.strip()
                
                # Parsing logic (เหมือนเดิม)
                meaning = "No meaning found"
                example = "No example found"
                
                lines = ai_text.split('\n')
                for line in lines:
                    if line.startswith("Meaning:"):
                        meaning = line.replace("Meaning:", "").strip()
                    elif line.startswith("Example:"):
                        example = line.replace("Example:", "").strip()

                # บันทึกลง Supabase
                data = {
                    "word": word, # เก็บคำที่ User พิมพ์มา (จะเป็นไทยหรืออังกฤษก็ได้)
                    "meaning": meaning,
                    "example_sentence": example
                }
                supabase.table("vocab").insert(data).execute()

                reply_text = (f"✅ บันทึกคำว่า '{word}' เรียบร้อย!\n\n"
                              f"📍 แปลว่า: {meaning}\n"
                              f"📝 ตัวอย่าง: {example}")
                
            except Exception as e:
                print(f"Error: {e}")
                reply_text = "ขอโทษครับ ระบบมีปัญหาตอนบันทึก ลองใหม่อีกครั้งนะ"

    # --- Logic: อื่นๆ ---
    else:
        reply_text = "พิมพ์ 'เพิ่ม: [คำศัพท์]' เพื่อบันทึกคำศัพท์ใหม่นะครับ"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )