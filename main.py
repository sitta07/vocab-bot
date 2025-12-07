import os
import random
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client
from dotenv import load_dotenv

# โหลด Config สำหรับ Local Run
load_dotenv()

app = FastAPI()

# --- 1. CONFIGURATION ---
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# ตรวจสอบ Key (กันพลาด)
if not all([LINE_ACCESS_TOKEN, LINE_SECRET, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("⚠️ Warning: Environment variables are missing!")

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# Setup Gemini (ใช้ Model Flash ตัวใหม่ล่าสุด)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

# Setup Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Error: {e}")

# --- 2. HELPER FUNCTIONS ---
def save_user(user_id):
    """บันทึก User ID ลง DB เพื่อใช้ส่ง Quiz"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except Exception as e:
        print(f"Save user error: {e}")

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Bot is alive and ready to teach!"}

@app.get("/broadcast-quiz")
def broadcast_quiz():
    """ฟังก์ชันสำหรับ Cron Job ยิงเพื่อส่งโจทย์"""
    try:
        # 1. หา User ทั้งหมด
        users = supabase.table("users").select("user_id").execute().data
        if not users: return {"msg": "No users found"}

        # 2. สุ่มคำศัพท์จาก DB
        vocab_list = supabase.table("vocab").select("*").limit(100).execute().data
        if not vocab_list: return {"msg": "No vocab found"}
            
        selected = random.choice(vocab_list)
        word = selected['word']
        meaning = selected.get('meaning', '-')

        # 3. ส่งข้อความ (โจทย์) หาทุกคน
        msg = (f"🔥 ภารกิจประลองปัญญา!\n\n"
               f"คำศัพท์: {word}\n"
               f"ความหมาย: {meaning}\n\n"
               f"👉 จงแต่งประโยคภาษาอังกฤษโดยใช้คำนี้ส่งกลับมา!")

        for user in users:
            try:
                line_bot_api.push_message(user['user_id'], TextSendMessage(text=msg))
            except:
                continue # ถ้าส่งไม่ผ่าน (Block) ให้ข้ามไป
            
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

# --- 4. MESSAGE HANDLER (LOGIC หลัก) ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id
    
    # เก็บ User ID ทุกครั้งที่คุยกัน
    save_user(user_id)
    
    # === MENU 1: คู่มือคำสั่ง ===
    if user_msg == "คำสั่ง":
        reply_text = (f"🤖 คู่มือการใช้งาน:\n\n"
                      f"1. เพิ่ม: [ศัพท์] -> จดศัพท์ใหม่\n"
                      f"2. ลบคำศัพท์: [ศัพท์] -> ลบออก\n"
                      f"3. คลังคำศัพท์ -> ดูรายการศัพท์ล่าสุด\n"
                      f"4. พิมพ์ประโยคภาษาอังกฤษ -> ส่งการบ้าน (AI ตรวจให้)")

    # === MENU 2: ดูคลังคำศัพท์ ===
    elif user_msg == "คลังคำศัพท์":
        try:
            # ดึง 20 คำล่าสุด
            response = supabase.table("vocab").select("word").order("id", desc=True).limit(20).execute()
            words = response.data
            
            if not words:
                reply_text = "📭 คลังว่างเปล่า ลองพิมพ์ 'เพิ่ม: [ศัพท์]' ดูสิ!"
            else:
                word_list = "\n".join([f"- {item['word']}" for item in words])
                reply_text = f"📚 ศัพท์ล่าสุด ({len(words)} คำ):\n\n{word_list}"
        except:
            reply_text = "ดึงข้อมูลพลาด ลองใหม่นะครับ"

    # === MENU 3: ลบคำศัพท์ ===
    elif user_msg.startswith("ลบคำศัพท์:"):
        try:
            word_to_delete = user_msg.split(":", 1)[1].strip()
            if not word_to_delete:
                reply_text = "ระบุคำที่จะลบหลัง : ด้วยนะครับ"
            else:
                supabase.table("vocab").delete().ilike("word", word_to_delete).execute()
                reply_text = f"🗑️ ลบคำว่า '{word_to_delete}' ออกจากคลังแล้วครับ"
        except Exception as e:
            print(e)
            reply_text = "ระบบลบขัดข้องครับ"

    # === MENU 4: เพิ่มคำศัพท์ (Add Vocab) ===
    elif user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
        except:
            word = ""
            
        if not word:
            reply_text = "อย่าลืมใส่ศัพท์หลัง : นะครับ เช่น 'เพิ่ม: Cat'"
        else:
            try:
                # Prompt: แปลและยกตัวอย่าง
                prompt = (f"Word: '{word}'. "
                          f"1. If English, translate to Thai (short meaning). "
                          f"2. If Thai, translate to English. "
                          f"3. Example sentence (simple English). "
                          f"Format:\nMeaning: ...\nExample: ...")
                
                res = model.generate_content(prompt)
                text = res.text.strip()
                
                meaning, example = "-", "-"
                for line in text.split('\n'):
                    if line.startswith("Meaning:"): meaning = line.replace("Meaning:", "").strip()
                    elif line.startswith("Example:"): example = line.replace("Example:", "").strip()

                # Save DB
                data = {"word": word, "meaning": meaning, "example_sentence": example}
                supabase.table("vocab").insert(data).execute()

                reply_text = f"✅ จดแล้ว!\n🔤 {word}\n📖 {meaning}\n🗣️ {example}"
            except Exception as e:
                print(e)
                reply_text = "ระบบรวนนิดหน่อย ลองใหม่อีกทีนะครับ"

    # === MENU 5: โหมดตรวจการบ้าน (Grading Mode) ===
    else:
        reply_text = "ขอตรวจแป๊บ... 🧐"
        try:
            # 🔥 Prompt: ครูใจดี (Ignore punctuation errors)
            prompt = (f"User sentence: '{user_msg}'\n"
                      f"Task: \n"
                      f"1. Identify the main English vocabulary word used.\n"
                      f"2. Check if the word is used correctly in context.\n"
                      f"3. **IGNORE** minor punctuation errors (like missing periods, commas) or capitalization.\n"
                      f"4. If the sentence is understandable and uses the word correctly, mark Correct as 'Yes'.\n"
                      f"Format:\n"
                      f"Word: [The main word]\n"
                      f"Correct: [Yes/No]\n"
                      f"Feedback: [Short feedback in Thai. Be encouraging.]")
            
            res = model.generate_content(prompt)
            ai_text = res.text.strip()
            
            detected_word, is_correct, feedback = "", False, ""
            for line in ai_text.split('\n'):
                if line.startswith("Word:"): detected_word = line.replace("Word:", "").strip()
                elif line.startswith("Correct:"): is_correct = "Yes" in line
                elif line.startswith("Feedback:"): feedback = line.replace("Feedback:", "").strip()

            # MLOps Log: บันทึกผลการเรียนลง DB
            vocab_data = supabase.table("vocab").select("id").ilike("word", detected_word).execute().data
            vocab_id = vocab_data[0]['id'] if vocab_data else None
            
            supabase.table("user_logs").insert({
                "user_id": user_id,
                "vocab_id": vocab_id,
                "user_answer": user_msg,
                "is_correct": is_correct
            }).execute()

            # ตอบกลับผลสอบ
            icon = "🎉 แจ๋วเลย!" if is_correct else "🤏 นิดนึงนะ..."
            reply_text = f"{icon}\nศัพท์: {detected_word}\nผล: {'✅ ผ่าน' if is_correct else '❌ แก้ไข'}\n\n💬 {feedback}"
            
        except Exception as e:
            print(f"Grading Error: {e}")
            reply_text = "ครู AI มึนหัวนิดหน่อย ส่งใหม่นะครับ"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))