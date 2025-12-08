# vocab-bot-offline.py
import os
import random
import json
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client
from dotenv import load_dotenv
import difflib  # สำหรับตรวจสอบความใกล้เคียงของคำ

# --- 1. CONFIGURATION ---
load_dotenv()

app = FastAPI()

# Load Environment Variables
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# Check Keys
if not all([LINE_ACCESS_TOKEN, LINE_SECRET, SUPABASE_URL, SUPABASE_KEY]):
    print("⚠️ Warning: Environment variables are missing!")

# Setup Clients
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# Setup Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Error: {e}")

# 🔥 GLOBAL STATE (RAM)
user_sessions = {}

# 🔥 LOCAL SYNONYMS DATABASE
SYNONYMS_DB = {
    # คำกริยา
    "learn": ["เรียนรู้", "เรียน", "ศึกษา", "หาความรู้", "ฝึกฝน"],
    "study": ["เรียน", "ศึกษา", "ค้นคว้า", "ทบทวน"],
    "practice": ["ฝึกฝน", "ฝึก", "ปฏิบัติ", "ทำซ้ำ"],
    "improve": ["ปรับปรุง", "พัฒนา", "ทำให้ดีขึ้น"],
    "remember": ["จำ", "จำได้", "ระลึกได้"],
    "forget": ["ลืม", "จำไม่ได้"],
    "understand": ["เข้าใจ", "รู้เรื่อง", "ตระหนัก"],
    
    # คำคุณศัพท์
    "happy": ["มีความสุข", "สุขใจ", "ปลาบปลื้ม", "ยินดี"],
    "sad": ["เศร้า", "เสียใจ", "เศร้าสร้อย"],
    "big": ["ใหญ่", "กว้างขวาง", "มโหฬาร"],
    "small": ["เล็ก", "จ้อย", "น้อย"],
    "beautiful": ["สวย", "งาม", "งดงาม"],
    "difficult": ["ยาก", "ลำบาก", "ยุ่งยาก"],
    "easy": ["ง่าย", "สะดวก", "ราบรื่น"],
    
    # คำนาม
    "knowledge": ["ความรู้", "ภูมิรู้", "วิชาการ"],
    "friend": ["เพื่อน", "สหาย", "มิตร"],
    "home": ["บ้าน", "ที่อยู่อาศัย", "เรือน"],
    "food": ["อาหาร", "ของกิน", "โภชนาการ"],
    "water": ["น้ำ", "แหล่งน้ำ"],
    "money": ["เงิน", "ทุน", "ทรัพย์"],
    "time": ["เวลา", "วาระ", "คราว"],
    
    # คำพื้นฐานเพิ่มเติม
    "good": ["ดี", "ดีเยี่ยม", "ยอดเยี่ยม", "เลิศ"],
    "bad": ["แย่", "ไม่ดี", "เลว"],
    "new": ["ใหม่", "ใหม่เอี่ยม"],
    "old": ["เก่า", "แก่", "โบราณ"],
    "fast": ["เร็ว", "ว่องไว", "รวดเร็ว"],
    "slow": ["ช้า", "เนิบ", "เชื่องช้า"],
    "hot": ["ร้อน", "อุ่น"],
    "cold": ["เย็น", "หนาว", "เย็นยะเยือก"],
    "high": ["สูง", "ชั้นสูง"],
    "low": ["ต่ำ", "ชั้นต่ำ"],
    "right": ["ถูกต้อง", "ใช่", "เหมาะสม"],
    "wrong": ["ผิด", "ไม่ถูกต้อง", "คลาดเคลื่อน"],
}

