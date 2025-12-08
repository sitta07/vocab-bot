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
# Structure: { 'UserId...': {'attempts': 0, 'current_word': 'cat'} }
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
        # 🔥 STEP 1: กรองข้อความที่ไม่ใช่ประโยคภาษาอังกฤษ
        if len(user_msg) < 5 or not any(c.isalpha() for c in user_msg):
            reply_text = "🤔 ส่งประโยคภาษาอังกฤษมาให้ครูตรวจนะครับ\n(หรือพิมพ์ 'คำสั่ง' ดูเมนู)"
        else:
            try:
                # 🔥 STEP 2: ตรวจสอบว่าเป็นภาษาอังกฤษก่อน
                check_prompt = f"Is this English sentence? Answer ONLY 'Yes' or 'No': '{user_msg}'"
                check_res = model.generate_content(check_prompt)
                is_english = "yes" in check_res.text.lower()
                
                if not is_english:
                    reply_text = "📝 นี่ดูไม่ค่อยเป็นประโยคภาษาอังกฤษนะครับ\nลองส่งใหม่ หรือพิมพ์ 'คำสั่ง' ดูวิธีใช้งาน"
                else:
                    # 🔥 STEP 3: ดึง Session + เช็คว่ากำลังทำโจทย์คำเดียวกันไหม
                    session = user_sessions.get(user_id, {'attempts': 0, 'current_word': None})
                    
                    # หา Main Word จากประโยค
                    word_detect_prompt = f"Extract the main vocabulary word from: '{user_msg}'. Answer with ONE word only."
                    word_res = model.generate_content(word_detect_prompt)
                    detected_word = word_res.text.strip().split()[0].lower()
                    
                    # ถ้า session ว่าง หรือเปลี่ยนคำ -> reset
                    if session['current_word'] is None or session['current_word'].lower() != detected_word:
                        session = {'attempts': 0, 'current_word': detected_word}
                    
                    current_attempt = session['attempts'] + 1
                    
                    # 🔥 STEP 4: ตรวจประโยค (PROMPT ชัดเจนขึ้น)
                    prompt = (f"Sentence: '{user_msg}'\n"
                              f"Task: Grade as strict English teacher.\n\n"
                              f"RULES:\n"
                              f"1. Grammar wrong? → Pass: No\n"
                              f"2. Too short/simple? (under 6 words OR lacks detail) → Pass: No\n"
                              f"3. Correct + detailed (7+ words, clear meaning)? → Pass: Yes\n\n"
                              f"OUTPUT (strict format):\n"
                              f"Word: [main word]\n"
                              f"Pass: [Yes or No]\n"
                              f"Reason: [Thai explanation in 1 line]\n"
                              f"Feedback: [Thai suggestion]\n"
                              f"Better: [Corrected English sentence]")
                    
                    res = model.generate_content(prompt)
                    
                    # กัน ValueError
                    try:
                        ai_text = res.text.strip()
                    except ValueError:
                        ai_text = "Pass: No\nReason: AI ประมวลผลไม่ได้\nFeedback: ลองใหม่ครับ\nBetter: -"

                    # Parse AI Response
                    is_pass = False
                    reason = "ไม่ระบุสาเหตุ"
                    feedback = "ลองปรับปรุงดูนะครับ"
                    better_ver = "No suggestion"

                    for line in ai_text.split('\n'):
                        line = line.strip()
                        if line.startswith("Pass:"): 
                            is_pass = "yes" in line.lower()
                        elif line.startswith("Reason:"): 
                            reason = line.replace("Reason:", "").strip()
                        elif line.startswith("Feedback:"): 
                            feedback = line.replace("Feedback:", "").strip()
                        elif line.startswith("Better:"): 
                            better_ver = line.replace("Better:", "").strip()

                    # 🔥 STEP 5: ตัดสินผล
                    if is_pass:
                        # ✅ ผ่าน -> ล้าง session
                        if user_id in user_sessions: 
                            del user_sessions[user_id]
                        
                        reply_text = (f"🎉 ยอดเยี่ยม! ผ่านเลย\n"
                                      f"📌 ศัพท์: {detected_word}\n"
                                      f"✅ {reason}\n\n"
                                      f"💬 ตัวอย่างที่ดี:\n\"{better_ver}\"")
                        
                        # บันทึก Log (Optional)
                        try:
                            v_data = supabase.table("vocab").select("id").ilike("word", detected_word).limit(1).execute().data
                            if v_data:
                                supabase.table("user_logs").insert({
                                    "user_id": user_id, 
                                    "vocab_id": v_data[0]['id'], 
                                    "user_answer": user_msg, 
                                    "is_correct": True
                                }).execute()
                        except: 
                            pass

                    else:
                        # ❌ ไม่ผ่าน
                        if current_attempt < 3:
                            # ยังแก้ได้
                            session['attempts'] = current_attempt
                            user_sessions[user_id] = session
                            
                            reply_text = (f"🤔 ยังไม่ผ่านนะครับ (พยายามครั้งที่ {current_attempt}/3)\n\n"
                                          f"❌ ปัญหา: {reason}\n"
                                          f"💡 คำแนะนำ: {feedback}\n\n"
                                          f"👉 ลองแก้ไขแล้วส่งใหม่ สู้ๆ!")
                        else:
                            # ครบ 3 ครั้ง -> เฉลย + ล้าง session
                            if user_id in user_sessions: 
                                del user_sessions[user_id]
                            
                            reply_text = (f"❌ ครบ 3 ครั้งแล้วครับ\n\n"
                                          f"📝 ปัญหาหลัก: {reason}\n"
                                          f"🔑 ตัวอย่างที่ถูกต้อง:\n\"{better_ver}\"\n\n"
                                          f"💪 จำไว้นะครับ ครั้งหน้าต้องทำได้แน่!")

            except Exception as e:
                print(f"❌ System Error: {e}")
                reply_text = "😵‍💫 ระบบขัดข้องชั่วคราว รอสักครู่แล้วลองใหม่นะครับ"

    if reply_text:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))