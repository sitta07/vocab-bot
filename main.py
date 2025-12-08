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

# 🔥 GEMINI CONFIG (ปลดล็อก Safety Filter กัน Error)
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

# 🔥 GLOBAL STATE (MEMORY)
# เก็บสถานะว่า User คนนี้กำลังทำโจทย์คำว่าอะไรอยู่ และผิดไปกี่ครั้งแล้ว
# Structure: { 'U12345...': {'target_word': 'Apple', 'attempts': 0} }
user_sessions = {}

# --- 2. HELPER FUNCTIONS ---
def save_user(user_id):
    """เก็บ User ID ลง DB เพื่อใช้ Broadcast"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except: pass

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Strict Teacher Bot is ready!"}

@app.get("/broadcast-quiz")
def broadcast_quiz():
    """ยิงโจทย์หาทุกคน + บันทึก State ว่าต้องตอบคำนี้เท่านั้น"""
    try:
        # 1. หา User และ Vocab
        users = supabase.table("users").select("user_id").execute().data
        if not users: return {"msg": "No users found"}

        vocab_list = supabase.table("vocab").select("*").limit(100).execute().data
        if not vocab_list: return {"msg": "No vocab found"}
            
        # 2. สุ่มคำศัพท์
        selected = random.choice(vocab_list)
        word = selected['word']
        meaning = selected.get('meaning', '-')

        # 3. เตรียมข้อความ
        msg = (f"🔥 ภารกิจประลองปัญญา!\n\n"
               f"คำศัพท์โจทย์: {word}\n"
               f"ความหมาย: {meaning}\n\n"
               f"👉 แต่งประโยคโดยใช้คำว่า '{word}' ส่งมาเดี๋ยวนี้!")

        # 4. ส่งข้อความ + ฝัง Memory
        for user in users:
            uid = user['user_id']
            
            # 🔥 CRITICAL: บันทึกโจทย์ลง Memory ของ User คนนั้น
            user_sessions[uid] = {
                'target_word': word,
                'attempts': 0
            }
            
            try:
                line_bot_api.push_message(uid, TextSendMessage(text=msg))
            except: continue 
            
        return {"status": "success", "sent_to": len(users), "target_word": word}
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
                      f"4. (รอรับโจทย์) -> ระบบจะส่งโจทย์ให้เอง")

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

    # === MENU 5: ตรวจการบ้าน (The Real Logic) ===
    else:
        # 1. เช็คว่ามีโจทย์ค้างไหม
        session = user_sessions.get(user_id, {})
        target_word = session.get('target_word', None)
        current_attempt = session.get('attempts', 0) + 1
        
        # กรณี User พิมพ์เล่นๆ โดยไม่มีโจทย์ (หรือ Restart Server ไปแล้ว)
        if not target_word:
            reply_text = "ตอนนี้ยังไม่มีโจทย์ครับ 😴\nรอรอบถัดไป หรือพิมพ์ 'คำสั่ง' เพื่อดูเมนูนะ"
        
        else:
            try:
                # 🔥 STRICT PROMPT: ต้องมีคำศัพท์โจทย์ + ห้ามประโยคง่ายเกินไป
                prompt = (f"User input: '{user_msg}'\n"
                          f"Target Vocabulary: '{target_word}'\n"
                          f"Role: Strict English Teacher.\n"
                          f"Tasks:\n"
                          f"1. Check if the user used the Target Vocabulary '{target_word}' (allow variations like -ed, -ing, -s).\n"
                          f"   - IF NOT USED: Mark Pass = No (Reason: You did not use the word '{target_word}').\n"
                          f"2. Check Grammar. IF grammar is wrong -> Pass = No.\n"
                          f"3. Check Complexity. IF grammar is correct BUT sentence is too simple (Subject+Verb only, e.g. 'I eat {target_word}') -> Pass = No (Reason: Too simple, add more details).\n"
                          f"4. IF all criteria met -> Pass = Yes.\n\n"
                          f"Output Format:\n"
                          f"Pass: [Yes/No]\n"
                          f"Reason: [Short Thai reason]\n"
                          f"Feedback: [Thai advice]\n"
                          f"Better: [English correction]")
                
                res = model.generate_content(prompt)
                
                # กัน AI ไม่ตอบ (Safety Block Handle)
                try: ai_text = res.text.strip()
                except ValueError: ai_text = "Pass: No\nReason: AI Error\nFeedback: Try again"

                # Parse Response
                is_pass = False
                reason, feedback, better_ver = "ไม่ระบุ", "-", "No suggestion"

                for line in ai_text.split('\n'):
                    if line.startswith("Pass:"): is_pass = "Yes" in line
                    elif line.startswith("Reason:"): reason = line.replace("Reason:", "").strip()
                    elif line.startswith("Feedback:"): feedback = line.replace("Feedback:", "").strip()
                    elif line.startswith("Better:"): better_ver = line.replace("Better:", "").strip()

                # --- ตัดสินใจ ---
                if is_pass:
                    # ✅ ผ่าน
                    if user_id in user_sessions: del user_sessions[user_id] # ล้างโจทย์
                    reply_text = (f"🎉 เก่งมาก! ถูกต้องสมบูรณ์\n"
                                  f"โจทย์: {target_word}\n"
                                  f"ผล: ✅ ผ่านฉลุย\n"
                                  f"💬 {reason}\n"
                                  f"✨ ตัวอย่าง: \"{better_ver}\"")
                else:
                    # ❌ ไม่ผ่าน
                    if current_attempt < 3:
                        # อัปเดตครั้งที่ผิด
                        user_sessions[user_id]['attempts'] = current_attempt
                        reply_text = (f"🤔 ยังไม่ผ่าน (ครั้งที่ {current_attempt}/3)\n"
                                      f"🎯 โจทย์บังคับ: {target_word}\n"
                                      f"❌ ปัญหา: {reason}\n"
                                      f"💡 แนะนำ: {feedback}\n"
                                      f"👉 ลองแต่งใหม่ให้ดีกว่าเดิมครับ!")
                    else:
                        # หมดเวลา
                        if user_id in user_sessions: del user_sessions[user_id]
                        reply_text = (f"❌ ครบ 3 ครั้งแล้ว (3/3)\n"
                                      f"📝 ปัญหาคือ: {reason}\n"
                                      f"🔑 เฉลยที่ถูก: \"{better_ver}\"\n"
                                      f"จำรูปแบบไว้นะครับ ครั้งหน้าเอาใหม่!")

            except Exception as e:
                print(f"System Error: {e}")
                reply_text = "😵‍💫 ระบบตรวจขัดข้อง ลองส่งใหม่อีกทีนะครับ"

    # ส่งข้อความกลับ
    if reply_text:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))