# 🔥 EXAMPLE SENTENCES DATABASE
EXAMPLES_DB = {
    "learn": [
        "I want to learn English.",
        "She learns quickly.",
        "We learn from our mistakes."
    ],
    "study": [
        "He studies at university.",
        "I need to study for the exam.",
        "She is studying medicine."
    ],
    "practice": [
        "Practice makes perfect.",
        "I practice piano every day.",
        "They practice speaking English."
    ],
    "happy": [
        "I am very happy today.",
        "Happy birthday to you!",
        "They look so happy together."
    ],
    "sad": [
        "She felt sad after the movie.",
        "It's sad to see them go.",
        "Why are you so sad?"
    ],
    "friend": [
        "He is my best friend.",
        "We met a new friend yesterday.",
        "A good friend is hard to find."
    ],
    "home": [
        "I will go home soon.",
        "Home is where the heart is.",
        "She works from home."
    ],
}

# --- 2. HELPER FUNCTIONS ---
def save_user(user_id):
    """เก็บ User ID ลง DB"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except: 
        pass

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
    """เพิ่ม/ลดคะแนน"""
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
            # ถ้าไม่มีใน DB ให้ใช้ default words
            return get_default_vocab(exclude_words)
        
        # กรองคำที่เรียนแล้ว
        available = [v for v in vocab_list if v['word'].lower() not in [w.lower() for w in exclude_words]]
        
        if not available:
            # ถ้าเรียนหมดแล้ว ให้สุ่มจากทั้งหมด
            available = vocab_list
        
        return random.choice(available)
    except:
        return get_default_vocab(exclude_words)

def get_default_vocab(exclude_words=[]):
    """Default vocabulary list"""
    default_words = [
        {"word": "learn", "meaning": "เรียนรู้", "example": "I want to learn English."},
        {"word": "study", "meaning": "ศึกษา", "example": "He studies at university."},
        {"word": "practice", "meaning": "ฝึกฝน", "example": "Practice makes perfect."},
        {"word": "happy", "meaning": "มีความสุข", "example": "I am very happy today."},
        {"word": "friend", "meaning": "เพื่อน", "example": "He is my best friend."},
        {"word": "home", "meaning": "บ้าน", "example": "I will go home soon."},
        {"word": "book", "meaning": "หนังสือ", "example": "This is an interesting book."},
        {"word": "water", "meaning": "น้ำ", "example": "Drink more water."},
        {"word": "food", "meaning": "อาหาร", "example": "Thai food is delicious."},
        {"word": "time", "meaning": "เวลา", "example": "Time is valuable."},
    ]
    
    available = [w for w in default_words if w['word'].lower() not in exclude_words]
    if not available:
        available = default_words
    
    return random.choice(available)

def check_answer_offline(word, correct_meaning, user_answer):
    """ตรวจคำตอบแบบ offline"""
    user_answer = user_answer.strip().lower()
    correct_meaning_lower = correct_meaning.lower()
    
    # 1. ตรวจสอบตรงกันเป๊ะ
    if user_answer == correct_meaning_lower:
        return {
            "is_correct": True,
            "feedback": "ถูกต้องเป๊ะ! 🎯",
            "confidence": 1.0
        }
    
    # 2. ตรวจสอบคำพ้องความหมาย
    synonyms = SYNONYMS_DB.get(word.lower(), [])
    for synonym in synonyms:
        if synonym in user_answer or user_answer in synonym:
            return {
                "is_correct": True,
                "feedback": f"ใช้คำพ้องความหมาย '{synonym}' ก็ได้ครับ! ✅",
                "confidence": 0.9
            }
    
    # 3. ตรวจสอบความใกล้เคียง (string similarity)
    similarity = difflib.SequenceMatcher(None, user_answer, correct_meaning_lower).ratio()
    if similarity > 0.7:
        return {
            "is_correct": True,
            "feedback": f"ใกล้เคียงมาก! ({similarity*100:.0f}%) 👍",
            "confidence": similarity
        }
    
    # 4. ตรวจสอบคำที่อยู่ในความหมายเดียวกัน
    correct_words = correct_meaning_lower.split()
    user_words = user_answer.split()
    
    matching_words = sum(1 for uw in user_words if any(cw in uw or uw in cw for cw in correct_words))
    if matching_words >= len(correct_words) * 0.5:  # ครึ่งหนึ่งของคำที่ต้องใช้
        return {
            "is_correct": True,
            "feedback": "ใช้คำบางส่วนถูกต้องแล้ว! 😊",
            "confidence": 0.6
        }
    
    # 5. ถ้าผิดทั้งหมด
    return {
        "is_correct": False,
        "feedback": f"ลองใหม่อีกครั้งนะครับ",
        "confidence": 0.0
    }

def get_examples(word, count=2):
    """ดึงตัวอย่างประโยค"""
    word_lower = word.lower()
    
    # 1. ดูจาก examples database
    if word_lower in EXAMPLES_DB:
        examples = EXAMPLES_DB[word_lower]
        if len(examples) >= count:
            return random.sample(examples, count)
    
    # 2. ดูจาก DB
    try:
        result = supabase.table("vocab").select("example_sentence").eq("word", word).execute()
        if result.data and result.data[0].get('example_sentence'):
            return [result.data[0]['example_sentence']] + ["Try to use this word in conversation."]
    except:
        pass
    
    # 3. Default examples
    return [
        f"Can you use '{word}' in a sentence?",
        f"Practice using the word '{word}' daily."
    ]

def add_vocab_offline(word):
    """เพิ่มคำศัพท์แบบ offline"""
    # ตัวอย่างความหมายพื้นฐาน
    basic_meanings = {
        "learn": "เรียนรู้",
        "study": "ศึกษา", 
        "practice": "ฝึกฝน",
        "happy": "มีความสุข",
        "sad": "เศร้า",
        "friend": "เพื่อน",
        "home": "บ้าน",
        "book": "หนังสือ",
        "water": "น้ำ",
        "food": "อาหาร",
        "time": "เวลา",
        "good": "ดี",
        "bad": "แย่",
        "new": "ใหม่",
        "old": "เก่า",
        "big": "ใหญ่",
        "small": "เล็ก"
    }
    
    word_lower = word.lower()
    meaning = basic_meanings.get(word_lower, "โปรดระบุความหมายเพิ่มเติม")
    
    # สร้างตัวอย่างประโยคง่ายๆ
    examples = [
        f"I want to {word} more vocabulary.",
        f"She can {word} very well.",
        f"Let's {word} together."
    ]
    
    return {
        "meaning": meaning,
        "example": random.choice(examples)
    }

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Teacher Bot V3 (Offline Mode) is ready!"}

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

            msg = (f"🔥 ภารกิจมาแล้ว!\n\n"
                   f"❓ คำว่า '{word}' แปลว่าอะไร?\n\n"
                   f"💡 ตอบผิดไม่เป็นไร เดี๋ยวมีเฉลยพร้อมตัวอย่างให้ครับ")

            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=msg))
                # เก็บ session
                user_sessions[user_id] = {
                    'word': word,
                    'meaning': meaning
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
    if user_msg in ["คำสั่ง", "เมนู", "menu", "help"]:
        score, learned = get_user_score(user_id)
        reply_text = (f"🤖 คู่มือครูพี่ Bot V3 (Offline Mode):\n\n"
                      f"1. เริ่มเกม -> เริ่มทายคำศัพท์\n"
                      f"2. คะแนน -> ดูคะแนน\n"
                      f"3. คำใบ้ -> ขอคำใบ้ (ลด -2 คะแนน)\n"
                      f"4. เพิ่ม:[ศัพท์] -> เพิ่มคำใหม่\n"
                      f"5. ลบ:[ศัพท์] -> ลบคำ\n"
                      f"6. คลัง -> ดูศัพท์ทั้งหมด\n"
                      f"7. คลัง2 -> ดูศัพท์ที่เรียนแล้ว\n\n"
                      f"📊 คะแนน: {score} | 📚 จำได้: {len(learned)} คำ")

    # === MENU 2: คะแนน ===
    elif user_msg in ["คะแนน", "score", "สถิติ", "stats"]:
        score, learned = get_user_score(user_id)
        reply_text = (f"📊 สถิติความเทพ:\n\n"
                      f"⭐ คะแนนรวม: {score} XP\n"
                      f"📚 คำศัพท์ที่แม่นแล้ว: {len(learned)} คำ")
        
        if learned:
            learned_list = ", ".join(learned[:10])
            if len(learned) > 10:
                learned_list += f" และอีก {len(learned)-10} คำ"
            reply_text += f"\n\nคำที่เรียนแล้ว:\n{learned_list}"

    # === MENU 3: เริ่มเกม ===
    elif user_msg in ["เริ่มเกม", "เริ่ม", "start", "play", "game"]:
        _, learned = get_user_score(user_id)
        selected = get_random_vocab(learned)
        
        if not selected:
            reply_text = "📭 คลังศัพท์ว่างเปล่า! พิมพ์ 'เพิ่ม:[คำศัพท์]' เพื่อใส่คำใหม่ก่อนครับ"
        else:
            word = selected['word']
            meaning = selected.get('meaning', '-')
            
            # Reset Session ใหม่
            user_sessions[user_id] = {
                'word': word,
                'meaning': meaning,
                'hint_given': False
            }
            
            reply_text = (f"🎮 เริ่มเกม!\n\n"
                          f"❓ คำว่า '{word}' แปลว่าอะไร?\n\n"
                          f"💡 ตอบภาษาไทยมาเลย (ระบบ offline ตรวจอัตโนมัติ)")

    # === MENU 4: คำใบ้ ===
    elif user_msg in ["คำใบ้", "hint", "ช่วยด้วย"]:
        if user_id not in user_sessions:
            reply_text = "🤔 ยังไม่ได้เริ่มเกมเลยครับ พิมพ์ 'เริ่มเกม' ก่อนนะ"
        else:
            session = user_sessions[user_id]
            if session.get('hint_given'):
                reply_text = f"💡 ให้คำใบ้ไปแล้วไงครับ: {session['meaning']}"
            else:
                new_score = update_score(user_id, -2)
                session['hint_given'] = True
                user_sessions[user_id] = session
                
                reply_text = (f"💡 คำใบ้: {session['meaning']}\n"
                              f"(-2 คะแนน | เหลือ: {new_score})\n\n"
                              f"ถ้ารู้แล้วพิมพ์ตอบมาเลย!")

    # === MENU 5: คลังคำศัพท์ ===
    elif user_msg in ["คลังคำศัพท์", "คลัง", "vocab", "words"]:
        try:
            response = supabase.table("vocab").select("word, meaning").order("id", desc=True).limit(20).execute()
            words = response.data
            if not words:
                reply_text = "📭 คลังว่างเปล่าครับ พิมพ์ 'เพิ่ม:[คำศัพท์]' เพื่อเพิ่ม"
            else:
                word_list = "\n".join([f"- {item['word']} = {item.get('meaning', '?')}" for item in words])
                reply_text = f"📚 ศัพท์ 20 คำล่าสุด:\n\n{word_list}"
        except: 
            reply_text = "⚠️ ดึงข้อมูลไม่ได้ครับ"

    # === MENU 6: คำที่เรียนแล้ว ===
    elif user_msg in ["คลัง2", "เรียนแล้ว", "learned"]:
        score, learned = get_user_score(user_id)
        if not learned:
            reply_text = "📭 ยังไม่มีคำที่เรียนแล้วเลยครับ"
        else:
            word_list = "\n".join([f"- {word}" for word in learned[:20]])
            if len(learned) > 20:
                word_list += f"\n... และอีก {len(learned)-20} คำ"
            reply_text = f"📚 คำศัพท์ที่เรียนแล้ว ({len(learned)} คำ):\n\n{word_list}"

    # === MENU 7: ลบคำศัพท์ ===
    elif user_msg.startswith(("ลบ:", "ลบคำ:", "delete:")):
        try:
            target = user_msg.split(":", 1)[1].strip()
            if target:
                supabase.table("vocab").delete().ilike("word", target).execute()
                reply_text = f"🗑️ ลบ '{target}' เรียบร้อยครับ"
            else: 
                reply_text = "อย่าลืมใส่คำที่ต้องการลบหลัง : ด้วยนะครับ เช่น 'ลบ:learn'"
        except: 
            reply_text = "⚠️ ระบบลบมีปัญหา ลองใหม่ครับ"

    # === MENU 8: เพิ่มคำศัพท์ (Offline Mode) ===
    elif user_msg.lower().startswith(("เพิ่ม:", "add:", "new:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
            if not word:
                reply_text = "ใส่คำศัพท์หลัง : ด้วยนะครับ เช่น 'เพิ่ม: Resilience'"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                return
            
            # ใช้ offline function
            result = add_vocab_offline(word)
            meaning = result["meaning"]
            example = result["example"]
            
            # บันทึกลง database
            try:
                supabase.table("vocab").insert({
                    "word": word, 
                    "meaning": meaning, 
                    "example_sentence": example
                }).execute()
            except:
                pass  # ถ้า DB error ก็ยังตอบได้
            
            reply_text = (f"✅ เพิ่มคำศัพท์สำเร็จ!\n\n"
                         f"🔤 คำศัพท์: {word}\n"
                         f"📖 ความหมาย: {meaning}\n"
                         f"🗣️ ตัวอย่าง: {example}\n\n"
                         f"💡 พิมพ์ 'เริ่มเกม' เพื่อเล่นทันที!")
            
        except Exception as e:
            print(f"Add vocab error: {e}")
            reply_text = "⚠️ มีปัญหาในการเพิ่มคำศัพท์ ลองใหม่อีกครั้งครับ"

    # === MENU 9: ตรวจคำตอบ (Offline Mode) ===
    else:
        if user_id not in user_sessions:
            reply_text = ("🤔 อยากเล่นเกมพิมพ์ 'เริ่มเกม' ได้เลยครับ\n\n"
                         "💡 หรือพิมพ์ 'คำสั่ง' เพื่อดูเมนูทั้งหมด")
        else:
            session = user_sessions[user_id]
            word = session['word']
            correct_meaning = session['meaning']
            
            # ตรวจคำตอบแบบ offline
            result = check_answer_offline(word, correct_meaning, user_msg)
            is_correct = result["is_correct"]
            feedback = result["feedback"]
            
            # ดึงตัวอย่างประโยค
            examples = get_examples(word, 2)
            example_txt = "\n".join([f"• {ex}" for ex in examples]) if examples else ""
            
            # ล้าง session
            del user_sessions[user_id]

            if is_correct:
                # ✅ ถูกต้อง - ให้คะแนน
                new_score = update_score(user_id, 10)
                mark_word_learned(user_id, word)
                
                reply_text = (f"🎉 ถูกต้อง! (+10 คะแนน)\n\n"
                             f"💬 {feedback}\n"
                             f"📊 คะแนนรวมตอนนี้: {new_score}\n")
                
                if example_txt:
                    reply_text += f"\n📝 ตัวอย่างการใช้:\n{example_txt}\n"
                
                reply_text += "\n👉 พิมพ์ 'เริ่มเกม' เพื่อเล่นต่อ"
                
            else:
                # ❌ ผิด
                new_score = update_score(user_id, -1)
                
                reply_text = (f"❌ ยังไม่ถูกนะครับ (-1 คะแนน)\n\n"
                             f"📖 คำที่ถูกต้อง: {word} → {correct_meaning}\n"
                             f"💡 {feedback}\n")
                
                if example_txt:
                    reply_text += f"\n📝 ตัวอย่างช่วยจำ:\n{example_txt}\n"
                
                reply_text += f"\n📊 คะแนนรวม: {new_score}\n"
                reply_text += "ไม่เป็นไรครับ! พิมพ์ 'เริ่มเกม' เพื่อลองคำใหม่ได้เลย 😊"

    # ส่งข้อความกลับ Line
    if reply_text:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            print(f"LINE Reply Error: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)