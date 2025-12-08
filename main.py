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

# 🔥 GEMINI CONFIG
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
# Structure: { 'user_id': {'word': 'revise', 'meaning': '...', 'attempts': 0, 'hint_given': False} }
user_sessions = {}

# --- 2. HELPER FUNCTIONS ---
def save_user(user_id):
    """เก็บ User ID ลง DB"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except: pass

def get_user_score(user_id):
    """ดึงคะแนนปัจจุบัน"""
    try:
        result = supabase.table("user_scores").select("score, learned_words").eq("user_id", user_id).execute()
        if result.data:
            return result.data[0]['score'], result.data[0].get('learned_words', [])
        return 0, []
    except:
        return 0, []

def update_score(user_id, points):
    """เพิ่มคะแนน"""
    try:
        score, learned = get_user_score(user_id)
        new_score = score + points
        supabase.table("user_scores").upsert({
            "user_id": user_id,
            "score": new_score,
            "learned_words": learned
        }, on_conflict="user_id").execute()
        return new_score
    except:
        return 0

def mark_word_learned(user_id, word):
    """บันทึกว่าเรียนคำนี้แล้ว"""
    try:
        score, learned = get_user_score(user_id)
        if word.lower() not in [w.lower() for w in learned]:
            learned.append(word.lower())
            supabase.table("user_scores").upsert({
                "user_id": user_id,
                "score": score,
                "learned_words": learned
            }, on_conflict="user_id").execute()
    except:
        pass

def get_random_vocab(exclude_words=[]):
    """สุ่มศัพท์ที่ยังไม่เคยเรียน"""
    try:
        vocab_list = supabase.table("vocab").select("*").execute().data
        if not vocab_list:
            return None
        
        # กรองคำที่เรียนแล้ว
        available = [v for v in vocab_list if v['word'].lower() not in [w.lower() for w in exclude_words]]
        
        if not available:
            # ถ้าเรียนหมดแล้ว ให้สุ่มจากทั้งหมด
            available = vocab_list
        
        return random.choice(available)
    except:
        return None

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Teacher Bot is ready!"}

@app.get("/broadcast-quiz")
def broadcast_quiz():
    """ยิงโจทย์หาทุกคน (Cron Job)"""
    try:
        users = supabase.table("users").select("user_id").execute().data
        if not users: 
            return {"msg": "No users found"}

        for user in users:
            user_id = user['user_id']
            _, learned = get_user_score(user_id)
            selected = get_random_vocab(learned)
            
            if not selected:
                continue
                
            word = selected['word']
            meaning = selected.get('meaning', '-')

            msg = (f"🔥 ภารกิจประลองปัญญา!\n\n"
                   f"❓ คำว่า '{word}' แปลว่าอะไร?\n\n"
                   f"💡 พิมพ์คำตอบมาเลย (ภาษาไทย)")

            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=msg))
                # เก็บ session
                user_sessions[user_id] = {
                    'word': word,
                    'meaning': meaning,
                    'attempts': 0,
                    'hint_given': False
                }
            except: 
                continue 
            
        return {"status": "success", "sent_to": len(users)}
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
    if user_msg in ["คำสั่ง", "เมนู", "menu"]:
        score, learned = get_user_score(user_id)
        reply_text = (f"🤖 คู่มือครูพี่ Bot:\n\n"
                      f"1. เริ่มเกม -> เริ่มทายคำศัพท์\n"
                      f"2. คะแนน -> ดูคะแนนและสถิติ\n"
                      f"3. คำใบ้ -> ขอคำใบ้ (ลดคะแนน -2)\n"
                      f"4. เพิ่ม: [ศัพท์] -> เพิ่มคำใหม่\n"
                      f"5. ลบคำศัพท์: [ศัพท์] -> ลบคำ\n"
                      f"6. คลังคำศัพท์ -> ดูทั้งหมด\n\n"
                      f"📊 คะแนนปัจจุบัน: {score} คะแนน\n"
                      f"📚 เรียนไปแล้ว: {len(learned)} คำ")

    # === MENU 2: คะแนน ===
    elif user_msg in ["คะแนน", "score", "สถิติ"]:
        score, learned = get_user_score(user_id)
        reply_text = (f"📊 สถิติของคุณ:\n\n"
                      f"⭐ คะแนนรวม: {score} คะแนน\n"
                      f"📚 จำนวนคำที่เรียน: {len(learned)} คำ\n"
                      f"🎯 อัตราความสำเร็จ: {len(learned)*100//max(len(learned)+1,1)}%")

    # === MENU 3: เริ่มเกม ===
    elif user_msg in ["เริ่มเกม", "เริ่ม", "start", "play"]:
        _, learned = get_user_score(user_id)
        selected = get_random_vocab(learned)
        
        if not selected:
            reply_text = "📭 คลังศัพท์ว่างเปล่า ใช้คำสั่ง 'เพิ่ม: [คำศัพท์]' เพื่อเพิ่มคำใหม่"
        else:
            word = selected['word']
            meaning = selected.get('meaning', '-')
            
            user_sessions[user_id] = {
                'word': word,
                'meaning': meaning,
                'attempts': 0,
                'hint_given': False
            }
            
            reply_text = (f"🎮 เกมเริ่มแล้ว!\n\n"
                          f"❓ คำว่า '{word}' แปลว่าอะไร?\n\n"
                          f"💡 พิมพ์คำตอบเป็นภาษาไทย\n"
                          f"🆘 พิมพ์ 'คำใบ้' หากต้องการความช่วยเหลือ")

    # === MENU 4: คำใบ้ ===
    elif user_msg in ["คำใบ้", "hint", "help"]:
        if user_id not in user_sessions:
            reply_text = "🤔 ยังไม่มีเกมที่กำลังเล่น พิมพ์ 'เริ่มเกม' ก่อนนะครับ"
        else:
            session = user_sessions[user_id]
            if session['hint_given']:
                reply_text = f"💡 ได้ให้คำใบ้ไปแล้ว: {session['meaning']}"
            else:
                # ลดคะแนน
                new_score = update_score(user_id, -2)
                session['hint_given'] = True
                user_sessions[user_id] = session
                
                reply_text = (f"💡 คำใบ้: {session['meaning']}\n"
                              f"(-2 คะแนน, คะแนนตอนนี้: {new_score})\n\n"
                              f"ลองตอบใหม่ดูสิ!")

    # === MENU 5: คลังคำศัพท์ ===
    elif user_msg in ["คลังคำศัพท์", "คลัง", "vocab"]:
        try:
            response = supabase.table("vocab").select("word").order("id", desc=True).limit(20).execute()
            words = response.data
            if not words:
                reply_text = "📭 คลังว่างเปล่าครับ"
            else:
                word_list = "\n".join([f"{i+1}. {item['word']}" for i, item in enumerate(words)])
                reply_text = f"📚 ศัพท์ล่าสุด (20 คำ):\n\n{word_list}"
        except: 
            reply_text = "⚠️ ดึงข้อมูลไม่ได้ครับ"

    # === MENU 6: ลบคำศัพท์ ===
    elif user_msg.startswith("ลบคำศัพท์:") or user_msg.startswith("ลบ:"):
        try:
            target = user_msg.split(":", 1)[1].strip()
            if target:
                supabase.table("vocab").delete().ilike("word", target).execute()
                reply_text = f"🗑️ ลบ '{target}' แล้วครับ"
            else: 
                reply_text = "ระบุคำหลัง : ด้วยนะครับ"
        except: 
            reply_text = "⚠️ ลบไม่ได้ครับ"

    # === MENU 7: เพิ่มคำศัพท์ ===
    elif user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
            if word:
                prompt = (f"Word: '{word}'. Translate to Thai & English Example. "
                          f"Format:\nMeaning: ...\nExample: ...")
                res = model.generate_content(prompt)
                
                meaning, example = "-", "-"
                for line in res.text.strip().split('\n'):
                    if line.startswith("Meaning:"): 
                        meaning = line.replace("Meaning:", "").strip()
                    elif line.startswith("Example:"): 
                        example = line.replace("Example:", "").strip()

                supabase.table("vocab").insert({
                    "word": word, 
                    "meaning": meaning, 
                    "example_sentence": example
                }).execute()
                
                reply_text = f"✅ จดแล้ว!\n🔤 {word}\n📖 {meaning}\n🗣️ {example}"
            else: 
                reply_text = "ใส่คำศัพท์หลัง : ด้วยนะครับ"
        except Exception as e:
            print(f"Add vocab error: {e}")
            reply_text = "⚠️ AI กำลังมึน ลองใหม่ครับ"

    # === MENU 8: ตอบคำถาม (ตรวจคำตอบ) ===
    else:
        # เช็คว่ามี Session หรือไม่
        if user_id not in user_sessions:
            reply_text = "🤔 พิมพ์ 'เริ่มเกม' เพื่อเริ่มทายคำศัพท์\nหรือพิมพ์ 'คำสั่ง' ดูเมนู"
        else:
            session = user_sessions[user_id]
            correct_meaning = session['meaning']
            word = session['word']
            current_attempt = session['attempts'] + 1
            
            try:
                # ใช้ AI ตรวจคำตอบ (Flexible)
                prompt = (f"Question: The word '{word}' means what in Thai?\n"
                          f"Correct answer: {correct_meaning}\n"
                          f"User answer: '{user_msg}'\n\n"
                          f"Task: Check if user's answer is correct (accept synonyms/similar meanings).\n"
                          f"Output ONLY:\n"
                          f"Correct: [Yes or No]\n"
                          f"Reason: [Thai explanation in 1 line]")
                
                res = model.generate_content(prompt)
                ai_text = res.text.strip()
                
                # Parse
                is_correct = False
                reason = "ไม่ระบุ"
                
                for line in ai_text.split('\n'):
                    line = line.strip()
                    if line.startswith("Correct:"): 
                        is_correct = "yes" in line.lower()
                    elif line.startswith("Reason:"): 
                        reason = line.replace("Reason:", "").strip()
                
                # ตัดสินผล
                if is_correct:
                    # ✅ ถูกต้อง
                    points = 10 - (current_attempt * 2) - (5 if session['hint_given'] else 0)
                    points = max(points, 1)  # ขั้นต่ำ 1 คะแนน
                    
                    new_score = update_score(user_id, points)
                    mark_word_learned(user_id, word)
                    
                    # ลบ session
                    del user_sessions[user_id]
                    
                    # สร้างประโยคตัวอย่าง
                    try:
                        example_prompt = f"Create a simple English sentence using the word '{word}'. Just the sentence, no explanation."
                        example_res = model.generate_content(example_prompt)
                        example_sentence = example_res.text.strip()
                    except:
                        example_sentence = f"I need to {word} my notes."
                    
                    reply_text = (f"🎉 ถูกต้อง! +{points} คะแนน\n\n"
                                  f"✅ {reason}\n"
                                  f"📊 คะแนนรวม: {new_score}\n\n"
                                  f"💬 ตัวอย่างประโยค:\n\"{example_sentence}\"\n\n"
                                  f"พิมพ์ 'เริ่มเกม' เล่นต่อ!")
                
                else:
                    # ❌ ผิด
                    if current_attempt < 3:
                        session['attempts'] = current_attempt
                        user_sessions[user_id] = session
                        
                        reply_text = (f"❌ ยังไม่ถูกนะครับ ({current_attempt}/3)\n\n"
                                      f"💭 {reason}\n\n"
                                      f"🔄 ลองใหม่อีกครั้ง หรือพิมพ์ 'คำใบ้'")
                    else:
                        # ครบ 3 ครั้ง
                        del user_sessions[user_id]
                        update_score(user_id, -3)
                        
                        reply_text = (f"❌ ครบ 3 ครั้งแล้ว (-3 คะแนน)\n\n"
                                      f"📖 คำตอบที่ถูก: {correct_meaning}\n"
                                      f"💡 {reason}\n\n"
                                      f"จำไว้นะครับ! พิมพ์ 'เริ่มเกม' เล่นต่อ")
            
            except Exception as e:
                print(f"Check answer error: {e}")
                reply_text = "😵‍💫 ระบบขัดข้อง ลองส่งใหม่อีกทีครับ"

    # ส่งข้อความตอบกลับ
    if reply_text:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            print(f"LINE Reply Error: {e}")