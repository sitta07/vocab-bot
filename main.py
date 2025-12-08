import os
import random
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client
from dotenv import load_dotenv

# --- 1. CONFIGURATION ---
load_dotenv()

app = FastAPI()

# Load Environment Variables
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# Check Keys
if not all([LINE_ACCESS_TOKEN, LINE_SECRET, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("⚠️ Warning: Environment variables are missing!")

# Setup Clients
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 🔥 GEMINI CONFIG (WITH SAFETY SETTINGS)
# ปลดล็อก Safety Filter เพื่อกัน AI บล็อกคำตอบตัวเอง (แก้ปัญหา ValueError)
genai.configure(api_key=GEMINI_API_KEY)
safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]
model = genai.GenerativeModel('gemini-flash-latest', safety_settings=safety_settings)

# Setup Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Error: {e}")

# 🔥 GLOBAL STATE (RAM)
# ใช้เก็บ Attempt count ชั่วคราว (Restart แล้วหาย)
# Structure: { 'UserId...': {'attempts': 0, 'last_word': 'cat'} }
user_sessions = {}

# --- 2. HELPER FUNCTIONS ---
def save_user(user_id):
    """เก็บ User ID ลง DB"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except: pass

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Teacher Bot is ready and stable!"}

@app.get("/broadcast-quiz")
def broadcast_quiz():
    """ยิงโจทย์หาทุกคน (Cron Job)"""
    try:
        users = supabase.table("users").select("user_id").execute().data
        if not users: return {"msg": "No users found"}

        vocab_list = supabase.table("vocab").select("*").limit(100).execute().data
        if not vocab_list: return {"msg": "No vocab found"}
            
        selected = random.choice(vocab_list)
        word = selected['word']
        meaning = selected.get('meaning', '-')

        msg = (f"🔥 ภารกิจประลองปัญญา!\n\n"
               f"คำศัพท์: {word}\n"
               f"ความหมาย: {meaning}\n\n"
               f"👉 แต่งประโยคโดยใช้คำนี้ส่งกลับมา!")

        for user in users:
            try:
                line_bot_api.push_message(user['user_id'], TextSendMessage(text=msg))
            except: continue 
            
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

# --- 4. MESSAGE HANDLER ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id
    
    save_user(user_id)
    reply_text = ""

    # === MENU 1: คำสั่ง ===
    if user_msg == "คำสั่ง":
        reply_text = (f"🤖 คู่มือครูพี่ Bot:\n\n"
                      f"1. เพิ่ม: [ศัพท์] -> จดศัพท์ใหม่\n"
                      f"2. ลบคำศัพท์: [ศัพท์] -> ลบออก\n"
                      f"3. คลังคำศัพท์ -> ดู 20 คำล่าสุด\n"
                      f"4. พิมพ์ประโยคภาษาอังกฤษ -> ส่งการบ้าน (มีแก้ตัว 3 ครั้ง!)")

    # === MENU 2: คลังคำศัพท์ ===
    elif user_msg == "คลังคำศัพท์":
        try:
            response = supabase.table("vocab").select("word").order("id", desc=True).limit(20).execute()
            words = response.data
            if not words:
                reply_text = "📭 คลังว่างเปล่าครับ"
            else:
                word_list = "\n".join([f"- {item['word']}" for item in words])
                reply_text = f"📚 ศัพท์ล่าสุด:\n\n{word_list}"
        except: reply_text = "⚠️ ดึงข้อมูลไม่ได้ครับ"

    # === MENU 3: ลบคำศัพท์ ===
    elif user_msg.startswith("ลบคำศัพท์:"):
        try:
            target = user_msg.split(":", 1)[1].strip()
            if target:
                supabase.table("vocab").delete().ilike("word", target).execute()
                reply_text = f"🗑️ ลบ '{target}' แล้วครับ"
            else: reply_text = "ระบุคำหลัง : ด้วยนะครับ"
        except: reply_text = "⚠️ ลบไม่ได้ครับ"

    # === MENU 4: เพิ่มคำศัพท์ ===
    elif user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
            if word:
                # Prompt แบบสั้นๆ ประหยัด Token
                prompt = (f"Word: '{word}'. Translate to Thai & English Example. "
                          f"Format:\nMeaning: ...\nExample: ...")
                res = model.generate_content(prompt)
                
                meaning, example = "-", "-"
                for line in res.text.strip().split('\n'):
                    if line.startswith("Meaning:"): meaning = line.replace("Meaning:", "").strip()
                    elif line.startswith("Example:"): example = line.replace("Example:", "").strip()

                supabase.table("vocab").insert({"word": word, "meaning": meaning, "example_sentence": example}).execute()
                reply_text = f"✅ จดแล้ว!\n🔤 {word}\n📖 {meaning}\n🗣️ {example}"
            else: reply_text = "ใส่คำศัพท์หลัง : ด้วยนะครับ"
        except: reply_text = "⚠️ AI กำลังมึน ลองใหม่ครับ"

    # === MENU 5: ตรวจการบ้าน (Robust + Strict + Retry) ===
    else:
        # 1. เช็ค Session
        session = user_sessions.get(user_id, {'attempts': 0})
        current_attempt = session.get('attempts', 0) + 1
        
        try:
            # 🔥 PROMPT: ครูเข้มงวด (Strict Grammar & Detail)
            prompt = (f"User input: '{user_msg}'\n"
                      f"Role: English Teacher. Strict on grammar AND detail.\n"
                      f"Rules:\n"
                      f"1. IF grammar is wrong -> Pass: No.\n"
                      f"2. IF grammar is correct BUT sentence is too simple (e.g. 'I eat', 'It is good') -> Pass: No (Reason: Too simple).\n"
                      f"3. IF correct AND detailed -> Pass: Yes.\n"
                      f"Output Format:\n"
                      f"Word: [Main Word]\n"
                      f"Pass: [Yes/No]\n"
                      f"Reason: [Short Thai reason]\n"
                      f"Feedback: [Thai advice]\n"
                      f"Better: [English correction]")
            
            res = model.generate_content(prompt)
            
            # 🔥 กันเหนียว: เช็คว่า AI ตอบกลับมาจริงไหม
            try:
                ai_text = res.text.strip()
            except ValueError:
                # กรณี AI บล็อก หรือ Error ให้สร้างคำตอบปลอมๆ เพื่อไม่ให้ Bot พัง
                ai_text = "Pass: No\nReason: AI ประมวลผลผิดพลาด\nFeedback: ลองส่งใหม่อีกครั้งครับ"

            # Parse Response
            detected_word, is_pass = "", False
            reason, feedback, better_ver = "ไม่ระบุ", "ลองใหม่ครับ", "No suggestion"

            for line in ai_text.split('\n'):
                if line.startswith("Word:"): detected_word = line.replace("Word:", "").strip()
                elif line.startswith("Pass:"): is_pass = "Yes" in line
                elif line.startswith("Reason:"): reason = line.replace("Reason:", "").strip()
                elif line.startswith("Feedback:"): feedback = line.replace("Feedback:", "").strip()
                elif line.startswith("Better:"): better_ver = line.replace("Better:", "").strip()

            # --- ตัดสินใจตอบกลับ ---
            if is_pass:
                # ✅ ผ่าน
                if user_id in user_sessions: del user_sessions[user_id]
                reply_text = (f"🎉 เยี่ยมมาก! ผ่านฉลุย\n"
                              f"ศัพท์: {detected_word}\n"
                              f"✅ {reason}\n"
                              f"✨ ตัวอย่าง: \"{better_ver}\"")
                # Log DB (Optional)
                try:
                    v_data = supabase.table("vocab").select("id").ilike("word", detected_word).execute().data
                    if v_data:
                        supabase.table("user_logs").insert({
                            "user_id": user_id, "vocab_id": v_data[0]['id'], "user_answer": user_msg, "is_correct": True
                        }).execute()
                except: pass

            else:
                # ❌ ไม่ผ่าน
                if current_attempt < 3:
                    # ยังไม่ครบ 3 ครั้ง
                    user_sessions[user_id] = {'attempts': current_attempt}
                    reply_text = (f"🤔 ยังไม่ผ่านครับ (ครั้งที่ {current_attempt}/3)\n"
                                  f"❌ {reason}\n"
                                  f"💡 {feedback}\n"
                                  f"👉 สู้ๆ ลองแต่งใหม่ให้ยาวขึ้นอีกนิด!")
                else:
                    # ครบ 3 ครั้ง -> เฉลย
                    if user_id in user_sessions: del user_sessions[user_id]
                    reply_text = (f"❌ ครบ 3 ครั้งแล้ว (3/3)\n"
                                  f"📝 ปัญหาคือ: {reason}\n"
                                  f"🔑 เฉลยที่ถูก: \"{better_ver}\"\n"
                                  f"จำรูปแบบไว้นะครับ ครั้งหน้าเอาใหม่!")

        except Exception as e:
            print(f"❌ System Error: {e}")
            reply_text = "😵‍💫 ระบบขัดข้องชั่วคราว ลองส่งใหม่อีกทีนะครับ"

    if reply_text:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))