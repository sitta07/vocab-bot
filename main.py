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
# โหลดตัวแปรจากไฟล์ .env
load_dotenv()

app = FastAPI()

# ดึงค่าจาก Environment Variables
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# ตรวจสอบ Key (กันพลาด)
if not all([LINE_ACCESS_TOKEN, LINE_SECRET, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("⚠️ Warning: Environment variables are missing!")

# Setup Clients
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

genai.configure(api_key=GEMINI_API_KEY)
# ใช้ Model Flash เพื่อความไว (หรือจะใช้ pro ก็ได้ถ้าเน้นฉลาดจัดๆ)
model = genai.GenerativeModel('gemini-flash-latest')

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Error: {e}")

# 🔥 GLOBAL STATE: เก็บสถานะการตอบผิด (RAM ชั่วคราว)
# Structure: { 'UserId_...': {'attempts': 0, 'last_word': 'cat'} }
user_sessions = {}

# --- 2. HELPER FUNCTIONS ---
def save_user(user_id):
    """บันทึก User ID ลง DB"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except Exception as e:
        print(f"Save user error: {e}")

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Teacher Bot is ready! 🎓"}

@app.get("/broadcast-quiz")
def broadcast_quiz():
    """ฟังก์ชันสำหรับ Cron Job ยิงเพื่อส่งโจทย์"""
    try:
        # 1. หา User ทั้งหมด
        users = supabase.table("users").select("user_id").execute().data
        if not users: return {"msg": "No users found"}

        # 2. สุ่มคำศัพท์
        vocab_list = supabase.table("vocab").select("*").limit(100).execute().data
        if not vocab_list: return {"msg": "No vocab found"}
            
        selected = random.choice(vocab_list)
        word = selected['word']
        meaning = selected.get('meaning', '-')

        # 3. ส่งโจทย์
        msg = (f"🔥 ภารกิจประลองปัญญา!\n\n"
               f"คำศัพท์: {word}\n"
               f"ความหมาย: {meaning}\n\n"
               f"👉 จงแต่งประโยคภาษาอังกฤษโดยใช้คำนี้ส่งกลับมา!")

        for user in users:
            try:
                line_bot_api.push_message(user['user_id'], TextSendMessage(text=msg))
            except:
                continue 
            
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

# --- 4. MESSAGE HANDLER (CORE LOGIC) ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id
    
    save_user(user_id) # เก็บ User ไว้ Broadcast ทีหลัง
    reply_text = ""

    # === MENU 1: คู่มือคำสั่ง ===
    if user_msg == "คำสั่ง":
        reply_text = (f"🤖 คู่มือครูพี่ Bot:\n\n"
                      f"1. เพิ่ม: [ศัพท์] -> จดศัพท์ใหม่เข้าคลัง\n"
                      f"2. ลบคำศัพท์: [ศัพท์] -> ลบออก\n"
                      f"3. คลังคำศัพท์ -> ดูรายการศัพท์ล่าสุด\n"
                      f"4. พิมพ์ประโยคภาษาอังกฤษ -> ส่งการบ้าน (ครูตรวจละเอียดนะ ห้ามลักไก่!)")

    # === MENU 2: ดูคลังคำศัพท์ ===
    elif user_msg == "คลังคำศัพท์":
        try:
            response = supabase.table("vocab").select("word").order("id", desc=True).limit(20).execute()
            words = response.data
            if not words:
                reply_text = "📭 คลังว่างเปล่า พิมพ์ 'เพิ่ม: [ศัพท์]' เพื่อเริ่มสะสม!"
            else:
                word_list = "\n".join([f"- {item['word']}" for item in words])
                reply_text = f"📚 ศัพท์ล่าสุด ({len(words)} คำ):\n\n{word_list}"
        except:
            reply_text = "⚠️ ดึงข้อมูลพลาด ลองใหม่นะครับ"

    # === MENU 3: ลบคำศัพท์ ===
    elif user_msg.startswith("ลบคำศัพท์:"):
        try:
            word_to_delete = user_msg.split(":", 1)[1].strip()
            if not word_to_delete:
                reply_text = "ระบุคำที่จะลบหลัง : ด้วยนะครับ"
            else:
                supabase.table("vocab").delete().ilike("word", word_to_delete).execute()
                reply_text = f"🗑️ ลบคำว่า '{word_to_delete}' เรียบร้อยครับ"
        except:
            reply_text = "⚠️ ระบบลบขัดข้องครับ"

    # === MENU 4: เพิ่มคำศัพท์ (Add Vocab) ===
    elif user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
        except:
            word = ""
            
        if not word:
            reply_text = "อย่าลืมใส่ศัพท์หลัง : นะครับ เช่น 'เพิ่ม: Resilience'"
        else:
            try:
                # Prompt สำหรับแปลและหาตัวอย่าง
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

                data = {"word": word, "meaning": meaning, "example_sentence": example}
                supabase.table("vocab").insert(data).execute()

                reply_text = f"✅ จดแล้ว!\n🔤 {word}\n📖 {meaning}\n🗣️ {example}"
            except Exception as e:
                print(e)
                reply_text = "⚠️ AI ง่วงนอน ลองใหม่อีกทีนะครับ"

    # === MENU 5: โหมดตรวจการบ้าน (Strict Teacher Mode) ===
    else:
        # 1. เช็คสถานะเก่า (Retry Logic)
        session = user_sessions.get(user_id, {'attempts': 0, 'last_word': ''})
        current_attempt = session['attempts'] + 1
        
        try:
            # 🔥 PROMPT: ครูระเบียบจัด (Strict but helpful)
            # เช็ค 2 อย่าง: Grammar ถูกไหม? + ความพยายาม (Effort) พอไหม?
            prompt = (f"User input: '{user_msg}'\n"
                      f"Role: English Teacher. Be supportive but strict on quality.\n"
                      f"Task: Evaluate the sentence based on 2 criteria:\n"
                      f"1. **Grammar**: Is it grammatically correct? (Ignore minor punctuation/capitalization).\n"
                      f"2. **Effort & Detail**: REJECT sentences that are too simple (e.g., 'I eat', 'She runs', 'It is bad'). "
                      f"   User MUST include an object, preposition, or adjective (e.g., 'I eat an apple', 'She runs fast').\n"
                      f"3. **Analysis**: Explain *why* it is wrong or too simple.\n\n"
                      
                      f"Output Format:\n"
                      f"Word: [Main Vocabulary used]\n"
                      f"Pass: [Yes/No] (Mark 'No' if grammar is wrong OR sentence is too simple)\n"
                      f"Reason: [Short analysis in Thai. e.g., 'Grammar ผิดตรง...' or 'ประโยคสั้นไป ลองเติมกรรมหน่อย']\n"
                      f"Feedback: [Actionable advice in Thai. e.g., 'ลองบอกเพิ่มว่ากินอะไร?']\n"
                      f"Better: [A corrected or better version in English]")
            
            res = model.generate_content(prompt)
            ai_text = res.text.strip()
            
            # Default Values
            detected_word = ""
            is_pass = False
            reason = "ประโยคยังไม่สมบูรณ์"
            feedback = "ลองใหม่นะครับ"
            better_ver = "No suggestion"

            # Parse Response
            for line in ai_text.split('\n'):
                if line.startswith("Word:"): detected_word = line.replace("Word:", "").strip()
                elif line.startswith("Pass:"): is_pass = "Yes" in line
                elif line.startswith("Reason:"): reason = line.replace("Reason:", "").strip()
                elif line.startswith("Feedback:"): feedback = line.replace("Feedback:", "").strip()
                elif line.startswith("Better:"): better_ver = line.replace("Better:", "").strip()

            # --- ตัดสินใจตอบกลับ ---
            if is_pass:
                # ✅ ผ่านฉลุย (Reset Quota)
                if user_id in user_sessions: del user_sessions[user_id]
                
                reply_text = (f"🎉 เยี่ยมมาก! ผ่านครับ\n"
                              f"ศัพท์: {detected_word}\n"
                              f"ผล: ✅ ผ่านฉลุย\n"
                              f"💡 {reason}\n"  # บอกเหตุผลว่าทำไมถึงดี
                              f"✨ ตัวอย่างสวยๆ: \"{better_ver}\"")
                
                # Log Success (Optional)
                try:
                    vocab_data = supabase.table("vocab").select("id").ilike("word", detected_word).execute().data
                    v_id = vocab_data[0]['id'] if vocab_data else None
                    supabase.table("user_logs").insert({
                        "user_id": user_id, "vocab_id": v_id, "user_answer": user_msg, "is_correct": True
                    }).execute()
                except: pass

            else:
                # ❌ ไม่ผ่าน (Grammar ผิด หรือ ง่ายไป)
                if current_attempt < 3:
                    # ยังไม่ครบ 3 ครั้ง -> ให้แก้ตัว
                    user_sessions[user_id] = {'attempts': current_attempt, 'last_word': detected_word}
                    reply_text = (f"🤔 ยังไม่ผ่านครับ (ครั้งที่ {current_attempt}/3)\n"
                                  f"❌ ปัญหา: {reason}\n"
                                  f"💡 คำแนะนำ: {feedback}\n"
                                  f"👉 สู้ๆ ลองแต่งใหม่ให้ยาวกว่าเดิมนิดนึง!")
                else:
                    # ครบ 3 ครั้ง -> เฉลย
                    if user_id in user_sessions: del user_sessions[user_id]
                    reply_text = (f"❌ ครบ 3 ครั้งแล้ว (3/3)\n"
                                  f"📝 จุดที่ต้องแก้: {reason}\n"
                                  f"🔑 เฉลยที่ควรเป็น: \"{better_ver}\"\n"
                                  f"จำไว้นะครับ ครั้งหน้าเอาใหม่!")

        except Exception as e:
            print(f"Grading Error: {e}")
            reply_text = "😵‍💫 ครู AI มึนหัวนิดหน่อย ลองส่งใหม่อีกทีนะครับ"

    # ส่งข้อความกลับ
    if reply_text:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))