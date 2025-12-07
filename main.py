import os
import random
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Config
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Db Error: {e}")

# --- 🆕 ฟังก์ชันช่วย: บันทึก User ---
def save_user(user_id):
    try:
        # พยายาม insert, ถ้ามีแล้ว (duplicate) ก็ช่างมัน (ignore)
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except Exception as e:
        print(f"Save user error: {e}")

@app.get("/")
def health_check():
    return {"status": "ok"}

# --- 🆕 API Endpoint สำหรับ Cron Job (นาฬิกาปลุก) ---
# ใครยิง Link นี้ บอทจะทำงานทันที
@app.get("/broadcast-quiz")
def broadcast_quiz():
    try:
        # 1. หา User ทั้งหมด
        users_response = supabase.table("users").select("user_id").execute()
        users = users_response.data
        
        if not users:
            return {"msg": "No users found"}

        # 2. สุ่มคำศัพท์ 1 คำจาก DB
        # (เทคนิค: ดึงมา 100 คำล่าสุดแล้วสุ่มใน Python เพื่อความง่าย)
        vocab_response = supabase.table("vocab").select("*").limit(100).execute()
        vocab_list = vocab_response.data
        
        if not vocab_list:
            return {"msg": "No vocab found"}
            
        selected_word = random.choice(vocab_list)
        word = selected_word['word']
        meaning = selected_word.get('meaning', '-')
        example = selected_word.get('example_sentence', '-')

        # 3. ส่งข้อความหาทุกคน
        msg = (f"⏰ ทบทวนคำศัพท์รอบเช้า!\n\n"
               f"❓ คำว่า: {word}\n"
               f"📖 แปล: {meaning}\n"
               f"🗣️ ตย: {example}")

        for user in users:
            line_bot_api.push_message(
                user['user_id'],
                TextSendMessage(text=msg)
            )
            
        return {"status": "success", "sent_to": len(users), "word": word}
        
    except Exception as e:
        return {"status": "error", "detail": str(e)}

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
    user_id = event.source.user_id # ดึง ID คนส่ง
    
    # 🆕 บันทึก User ID ทุกครั้งที่คุยกัน
    save_user(user_id)
    
    if user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
        except:
            word = ""

        if not word:
            reply_text = "อย่าลืมพิมพ์คำศัพท์หลัง : นะครับ"
        else:
            try:
                # Prompt (ย่อให้สั้นลงนิดนึงเพื่อประหยัด Token)
                prompt = (f"Word: '{word}'. "
                          f"1. If English, translate to Thai. If Thai, translate to English. "
                          f"2. Meaning & Example sentence. "
                          f"Format:\nMeaning: ...\nExample: ...")
                
                response = model.generate_content(prompt)
                ai_text = response.text.strip()
                
                meaning = "-"
                example = "-"
                for line in ai_text.split('\n'):
                    if line.startswith("Meaning:"): meaning = line.replace("Meaning:", "").strip()
                    elif line.startswith("Example:"): example = line.replace("Example:", "").strip()

                data = {"word": word, "meaning": meaning, "example_sentence": example}
                supabase.table("vocab").insert(data).execute()

                reply_text = (f"✅ จดเรียบร้อย!\n"
                              f"🔤 ศัพท์: {word}\n\n"
                              f"📖 แปล: {meaning}\n"
                              f"🗣️ ตย: {example}")
                
            except Exception as e:
                print(f"Error: {e}")
                reply_text = "ระบบรวนนิดหน่อย ลองใหม่นะ"
    else:
        reply_text = "พิมพ์ 'เพิ่ม: [ศัพท์]' เพื่อจดศัพท์ หรือรอรับ Quiz ตอนเช้านะครับ!"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))