import os
import random
import json
import time
import re
import logging
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client
from dotenv import load_dotenv
from functools import wraps
from datetime import datetime

# --- 1. CONFIGURATION ---
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI()

# Load Environment Variables
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# Check Keys
if not all([LINE_ACCESS_TOKEN, LINE_SECRET, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    logger.error("⚠️ Warning: Environment variables are missing!")
    print("⚠️ Warning: Environment variables are missing!")

# Setup Clients
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 🔥 GEMINI CONFIG
genai.configure(api_key=GEMINI_API_KEY)
# ปรับ model เป็น flash เพื่อความไวและประหยัด
model = genai.GenerativeModel('gemini-flash-latest') 

# Setup Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Supabase connected successfully")
except Exception as e:
    logger.error(f"Supabase Connection Error: {e}")
    print(f"Supabase Connection Error: {e}")

# 🔥 GLOBAL STATE (RAM)
# Structure: { 'user_id': {'word': 'revise', 'meaning': '...'} }
user_sessions = {}
pending_deletions = {}  # สำหรับระบบลบแบบยืนยัน 2 ขั้นตอน

# --- 2. HELPER FUNCTIONS ---
def retry_on_failure(max_retries=3, delay=1):
    """Decorator for retry logic"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Function {func.__name__} failed after {max_retries} attempts: {e}")
                        raise
                    logger.warning(f"Retry {attempt + 1}/{max_retries} for {func.__name__}: {e}")
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

def sanitize_word(word):
    """ป้องกัน SQL injection และคำสั่งอันตราย"""
    if not word:
        return ""
    
    # ลบอักขระพิเศษ (อนุญาตเฉพาะ a-z, A-Z, 0-9, space, hyphen, apostrophe)
    word = re.sub(r'[^\w\s\-\']', '', word, flags=re.UNICODE)
    word = word.strip()
    
    # จำกัดความยาว
    if len(word) > 50:
        word = word[:50]
    
    return word

def log_operation(user_id, operation, details=""):
    """บันทึกการดำเนินการ"""
    try:
        log_msg = f"User:{user_id} | Operation:{operation} | Details:{details}"
        logger.info(log_msg)
        print(f"📝 LOG: {log_msg}")
        
        # ลองบันทึกลง DB
        try:
            supabase.table("logs").insert({
                "user_id": user_id,
                "operation": operation,
                "details": str(details),
                "timestamp": int(time.time())
            }).execute()
        except Exception as db_error:
            # ถ้าไม่มีตาราง logs ให้สร้างตาราง user_logs แทน
            if "Could not find the table" in str(db_error) and "logs" in str(db_error):
                logger.warning("Table 'logs' not found, skipping DB logging")
                # สร้างตารางผ่าน Python ไม่ได้ใน Supabase ต้องสร้างผ่าน SQL
                print("ℹ️ Note: Please create 'logs' table in Supabase SQL Editor")
            else:
                logger.error(f"Logging to DB failed: {db_error}")
                
    except Exception as e:
        logger.error(f"Logging error: {e}")

def save_user(user_id):
    """เก็บ User ID ลง DB"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
        log_operation(user_id, "save_user")
    except Exception as e:
        logger.error(f"Save user error: {e}")

def get_user_score(user_id):
    """ดึงคะแนนปัจจุบัน"""
    try:
        result = supabase.table("user_scores").select("score, learned_words").eq("user_id", user_id).execute()
        if result.data:
            return result.data[0]['score'], result.data[0].get('learned_words', [])
        return 0, []
    except Exception as e:
        logger.error(f"Get user score error: {e}")
        return 0, []

@retry_on_failure(max_retries=2)
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
        
        log_operation(user_id, "update_score", f"points:{points}, new_score:{new_score}")
        return new_score
    except Exception as e:
        logger.error(f"Update score error: {e}")
        return 0

def mark_word_learned(user_id, word):
    """บันทึกว่าเรียนคำนี้แล้ว"""
    try:
        score, learned = get_user_score(user_id)
        word_lower = word.lower()
        learned_lower = [w.lower() for w in learned]
        
        if word_lower not in learned_lower:
            learned.append(word)
            supabase.table("user_scores").upsert({
                "user_id": user_id,
                "score": score,
                "learned_words": learned
            }, on_conflict="user_id").execute()
            
            log_operation(user_id, "mark_word_learned", word)
    except Exception as e:
        logger.error(f"Mark word learned error: {e}")

@retry_on_failure(max_retries=2)
def get_random_vocab(exclude_words=[]):
    """สุ่มศัพท์ที่ยังไม่เคยเรียน"""
    try:
        vocab_list = supabase.table("vocab").select("*").execute().data
        if not vocab_list:
            return None
        
        exclude_lower = [w.lower() for w in exclude_words]
        available = [v for v in vocab_list if v['word'].lower() not in exclude_lower]
        
        if not available:
            available = vocab_list
        
        return random.choice(available) if available else None
    except Exception as e:
        logger.error(f"Get random vocab error: {e}")
        return None

def save_user_log(user_id, vocab_id, is_correct, user_answer):
    """บันทึกประวัติการตอบ"""
    try:
        supabase.table("user_logs").insert({
            "user_id": user_id,
            "vocab_id": vocab_id,
            "is_correct": is_correct,
            "user_answer": user_answer
        }).execute()
    except Exception as e:
        logger.error(f"Save user log error: {e}")

def get_vocab_id_by_word(word):
    """หาค่า id ของคำศัพท์จาก word"""
    try:
        result = supabase.table("vocab").select("id").eq("word", word).execute()
        if result.data:
            return result.data[0]['id']
        return None
    except Exception as e:
        logger.error(f"Get vocab id error: {e}")
        return None

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Teacher Bot V2 (Senior Logic) is ready!", "time": datetime.now().isoformat()}

@app.get("/broadcast-quiz")
def broadcast_quiz():
    """ยิงโจทย์หาทุกคน (Cron Job)"""
    try:
        users = supabase.table("users").select("user_id").execute().data
        if not users: 
            return {"msg": "No users found"}

        success_count = 0
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
                    'meaning': meaning,
                    'hint_given': False,
                    'vocab_id': selected.get('id')
                }
                success_count += 1
                log_operation(user_id, "broadcast_quiz", word)
            except Exception as e:
                logger.error(f"Push message error for user {user_id}: {e}")
                continue 
            
        return {"status": "success", "sent_to": success_count, "total_users": len(users)}
    except Exception as e:
        logger.error(f"Broadcast quiz error: {e}")
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
    
    # Log incoming message
    log_operation(user_id, "received_message", user_msg[:50])

    # === MENU 1: คำสั่ง ===
    if user_msg in ["คำสั่ง", "เมนู", "menu", "help"]:
        score, learned = get_user_score(user_id)
        reply_text = (f"🤖 คู่มือครูพี่ Bot V2:\n\n"
                      f"1. เริ่มเกม -> เริ่มทายคำศัพท์\n"
                      f"2. คะแนน -> ดูคะแนน\n"
                      f"3. คำใบ้ -> ขอคำใบ้ (ลด -2 คะแนน)\n"
                      f"4. เพิ่ม: [ศัพท์] -> เพิ่มคำใหม่\n"
                      f"5. ลบ: [ศัพท์] -> ลบคำ\n"
                      f"6. คลัง -> ดูศัพท์ทั้งหมด\n"
                      f"7. สิทธ์ -> ตรวจสอบสิทธิ์\n"
                      f"8. ตรวจสอบระบบ -> สำหรับแอดมิน\n"
                      f"9. ยกเลิก -> ยกเลิกการกระทำ\n\n"
                      f"📊 คะแนน: {score} | 📚 จำได้: {len(learned)} คำ")

    # === MENU 2: คะแนน ===
    elif user_msg in ["คะแนน", "score", "สถิติ", "points"]:
        score, learned = get_user_score(user_id)
        reply_text = (f"📊 สถิติความเทพ:\n\n"
                      f"⭐ คะแนนรวม: {score} XP\n"
                      f"📚 คำศัพท์ที่แม่นแล้ว: {len(learned)} คำ\n"
                      f"🎯 เซสชั่นปัจจุบัน: {'มี' if user_id in user_sessions else 'ไม่มี'}")

    # === MENU 3: เริ่มเกม ===
    elif user_msg in ["เริ่มเกม", "เริ่ม", "start", "play", "quiz"]:
        # ลบ pending deletion ถ้ามี
        if user_id in pending_deletions:
            del pending_deletions[user_id]
        
        _, learned = get_user_score(user_id)
        selected = get_random_vocab(learned)
        
        if not selected:
            reply_text = "📭 คลังศัพท์ว่างเปล่า! พิมพ์ 'เพิ่ม: [คำศัพท์]' เพื่อใส่คำใหม่ก่อนครับ"
        else:
            word = selected['word']
            meaning = selected.get('meaning', '-')
            
            # Reset Session ใหม่
            user_sessions[user_id] = {
                'word': word,
                'meaning': meaning,
                'hint_given': False,
                'vocab_id': selected.get('id')
            }
            
            reply_text = (f"🎮 เริ่มกันเลย!\n\n"
                          f"❓ คำว่า '{word}' แปลว่าอะไร?\n\n"
                          f"💡 ตอบภาษาไทยมาเลย (ตอบผิดมีเฉลยให้ทันที)")

    # === MENU 4: คำใบ้ ===
    elif user_msg in ["คำใบ้", "hint", "clue"]:
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
    elif user_msg in ["คลังคำศัพท์", "คลัง", "vocab", "vocabulary"]:
        try:
            response = supabase.table("vocab").select("word, meaning").order("id", desc=True).limit(20).execute()
            words = response.data
            if not words:
                reply_text = "📭 คลังว่างเปล่าครับ"
            else:
                word_list = "\n".join([f"- {item['word']}: {item.get('meaning', '')[:30]}..." for item in words])
                reply_text = f"📚 ศัพท์ 20 คำล่าสุด:\n\n{word_list}\n\n📊 ทั้งหมด: {len(words)} คำ"
        except Exception as e:
            logger.error(f"Get vocab list error: {e}")
            reply_text = "⚠️ ดึงข้อมูลไม่ได้ครับ เช็ค DB แป๊บ"

    # === MENU 6: ลบคำศัพท์ (แบบยืนยัน 2 ขั้นตอน) ===
    elif user_msg.startswith(("ลบคำศัพท์:", "ลบ:", "delete:")):
        try:
            # ตรวจสอบว่าเป็นขั้นตอนยืนยันหรือไม่
            if user_id in pending_deletions and user_msg.lower() in ["ยืนยัน", "confirm", "yes"]:
                # ขั้นตอนที่ 2: ยืนยันการลบ
                word_to_delete = pending_deletions[user_id]
                
                try:
                    # ลบจากฐานข้อมูล
                    supabase.table("vocab")\
                        .delete()\
                        .eq("word", word_to_delete)\
                        .execute()
                    
                    log_operation(user_id, "delete_word_confirmed", word_to_delete)
                    reply_text = f"✅ ลบคำว่า '{word_to_delete}' เรียบร้อยแล้ว"
                    
                except Exception as e:
                    logger.error(f"Delete word error: {e}")
                    reply_text = f"⚠️ ลบคำว่า '{word_to_delete}' ไม่สำเร็จ: {str(e)[:100]}"
                
                # ลบ pending deletion
                del pending_deletions[user_id]
                
            else:
                # ขั้นตอนที่ 1: ระบุคำที่จะลบ
                parts = user_msg.split(":", 1)
                if len(parts) < 2:
                    reply_text = "❌ รูปแบบ: `ลบ: [คำศัพท์]`"
                else:
                    target_word = sanitize_word(parts[1].strip())
                    
                    if not target_word:
                        reply_text = "❌ กรุณาระบุคำศัพท์ที่ต้องการลบ"
                    else:
                        # ค้นหาคำศัพท์
                        response = supabase.table("vocab")\
                            .select("word, meaning, example_sentence")\
                            .ilike("word", f"%{target_word}%")\
                            .limit(5)\
                            .execute()
                        
                        found_words = response.data
                        
                        if not found_words:
                            reply_text = f"❌ ไม่พบคำว่า '{target_word}' ในคลังคำศัพท์"
                        elif len(found_words) == 1:
                            # พบ 1 คำ ให้ขอ confirm
                            word_info = found_words[0]
                            pending_deletions[user_id] = word_info['word']
                            
                            reply_text = (f"⚠️ ยืนยันการลบ:\n\n"
                                        f"📝 คำ: {word_info['word']}\n"
                                        f"📖 ความหมาย: {word_info.get('meaning', '-')}\n"
                                        f"🗣️ ตัวอย่าง: {word_info.get('example_sentence', '-')[:50]}...\n\n"
                                        f"พิมพ์ 'ยืนยัน' เพื่อลบ\n"
                                        f"พิมพ์อื่นๆ เพื่อยกเลิก")
                        else:
                            # พบหลายคำ
                            word_list = "\n".join([f"{i+1}. {w['word']} - {w.get('meaning', '')[:30]}..." 
                                                for i, w in enumerate(found_words)])
                            reply_text = (f"🔍 พบหลายคำที่คล้าย '{target_word}':\n\n"
                                        f"{word_list}\n\n"
                                        f"ระบุให้ชัดเจนกว่านี้ เช่น 'ลบ: {found_words[0]['word']}'")
                        
        except Exception as e:
            logger.error(f"Delete word process error: {e}")
            reply_text = "⚠️ มีปัญหาในการลบ กรุณาลองใหม่ภายหลัง"

    # === MENU 7: เพิ่มคำศัพท์ ===
    elif user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
            if not word:
                reply_text = "ใส่คำศัพท์หลัง : ด้วยนะครับ เช่น 'เพิ่ม: Resilience'"
            else:
                word = sanitize_word(word)
                
                # ตรวจสอบว่ามีอยู่แล้วหรือไม่
                existing = supabase.table("vocab")\
                    .select("*")\
                    .ilike("word", word)\
                    .execute()
                
                if existing.data:
                    reply_text = f"⚠️ คำว่า '{word}' มีอยู่แล้วในคลัง\nความหมาย: {existing.data[0].get('meaning', '-')}"
                else:
                    # Prompt ขอ JSON จาก Gemini
                    prompt = (f"I want to learn the English word '{word}'. "
                            f"Provide:\n"
                            f"1. Thai meaning (short and clear)\n"
                            f"2. 1 simple English example sentence\n\n"
                            f"Response in JSON format: "
                            f'{{"meaning": "Thai meaning here", "example": "Example sentence here"}}')
                    
                    res = model.generate_content(prompt)
                    
                    # Cleaning JSON string
                    clean_text = res.text.strip()
                    if "```json" in clean_text:
                        clean_text = clean_text.split("```json")[1].split("```")[0]
                    elif "```" in clean_text:
                        clean_text = clean_text.split("```")[1].split("```")[0]
                    
                    data = json.loads(clean_text)

                    meaning = data.get("meaning", "-")
                    example = data.get("example", "-")

                    # บันทึกลงฐานข้อมูล
                    result = supabase.table("vocab").insert({
                        "word": word, 
                        "meaning": meaning, 
                        "example_sentence": example,
                        "added_by": user_id,
                        "added_at": int(time.time())
                    }).execute()
                    
                    # ดึง ID ที่เพิ่งเพิ่ม
                    vocab_id = None
                    if result.data:
                        vocab_id = result.data[0].get('id')
                    
                    log_operation(user_id, "add_word", f"word:{word}, id:{vocab_id}")
                    reply_text = (f"✅ จดศัพท์ใหม่แล้ว!\n\n"
                                f"🔤 {word}\n"
                                f"📖 {meaning}\n"
                                f"🗣️ {example}")
                    
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            reply_text = "⚠️ AI ตอบกลับมาไม่ถูกรูปแบบ ลองใหม่อีกครั้งครับ"
        except Exception as e:
            logger.error(f"Add vocab error: {e}")
            reply_text = f"⚠️ มีปัญหากับระบบ: {str(e)[:100]}"

    # === MENU 8: ตรวจสอบสิทธิ์ ===
    elif user_msg in ["สิทธ์", "สิทธิ์", "สิทธิ", "role", "admin"]:
        # ตรวจสอบว่าเป็น admin หรือไม่ (ปรับ user_id ตามต้องการ)
        admin_users = ["U1234567890abcdef1234567890abcdef"]  # เปลี่ยนเป็น ID จริงของคุณ
        
        if user_id in admin_users:
            reply_text = "👑 คุณคือ Admin!\nสามารถใช้งานทุกคำสั่งได้"
        else:
            reply_text = "👤 คุณคือ User ปกติ\nสามารถใช้งานคำสั่งพื้นฐานได้"

    # === MENU 9: ตรวจสอบระบบ (สำหรับ Admin) ===
    elif user_msg == "ตรวจสอบระบบ":
        admin_users = ["U1234567890abcdef1234567890abcdef"]  # เปลี่ยนเป็น ID จริง
        
        if user_id in admin_users:
            try:
                # นับจำนวนคำศัพท์
                vocab_result = supabase.table("vocab").select("*", count="exact").execute()
                vocab_count = vocab_result.count or 0
                
                # นับผู้ใช้
                user_result = supabase.table("users").select("*", count="exact").execute()
                user_count = user_result.count or 0
                
                # นับคะแนน
                score_result = supabase.table("user_scores").select("*", count="exact").execute()
                score_count = score_result.count or 0
                
                # ตรวจสอบ sessions
                active_sessions = len(user_sessions)
                pending_deletions_count = len(pending_deletions)
                
                reply_text = (f"📊 สถิติระบบ:\n\n"
                            f"🗃️ คำศัพท์ทั้งหมด: {vocab_count} คำ\n"
                            f"👥 ผู้ใช้ทั้งหมด: {user_count} คน\n"
                            f"⭐ ผู้ใช้มีคะแนน: {score_count} คน\n"
                            f"🎮 เซสชั่นปัจจุบัน: {active_sessions}\n"
                            f"🗑️ รอการยืนยันลบ: {pending_deletions_count}\n"
                            f"⏰ เวลาปัจจุบัน: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception as e:
                logger.error(f"System check error: {e}")
                reply_text = f"⚠️ ตรวจสอบระบบผิดพลาด: {str(e)[:100]}"
        else:
            reply_text = "❌ คำสั่งนี้สำหรับ Admin เท่านั้น"

    # === MENU 10: ยกเลิก ===
    elif user_msg in ["ยกเลิก", "cancel", "stop"]:
        if user_id in pending_deletions:
            word = pending_deletions[user_id]
            del pending_deletions[user_id]
            reply_text = f"✅ ยกเลิกการลบคำว่า '{word}' แล้ว"
        elif user_id in user_sessions:
            word = user_sessions[user_id]['word']
            del user_sessions[user_id]
            reply_text = f"✅ ยกเลิกเกมคำว่า '{word}' แล้ว"
        else:
            reply_text = "🤔 ไม่มีอะไรให้ยกเลิกครับ"

    # === DEFAULT: ตรวจคำตอบ ===
    else:
        if user_id in pending_deletions:
            # ถ้ามี pending deletion แต่พิมพ์คำอื่น นั่นคือยกเลิก
            word = pending_deletions[user_id]
            del pending_deletions[user_id]
            reply_text = f"❌ ยกเลิกการลบคำว่า '{word}' เพราะคุณพิมพ์: '{user_msg}'\n\nพิมพ์ 'คำสั่ง' เพื่อดูเมนู"
            
        elif user_id not in user_sessions:
            reply_text = "🤔 อยากเล่นเกมพิมพ์ 'เริ่มเกม' ได้เลยครับ\nหรือพิมพ์ 'คำสั่ง' เพื่อดูเมนู"
        else:
            session = user_sessions[user_id]
            word = session['word']
            correct_meaning = session['meaning']
            vocab_id = session.get('vocab_id')
            
            try:
                # Prompt ชุดเดียว ได้ครบทุกอย่าง
                prompt = (f"User is learning vocabulary. Word: '{word}' (Correct meaning: {correct_meaning}).\n"
                         f"User answered: '{user_msg}'\n\n"
                         f"Analyze and respond with:\n"
                         f"1. is_correct: true/false (accept synonyms and similar meanings in Thai)\n"
                         f"2. reason_thai: short explanation in Thai (friendly tone)\n"
                         f"3. examples: 3 simple English example sentences\n\n"
                         f"Response in strict JSON format only:\n"
                         f'{{"is_correct": boolean, "reason_thai": "...", "examples": ["Ex1", "Ex2", "Ex3"]}}')
                
                res = model.generate_content(prompt)
                
                # Cleaning & Parsing
                clean_text = res.text.strip()
                if "```json" in clean_text:
                    clean_text = clean_text.split("```json")[1].split("```")[0]
                elif "```" in clean_text:
                    clean_text = clean_text.split("```")[1].split("```")[0]
                
                result = json.loads(clean_text)
                
                is_correct = result.get("is_correct", False)
                reason = result.get("reason_thai", "ไม่มีคำอธิบาย")
                examples = result.get("examples", [])
                
                # บันทึกประวัติการตอบ
                if vocab_id:
                    save_user_log(user_id, vocab_id, is_correct, user_msg)
                
                # จัด Format ตัวอย่างประโยค
                example_txt = "\n".join([f"• {ex}" for ex in examples]) if examples else "ไม่มีตัวอย่าง"

                # ล้าง Session
                del user_sessions[user_id]

                if is_correct:
                    # ✅ ถูกต้อง
                    new_score = update_score(user_id, 10)
                    mark_word_learned(user_id, word)
                    
                    reply_text = (f"🎉 สุดยอด! ถูกต้องครับ (+10 คะแนน)\n\n"
                                 f"💬 {reason}\n\n"
                                 f"📊 คะแนนรวม: {new_score}\n\n"
                                 f"🌟 ตัวอย่างการใช้:\n{example_txt}\n\n"
                                 f"👉 พิมพ์ 'เริ่มเกม' เพื่อลุยข้อต่อไป!")
                else:
                    # ❌ ผิด (เฉลยเลย)
                    new_score = update_score(user_id, -2)
                    
                    reply_text = (f"❌ ยังไม่ใช่นะครับ (-2 คะแนน)\n\n"
                                 f"📖 เฉลย: {word} แปลว่า \"{correct_meaning}\"\n"
                                 f"💡 คำแนะนำ: {reason}\n\n"
                                 f"🌟 ดูตัวอย่างประโยคช่วยจำ:\n{example_txt}\n\n"
                                 f"ไม่ต้องซีเรียสครับ พิมพ์ 'เริ่มเกม' ลองคำใหม่เลย!")
                
                log_operation(user_id, "check_answer", f"word:{word}, correct:{is_correct}, score_change:{'10' if is_correct else '-2'}")
            
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error in answer check: {e}")
                reply_text = f"⚠️ AI ตอบกลับมาไม่ถูกรูปแบบ\n\nเฉลย: {word} แปลว่า \"{correct_meaning}\"\n\nลองตอบใหม่อีกครั้ง!"
                # ไม่ลบ session ให้ลองใหม่
                if user_id not in user_sessions:
                    user_sessions[user_id] = session
            except Exception as e:
                logger.error(f"Check answer error: {e}")
                reply_text = f"😵‍💫 ระบบประมวลผลผิดพลาด\n\nเฉลย: {word} แปลว่า \"{correct_meaning}\"\n\nลองตอบใหม่อีกทีนะครับ"
                # ไม่ลบ session ให้ลองใหม่
                if user_id not in user_sessions:
                    user_sessions[user_id] = session

    # ส่งข้อความกลับ Line
    if reply_text:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            log_operation(user_id, "reply_sent", reply_text[:50])
        except Exception as e:
            logger.error(f"LINE Reply Error: {e}")
            print(f"LINE Reply Error: {e}")

# --- 5. ADDITIONAL ENDPOINTS ---
@app.get("/stats")
def get_stats():
    """Get system statistics"""
    try:
        vocab_count = supabase.table("vocab").select("*", count="exact").execute().count or 0
        user_count = supabase.table("users").select("*", count="exact").execute().count or 0
        score_count = supabase.table("user_scores").select("*", count="exact").execute().count or 0
        active_sessions = len(user_sessions)
        pending_deletions_count = len(pending_deletions)
        
        return {
            "status": "ok",
            "vocabulary_count": vocab_count,
            "user_count": user_count,
            "user_scores_count": score_count,
            "active_sessions": active_sessions,
            "pending_deletions": pending_deletions_count,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Get stats error: {e}")
        return {"status": "error", "detail": str(e)}

@app.get("/reset/{user_id}")
def reset_user(user_id: str):
    """Reset user data (for testing)"""
    try:
        # ลบ session
        if user_id in user_sessions:
            del user_sessions[user_id]
        
        if user_id in pending_deletions:
            del pending_deletions[user_id]
        
        return {"status": "ok", "message": f"Reset user {user_id}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/vocab/count")
def count_vocab():
    """Count vocabulary"""
    try:
        result = supabase.table("vocab").select("*", count="exact").execute()
        return {"count": result.count or 0}
    except Exception as e:
        return {"error": str(e)}

# Run the app
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)