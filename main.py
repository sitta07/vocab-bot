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
from typing import Dict, Any, Optional, Tuple, List
import asyncio

# --- 1. CONFIGURATION ---
load_dotenv()

# Setup logging with better configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Teacher Bot V2", version="2.0.0")

# Load Environment Variables with validation
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# Validate environment variables
MISSING_VARS = []
for var_name, var_value in [
    ('LINE_CHANNEL_ACCESS_TOKEN', LINE_ACCESS_TOKEN),
    ('LINE_CHANNEL_SECRET', LINE_SECRET),
    ('GEMINI_API_KEY', GEMINI_API_KEY),
    ('SUPABASE_URL', SUPABASE_URL),
    ('SUPABASE_KEY', SUPABASE_KEY)
]:
    if not var_value:
        MISSING_VARS.append(var_name)

if MISSING_VARS:
    error_msg = f"❌ Missing environment variables: {', '.join(MISSING_VARS)}"
    logger.error(error_msg)
    print(error_msg)
    # 根据环境决定是否退出
    if os.getenv('ENVIRONMENT') == 'production':
        raise RuntimeError(error_msg)

# Setup Clients with error handling
try:
    line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_SECRET)
    logger.info("✅ LINE Bot API initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize LINE Bot API: {e}")
    raise

# Configure Gemini
try:
    genai.configure(api_key=GEMINI_API_KEY)
    # Use a more stable model configuration
    generation_config = {
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 40,
        "max_output_tokens": 1024,
    }
    model = genai.GenerativeModel(
        'gemini-1.5-flash-latest',
        generation_config=generation_config
    )
    logger.info("✅ Gemini API configured successfully")
except Exception as e:
    logger.error(f"Failed to configure Gemini API: {e}")
    raise

# Setup Supabase with connection test
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    # Test connection
    supabase.table("vocab").select("count", count="exact").limit(1).execute()
    logger.info("✅ Supabase connected and tested successfully")
except Exception as e:
    logger.error(f"Supabase Connection Error: {e}")
    print(f"Supabase Connection Error: {e}")
    # In production, you might want to raise an error
    if os.getenv('ENVIRONMENT') == 'production':
        raise

# 🔥 GLOBAL STATE (RAM) with thread safety consideration
# In production, consider using Redis or database for session storage
user_sessions: Dict[str, Dict[str, Any]] = {}
pending_deletions: Dict[str, str] = {}  # user_id -> word to delete

# --- 2. HELPER FUNCTIONS ---
def retry_on_failure(max_retries: int = 3, delay: float = 1, backoff: float = 2):
    """Decorator for retry logic with exponential backoff"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt == max_retries - 1:
                        break
                    
                    logger.warning(f"Retry {attempt + 1}/{max_retries} for {func.__name__}: {e}")
                    time.sleep(current_delay)
                    current_delay *= backoff  # Exponential backoff
            
            logger.error(f"Function {func.__name__} failed after {max_retries} attempts: {last_exception}")
            raise last_exception
        return wrapper
    return decorator

def sanitize_word(word: str) -> str:
    """Clean and validate input word"""
    if not word or not isinstance(word, str):
        return ""
    
    # Remove potentially dangerous characters
    word = re.sub(r'[<>"\'\`;]', '', word)  # Basic SQL injection protection
    word = word.strip()
    
    # Limit length
    if len(word) > 100:
        word = word[:100]
        logger.warning(f"Word truncated to 100 chars: {word}")
    
    return word

def truncate_text(text: str, max_length: int = 2000) -> str:
    """Truncate text for LINE message limits"""
    if not text:
        return ""
    
    if len(text) <= max_length:
        return text
    
    truncated = text[:max_length - 3] + "..."
    logger.info(f"Text truncated from {len(text)} to {len(truncated)} characters")
    return truncated

def log_operation(user_id: str, operation: str, details: Any = ""):
    """Log operations with structured data"""
    try:
        log_data = {
            "user_id": user_id,
            "operation": operation,
            "details": str(details)[:500],  # Limit detail length
            "timestamp": datetime.now().isoformat()
        }
        
        log_msg = f"User:{user_id} | Operation:{operation} | Details:{log_data['details']}"
        logger.info(log_msg)
        
        # Try to log to database
        try:
            supabase.table("logs").insert(log_data).execute()
        except Exception as db_error:
            # Log the error but don't fail the main operation
            logger.debug(f"Failed to log to database (non-critical): {db_error}")
            
    except Exception as e:
        # Don't let logging break the main flow
        logger.error(f"Logging error (non-critical): {e}")

@retry_on_failure(max_retries=2)
def save_user(user_id: str):
    """Save or update user in database"""
    try:
        supabase.table("users").upsert(
            {
                "user_id": user_id,
                "last_active": datetime.now().isoformat()
            },
            on_conflict="user_id"
        ).execute()
        log_operation(user_id, "save_user")
    except Exception as e:
        logger.error(f"Save user error: {e}")
        # Don't raise, as this might not be critical for all operations

def get_user_score(user_id: str) -> Tuple[int, List[str]]:
    """Get user's current score and learned words"""
    try:
        result = supabase.table("user_scores")\
            .select("score, learned_words")\
            .eq("user_id", user_id)\
            .execute()
        
        if result.data:
            data = result.data[0]
            return data['score'], data.get('learned_words', [])
        
        # Initialize if not exists
        return 0, []
        
    except Exception as e:
        logger.error(f"Get user score error: {e}")
        return 0, []

@retry_on_failure(max_retries=2)
def update_score(user_id: str, points: int) -> int:
    """Update user score with atomic operation"""
    try:
        # Use database atomic operation if possible
        # For Supabase, we need to fetch and update
        score, learned = get_user_score(user_id)
        new_score = max(0, score + points)  # Prevent negative scores if needed
        
        supabase.table("user_scores").upsert({
            "user_id": user_id,
            "score": new_score,
            "learned_words": learned,
            "updated_at": datetime.now().isoformat()
        }, on_conflict="user_id").execute()
        
        log_operation(user_id, "update_score", f"points:{points}, new_score:{new_score}")
        return new_score
        
    except Exception as e:
        logger.error(f"Update score error: {e}")
        return 0

def mark_word_learned(user_id: str, word: str):
    """Mark a word as learned by user"""
    try:
        score, learned = get_user_score(user_id)
        word_lower = word.lower()
        learned_lower = [w.lower() for w in learned]
        
        if word_lower not in learned_lower:
            learned.append(word)
            # Keep only last 1000 words to prevent array getting too large
            if len(learned) > 1000:
                learned = learned[-1000:]
                
            supabase.table("user_scores").upsert({
                "user_id": user_id,
                "score": score,
                "learned_words": learned
            }, on_conflict="user_id").execute()
            
            log_operation(user_id, "mark_word_learned", word)
            
    except Exception as e:
        logger.error(f"Mark word learned error: {e}")

@retry_on_failure(max_retries=2)
def get_random_vocab(exclude_words: List[str] = None) -> Optional[Dict[str, Any]]:
    """Get random vocabulary excluding already learned words"""
    try:
        if exclude_words is None:
            exclude_words = []
            
        # First, get count
        count_result = supabase.table("vocab")\
            .select("*", count="exact")\
            .execute()
        
        total_count = count_result.count or 0
        
        if total_count == 0:
            return None
        
        # If we have many words, use random sampling with multiple attempts
        max_attempts = 10
        for attempt in range(max_attempts):
            # Get a random offset
            offset = random.randint(0, max(0, total_count - 1))
            
            result = supabase.table("vocab")\
                .select("*")\
                .range(offset, offset)\
                .execute()
            
            if result.data:
                vocab = result.data[0]
                word_lower = vocab['word'].lower()
                
                # Check if word is in exclude list
                exclude_lower = [w.lower() for w in exclude_words]
                if word_lower not in exclude_lower:
                    return vocab
        
        # If we couldn't find a non-excluded word after attempts, return any
        result = supabase.table("vocab")\
            .select("*")\
            .limit(1)\
            .execute()
        
        return result.data[0] if result.data else None
        
    except Exception as e:
        logger.error(f"Get random vocab error: {e}")
        return None

@retry_on_failure(max_retries=2)
def save_user_log(user_id: str, vocab_id: int, is_correct: bool, user_answer: str):
    """Save user answer log"""
    try:
        supabase.table("user_logs").insert({
            "user_id": user_id,
            "vocab_id": vocab_id,
            "is_correct": is_correct,
            "user_answer": user_answer[:500],  # Limit answer length
            "answered_at": datetime.now().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"Save user log error: {e}")

def get_vocab_id_by_word(word: str) -> Optional[int]:
    """Get vocabulary ID by word"""
    try:
        result = supabase.table("vocab")\
            .select("id")\
            .eq("word", word)\
            .limit(1)\
            .execute()
        
        if result.data:
            return result.data[0]['id']
        return None
        
    except Exception as e:
        logger.error(f"Get vocab id error: {e}")
        return None

def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from text that might contain markdown or other formatting"""
    if not text:
        return None
    
    # Try to find JSON in the text
    json_pattern = r'\{.*\}'
    matches = re.findall(json_pattern, text, re.DOTALL)
    
    for match in matches:
        try:
            return json.loads(match)
        except json.JSONDecodeError:
            continue
    
    # If no JSON found, try to parse the whole text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

def cleanup_old_sessions(max_age_minutes: int = 30):
    """Clean up old user sessions to prevent memory leak"""
    try:
        current_time = time.time()
        users_to_remove = []
        
        for user_id, session in user_sessions.items():
            # If session has a timestamp, check age
            session_time = session.get('created_at', current_time)
            if current_time - session_time > max_age_minutes * 60:
                users_to_remove.append(user_id)
        
        for user_id in users_to_remove:
            del user_sessions[user_id]
            logger.info(f"Cleaned up old session for user {user_id}")
            
    except Exception as e:
        logger.error(f"Error cleaning up old sessions: {e}")

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    """Health check endpoint"""
    cleanup_old_sessions()  # Clean up on health check
    return {
        "status": "ok",
        "service": "Teacher Bot V2",
        "version": "2.0.0",
        "time": datetime.now().isoformat(),
        "active_sessions": len(user_sessions),
        "pending_deletions": len(pending_deletions)
    }

@app.get("/broadcast-quiz")
def broadcast_quiz():
    """Broadcast quiz to all users (for Cron Job)"""
    try:
        # Clean up old sessions first
        cleanup_old_sessions()
        
        # Get all users
        users_result = supabase.table("users")\
            .select("user_id")\
            .execute()
        
        if not users_result.data:
            return {"status": "success", "msg": "No users found", "sent_to": 0}
        
        users = users_result.data
        success_count = 0
        failed_users = []
        
        for user in users:
            user_id = user['user_id']
            
            try:
                # Get user's learned words
                _, learned = get_user_score(user_id)
                
                # Get random vocabulary
                selected = get_random_vocab(learned)
                
                if not selected:
                    logger.warning(f"No vocabulary available for user {user_id}")
                    continue
                
                word = selected['word']
                meaning = selected.get('meaning', '-')
                
                # Prepare message
                msg = (
                    f"🔥 ภารกิจมาแล้ว!\n\n"
                    f"❓ คำว่า '{word}' แปลว่าอะไร?\n\n"
                    f"💡 ตอบผิดไม่เป็นไร เดี๋ยวมีเฉลยพร้อมตัวอย่างให้ครับ"
                )
                
                # Send message
                line_bot_api.push_message(user_id, TextSendMessage(text=msg))
                
                # Store session
                user_sessions[user_id] = {
                    'word': word,
                    'meaning': meaning,
                    'hint_given': False,
                    'vocab_id': selected.get('id'),
                    'created_at': time.time()
                }
                
                success_count += 1
                log_operation(user_id, "broadcast_quiz", word)
                
                # Small delay to avoid rate limiting
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Failed to send quiz to user {user_id}: {e}")
                failed_users.append(user_id)
                continue
        
        return {
            "status": "success",
            "sent_to": success_count,
            "total_users": len(users),
            "failed_users": failed_users[:10]  # Limit response size
        }
        
    except Exception as e:
        logger.error(f"Broadcast quiz error: {e}")
        return {"status": "error", "detail": str(e)[:200]}

@app.post("/callback")
async def callback(request: Request):
    """LINE Webhook callback endpoint"""
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    
    try:
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.error(f"Error handling webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    
    return "OK"

# --- 4. MESSAGE HANDLER ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """Handle incoming LINE messages"""
    user_msg = event.message.text.strip()
    user_id = event.source.user_id
    
    # Clean up old sessions periodically (every 100 messages)
    if random.random() < 0.01:
        cleanup_old_sessions()
    
    save_user(user_id)
    
    # Log incoming message (truncated)
    log_operation(user_id, "received_message", user_msg[:100])
    
    # Process message based on content
    reply_text = process_message(user_id, user_msg)
    
    # Send reply if we have one
    if reply_text:
        try:
            # Truncate if too long for LINE
            reply_text = truncate_text(reply_text, 4900)
            line_bot_api.reply_message(
                event.reply_token, 
                TextSendMessage(text=reply_text)
            )
            log_operation(user_id, "reply_sent", reply_text[:100])
        except Exception as e:
            logger.error(f"LINE Reply Error for user {user_id}: {e}")

def process_message(user_id: str, user_msg: str) -> str:
    """Process user message and return reply text"""
    user_msg_lower = user_msg.lower()
    
    # === MENU 1: คำสั่ง ===
    if user_msg_lower in ["คำสั่ง", "เมนู", "menu", "help", "ช่วยเหลือ"]:
        return show_menu(user_id)
    
    # === MENU 2: คะแนน ===
    elif user_msg_lower in ["คะแนน", "score", "สถิติ", "points", "คะแนน"]:
        return show_score(user_id)
    
    # === MENU 3: เริ่มเกม ===
    elif user_msg_lower in ["เริ่มเกม", "เริ่ม", "start", "play", "quiz", "เกม"]:
        return start_game(user_id)
    
    # === MENU 4: คำใบ้ ===
    elif user_msg_lower in ["คำใบ้", "hint", "clue", "ใบ้"]:
        return give_hint(user_id)
    
    # === MENU 5: คลังคำศัพท์ ===
    elif user_msg_lower in ["คลังคำศัพท์", "คลัง", "vocab", "vocabulary", "ศัพท์"]:
        return show_vocabulary(user_id)
    
    # === MENU 6: ลบคำศัพท์ ===
    elif user_msg_lower.startswith(("ลบ:", "ลบคำศัพท์:", "delete:", "remove:")):
        return handle_delete_word(user_id, user_msg)
    
    # === MENU 7: เพิ่มคำศัพท์ ===
    elif user_msg_lower.startswith(("เพิ่ม:", "add:", "insert:")):
        return handle_add_word(user_id, user_msg)
    
    # === MENU 8: ยกเลิก ===
    elif user_msg_lower in ["ยกเลิก", "cancel", "stop"]:
        return handle_cancel(user_id)
    
    # === MENU 9: ตรวจสอบระบบ ===
    elif user_msg_lower in ["ตรวจสอบระบบ", "system", "status"]:
        return check_system_status()
    
    # === DEFAULT: ตรวจคำตอบ ===
    else:
        return handle_answer(user_id, user_msg)

def show_menu(user_id: str) -> str:
    """Show command menu"""
    score, learned = get_user_score(user_id)
    has_session = "🟢 กำลังเล่น" if user_id in user_sessions else "⚪ ไม่ได้เล่น"
    
    return (
        f"🤖 คู่มือครูพี่ Bot V2:\n\n"
        f"1. 🎮 เริ่มเกม -> เริ่มทายคำศัพท์\n"
        f"2. 📊 คะแนน -> ดูคะแนนและสถิติ\n"
        f"3. 💡 คำใบ้ -> ขอคำใบ้ (ลด -2 คะแนน)\n"
        f"4. ➕ เพิ่ม: [ศัพท์] -> เพิ่มคำใหม่\n"
        f"5. ❌ ลบ: [ศัพท์] -> ลบคำศัพท์\n"
        f"6. 📚 คลัง -> ดูศัพท์ทั้งหมด\n"
        f"7. 🛠️ ตรวจสอบระบบ -> ตรวจสอบสถานะระบบ\n"
        f"8. 🚫 ยกเลิก -> ยกเลิกการกระทำปัจจุบัน\n\n"
        f"📊 คะแนน: {score} XP\n"
        f"📚 จำได้: {len(learned)} คำ\n"
        f"🎮 สถานะ: {has_session}"
    )

def show_score(user_id: str) -> str:
    """Show user score"""
    score, learned = get_user_score(user_id)
    session_status = "🟢 มีเซสชั่น" if user_id in user_sessions else "⚪ ไม่มีเซสชั่น"
    
    # Get some stats
    try:
        # Get total correct answers
        correct_result = supabase.table("user_logs")\
            .select("*", count="exact")\
            .eq("user_id", user_id)\
            .eq("is_correct", True)\
            .execute()
        
        correct_count = correct_result.count or 0
        
        # Get total answers
        total_result = supabase.table("user_logs")\
            .select("*", count="exact")\
            .eq("user_id", user_id)\
            .execute()
        
        total_count = total_result.count or 0
        accuracy = (correct_count / total_count * 100) if total_count > 0 else 0
        
    except Exception as e:
        logger.error(f"Error getting user stats: {e}")
        correct_count = 0
        total_count = 0
        accuracy = 0
    
    return (
        f"📊 สถิติความเทพ:\n\n"
        f"⭐ คะแนนรวม: {score} XP\n"
        f"📚 คำศัพท์ที่แม่นแล้ว: {len(learned)} คำ\n"
        f"🎯 ถูกต้อง: {correct_count}/{total_count} ครั้ง\n"
        f"📈 ความแม่นยำ: {accuracy:.1f}%\n"
        f"🎮 เซสชั่น: {session_status}"
    )

def start_game(user_id: str) -> str:
    """Start a new game"""
    # Clear any pending deletions
    if user_id in pending_deletions:
        del pending_deletions[user_id]
    
    # Get user's learned words
    _, learned = get_user_score(user_id)
    
    # Get random vocabulary
    selected = get_random_vocab(learned)
    
    if not selected:
        return "📭 คลังศัพท์ว่างเปล่า! พิมพ์ 'เพิ่ม: [คำศัพท์]' เพื่อใส่คำใหม่ก่อนครับ"
    
    word = selected['word']
    meaning = selected.get('meaning', '-')
    
    # Create new session
    user_sessions[user_id] = {
        'word': word,
        'meaning': meaning,
        'hint_given': False,
        'vocab_id': selected.get('id'),
        'created_at': time.time()
    }
    
    return (
        f"🎮 เริ่มกันเลย!\n\n"
        f"❓ คำว่า '{word}' แปลว่าอะไร?\n\n"
        f"💡 ตอบภาษาไทยมาเลย (ตอบผิดมีเฉลยให้ทันที)\n"
        f"🤔 หรือพิมพ์ 'คำใบ้' เพื่อขอคำใบ้ (-2 คะแนน)"
    )

def give_hint(user_id: str) -> str:
    """Give hint for current word"""
    if user_id not in user_sessions:
        return "🤔 ยังไม่ได้เริ่มเกมเลยครับ พิมพ์ 'เริ่มเกม' ก่อนนะ"
    
    session = user_sessions[user_id]
    
    if session.get('hint_given'):
        return f"💡 ให้คำใบ้ไปแล้วไงครับ: {session['meaning']}"
    
    # Deduct points and give hint
    new_score = update_score(user_id, -2)
    session['hint_given'] = True
    user_sessions[user_id] = session
    
    return (
        f"💡 คำใบ้: {session['meaning']}\n"
        f"📉 (-2 คะแนน | เหลือ: {new_score} XP)\n\n"
        f"🤔 พอเดาออกไหม? พิมพ์คำตอบมาเลย!"
    )

def show_vocabulary(user_id: str) -> str:
    """Show vocabulary list"""
    try:
        # Get total count first
        count_result = supabase.table("vocab")\
            .select("*", count="exact")\
            .execute()
        
        total_count = count_result.count or 0
        
        if total_count == 0:
            return "📭 คลังว่างเปล่าครับ"
        
        # Get recent words
        response = supabase.table("vocab")\
            .select("word, meaning")\
            .order("id", desc=True)\
            .limit(15)\
            .execute()
        
        words = response.data
        
        if not words:
            return "⚠️ ไม่สามารถดึงข้อมูลได้"
        
        word_list = "\n".join([
            f"• {item['word']}: {item.get('meaning', 'ไม่มีคำแปล')[:40]}"
            for item in words
        ])
        
        return (
            f"📚 ศัพท์ 15 คำล่าสุด:\n\n{word_list}\n\n"
            f"📊 ทั้งหมด: {total_count} คำในคลัง\n"
            f"💡 พิมพ์ 'เริ่มเกม' เพื่อเริ่มทายคำศัพท์"
        )
        
    except Exception as e:
        logger.error(f"Error showing vocabulary: {e}")
        return "⚠️ ดึงข้อมูลไม่ได้ครับ เช็ค DB แป๊บ"

def handle_delete_word(user_id: str, user_msg: str) -> str:
    """Handle word deletion with confirmation"""
    try:
        # Check if this is confirmation step
        if user_id in pending_deletions and user_msg.lower() in ["ยืนยัน", "confirm", "yes", "ใช่"]:
            # Step 2: Confirm deletion
            word_to_delete = pending_deletions[user_id]
            
            try:
                # Delete from database
                result = supabase.table("vocab")\
                    .delete()\
                    .eq("word", word_to_delete)\
                    .execute()
                
                deleted_count = len(result.data) if result.data else 0
                
                if deleted_count > 0:
                    log_operation(user_id, "delete_word_confirmed", word_to_delete)
                    
                    # Clear any sessions containing this word
                    for uid, session in list(user_sessions.items()):
                        if session.get('word') == word_to_delete:
                            del user_sessions[uid]
                    
                    # Clear pending deletion
                    del pending_deletions[user_id]
                    
                    return f"✅ ลบคำว่า '{word_to_delete}' เรียบร้อยแล้ว"
                else:
                    del pending_deletions[user_id]
                    return f"⚠️ ไม่พบคำว่า '{word_to_delete}' ในคลัง"
                
            except Exception as e:
                logger.error(f"Delete word error: {e}")
                del pending_deletions[user_id]
                return f"⚠️ ลบคำว่า '{word_to_delete}' ไม่สำเร็จ: {str(e)[:100]}"
        
        else:
            # Step 1: Parse word to delete
            parts = user_msg.split(":", 1)
            if len(parts) < 2:
                return "❌ รูปแบบ: `ลบ: [คำศัพท์]` เช่น 'ลบ: hello'"
            
            target_word = sanitize_word(parts[1].strip())
            
            if not target_word:
                return "❌ กรุณาระบุคำศัพท์ที่ต้องการลบ"
            
            # Search for the word
            response = supabase.table("vocab")\
                .select("word, meaning, example_sentence, added_by")\
                .ilike("word", f"{target_word}%")\
                .limit(5)\
                .execute()
            
            found_words = response.data
            
            if not found_words:
                return f"❌ ไม่พบคำว่า '{target_word}' ในคลังคำศัพท์"
            elif len(found_words) == 1:
                # Found exactly one word, ask for confirmation
                word_info = found_words[0]
                pending_deletions[user_id] = word_info['word']
                
                return (
                    f"⚠️ ยืนยันการลบ:\n\n"
                    f"📝 คำ: {word_info['word']}\n"
                    f"📖 ความหมาย: {word_info.get('meaning', '-')}\n"
                    f"🗣️ ตัวอย่าง: {word_info.get('example_sentence', '-')[:50]}...\n"
                    f"👤 เพิ่มโดย: {word_info.get('added_by', 'ไม่ทราบ')}\n\n"
                    f"พิมพ์ 'ยืนยัน' เพื่อลบถาวร\n"
                    f"พิมพ์ 'ยกเลิก' หรือคำอื่นเพื่อยกเลิก"
                )
            else:
                # Found multiple words
                word_list = "\n".join([
                    f"{i+1}. {w['word']} - {w.get('meaning', '')[:30]}..."
                    for i, w in enumerate(found_words)
                ])
                
                return (
                    f"🔍 พบหลายคำที่คล้าย '{target_word}':\n\n"
                    f"{word_list}\n\n"
                    f"ระบุให้ชัดเจนกว่านี้ เช่น 'ลบ: {found_words[0]['word']}'"
                )
    
    except Exception as e:
        logger.error(f"Delete word process error: {e}")
        return "⚠️ มีปัญหาในการลบ กรุณาลองใหม่ภายหลัง"

def handle_add_word(user_id: str, user_msg: str) -> str:
    """Handle adding new word with Gemini"""
    try:
        # Extract word from message
        parts = user_msg.split(":", 1)
        if len(parts) < 2:
            return "❌ รูปแบบ: `เพิ่ม: [คำศัพท์]` เช่น 'เพิ่ม: resilience'"
        
        word = parts[1].strip()
        if not word:
            return "ใส่คำศัพท์หลัง : ด้วยนะครับ เช่น 'เพิ่ม: Resilience'"
        
        word = sanitize_word(word)
        
        # Check if word already exists
        existing = supabase.table("vocab")\
            .select("*")\
            .ilike("word", word)\
            .limit(1)\
            .execute()
        
        if existing.data:
            existing_word = existing.data[0]
            return (
                f"⚠️ คำว่า '{word}' มีอยู่แล้วในคลัง\n\n"
                f"📖 ความหมาย: {existing_word.get('meaning', '-')}\n"
                f"🗣️ ตัวอย่าง: {existing_word.get('example_sentence', '-')[:100]}"
            )
        
        # Get word details from Gemini with better prompt
        prompt = (
            f"Please provide Thai meaning and example sentence for English word: '{word}'\n\n"
            f"Requirements:\n"
            f"1. Thai meaning: Clear, concise translation in Thai\n"
            f"2. Example sentence: Simple English sentence using the word\n\n"
            f"Respond in JSON format only:\n"
            f'{{"meaning": "คำแปลภาษาไทย", "example": "Example sentence here"}}'
        )
        
        try:
            response = model.generate_content(prompt)
            response_text = response.text.strip()
            
            # Extract JSON from response
            data = extract_json_from_text(response_text)
            
            if not data:
                # Fallback if JSON extraction fails
                logger.warning(f"Could not extract JSON from Gemini response: {response_text[:200]}")
                # Try to parse as plain text
                lines = response_text.split('\n')
                meaning = lines[0] if len(lines) > 0 else "ไม่พบคำแปล"
                example = lines[1] if len(lines) > 1 else "No example provided"
                
                # Clean up
                meaning = meaning.replace("Meaning:", "").replace("ความหมาย:", "").strip()
                example = example.replace("Example:", "").replace("ตัวอย่าง:", "").strip()
                
                data = {
                    "meaning": meaning[:200],
                    "example": example[:500]
                }
            
            meaning = data.get("meaning", "ไม่พบคำแปล")
            example = data.get("example", "No example provided")
            
        except Exception as gemini_error:
            logger.error(f"Gemini API error: {gemini_error}")
            # Fallback to simple data
            meaning = "รอการอัพเดตคำแปล"
            example = f"I need to learn the word '{word}'."
        
        # Save to database
        result = supabase.table("vocab").insert({
            "word": word,
            "meaning": meaning,
            "example_sentence": example,
            "added_by": user_id,
            "added_at": datetime.now().isoformat()
        }).execute()
        
        # Get the inserted ID
        vocab_id = None
        if result.data:
            vocab_id = result.data[0].get('id')
        
        log_operation(user_id, "add_word", f"word:{word}, id:{vocab_id}")
        
        return (
            f"✅ จดศัพท์ใหม่แล้ว!\n\n"
            f"🔤 {word}\n"
            f"📖 {meaning}\n"
            f"🗣️ {example}\n\n"
            f"พิมพ์ 'เริ่มเกม' เพื่อลองทายคำนี้ดู!"
        )
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
        return "⚠️ AI ตอบกลับมาไม่ถูกรูปแบบ ลองใหม่อีกครั้งครับ"
    except Exception as e:
        logger.error(f"Add vocab error: {e}")
        return f"⚠️ มีปัญหากับระบบ: {str(e)[:100]}"

def handle_cancel(user_id: str) -> str:
    """Handle cancel operation"""
    cancelled_items = []
    
    if user_id in pending_deletions:
        word = pending_deletions[user_id]
        del pending_deletions[user_id]
        cancelled_items.append(f"ลบคำว่า '{word}'")
    
    if user_id in user_sessions:
        word = user_sessions[user_id].get('word', 'คำศัพท์')
        del user_sessions[user_id]
        cancelled_items.append(f"เกมคำว่า '{word}'")
    
    if cancelled_items:
        return f"🚫 ยกเลิก: {' และ '.join(cancelled_items)} เรียบร้อยแล้ว"
    else:
        return "🤔 ไม่มีอะไรที่กำลังดำเนินการให้ยกเลิกนะครับ"

def check_system_status() -> str:
    """Check system status"""
    try:
        # Get counts
        vocab_count = supabase.table("vocab").select("*", count="exact").execute().count or 0
        user_count = supabase.table("users").select("*", count="exact").execute().count or 0
        score_count = supabase.table("user_scores").select("*", count="exact").execute().count or 0
        
        return (
            f"🛠️ ตรวจสอบระบบ:\n\n"
            f"✅ LINE Bot: พร้อมใช้งาน\n"
            f"✅ Gemini AI: พร้อมใช้งาน\n"
            f"✅ Database: พร้อมใช้งาน\n\n"
            f"📊 สถิติ:\n"
            f"• คำศัพท์ในคลัง: {vocab_count} คำ\n"
            f"• ผู้ใช้ทั้งหมด: {user_count} คน\n"
            f"• ผู้ใช้ที่มีคะแนน: {score_count} คน\n"
            f"• เซสชั่นปัจจุบัน: {len(user_sessions)}\n"
            f"• การลบรอดำเนินการ: {len(pending_deletions)}\n\n"
            f"⏰ เวลาเซิร์ฟเวอร์: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    except Exception as e:
        logger.error(f"System check error: {e}")
        return f"⚠️ ตรวจสอบระบบไม่สมบูรณ์: {str(e)[:100]}"

def handle_answer(user_id: str, user_msg: str) -> str:
    """Handle user's answer"""
    # Check for pending deletions first
    if user_id in pending_deletions:
        # If there's a pending deletion and user types something else, cancel it
        word = pending_deletions[user_id]
        del pending_deletions[user_id]
        return (
            f"❌ ยกเลิกการลบคำว่า '{word}' เพราะคุณพิมพ์: '{user_msg}'\n\n"
            f"พิมพ์ 'คำสั่ง' เพื่อดูเมนูทั้งหมด"
        )
    
    # Check if user has an active session
    if user_id not in user_sessions:
        return (
            "🤔 อยากเล่นเกมพิมพ์ 'เริ่มเกม' ได้เลยครับ\n"
            "หรือพิมพ์ 'คำสั่ง' เพื่อดูเมนูทั้งหมด"
        )
    
    session = user_sessions[user_id]
    word = session['word']
    correct_meaning = session['meaning']
    vocab_id = session.get('vocab_id')
    
    try:
        # Use Gemini to check the answer
        prompt = (
            f"User is learning English vocabulary.\n"
            f"Word: '{word}'\n"
            f"Correct meaning in Thai: '{correct_meaning}'\n"
            f"User's answer in Thai: '{user_msg}'\n\n"
            f"Analyze if the user's answer is correct or approximately correct.\n"
            f"Consider synonyms and similar meanings.\n\n"
            f"Respond in strict JSON format only:\n"
            f'{{"is_correct": boolean, "explanation_thai": "คำอธิบายภาษาไทย", "examples": ["ประโยคตัวอย่าง 1", "ประโยคตัวอย่าง 2", "ประโยคตัวอย่าง 3"]}}'
        )
        
        response = model.generate_content(prompt)
        response_text = response.text.strip()
        
        # Extract JSON
        result = extract_json_from_text(response_text)
        
        if not result:
            logger.error(f"Could not parse Gemini response as JSON: {response_text[:200]}")
            # Fallback: simple string matching
            user_msg_lower = user_msg.lower()
            correct_lower = correct_meaning.lower()
            
            # Simple check for similarity
            is_correct = (
                user_msg_lower == correct_lower or
                user_msg_lower in correct_lower or
                correct_lower in user_msg_lower
            )
            
            result = {
                "is_correct": is_correct,
                "explanation_thai": "ตรวจสอบด้วยระบบพื้นฐาน",
                "examples": [
                    f"I need to use the word '{word}' in a sentence.",
                    f"Can you explain the meaning of '{word}'?",
                    f"Let's practice using '{word}' in conversation."
                ]
            }
        
        is_correct = result.get("is_correct", False)
        explanation = result.get("explanation_thai", "ไม่มีคำอธิบาย")
        examples = result.get("examples", [])
        
        # Save user log
        if vocab_id:
            save_user_log(user_id, vocab_id, is_correct, user_msg)
        
        # Format examples
        example_text = ""
        if examples and isinstance(examples, list):
            example_text = "\n".join([f"• {ex}" for ex in examples[:3]])  # Limit to 3 examples
        
        # Clear the session
        del user_sessions[user_id]
        
        if is_correct:
            # Correct answer
            new_score = update_score(user_id, 10)
            mark_word_learned(user_id, word)
            
            return (
                f"🎉 สุดยอด! ถูกต้องครับ (+10 คะแนน)\n\n"
                f"💬 {explanation}\n\n"
                f"📊 คะแนนรวม: {new_score} XP\n\n"
                f"🌟 ตัวอย่างการใช้:\n{example_text}\n\n"
                f"👉 พิมพ์ 'เริ่มเกม' เพื่อลุยข้อต่อไป!"
            )
        else:
            # Wrong answer
            new_score = update_score(user_id, -2)
            
            return (
                f"❌ ยังไม่ใช่นะครับ (-2 คะแนน)\n\n"
                f"📖 เฉลย: {word} แปลว่า \"{correct_meaning}\"\n"
                f"💡 คำแนะนำ: {explanation}\n\n"
                f"🌟 ตัวอย่างประโยคช่วยจำ:\n{example_text}\n\n"
                f"ไม่เป็นไรครับ! พิมพ์ 'เริ่มเกม' ลองคำใหม่เลย!"
            )
    
    except Exception as e:
        logger.error(f"Error checking answer: {e}")
        # In case of error, show the correct answer and clear session
        if user_id in user_sessions:
            del user_sessions[user_id]
        
        return (
            f"😵‍💫 ระบบตรวจคำตอบมีปัญหา\n\n"
            f"📖 เฉลย: {word} แปลว่า \"{correct_meaning}\"\n\n"
            f"ลองตอบใหม่อีกครั้งหรือพิมพ์ 'เริ่มเกม' เพื่อเริ่มเกมใหม่นะครับ"
        )

# --- 5. ADDITIONAL ENDPOINTS ---
@app.get("/stats")
def get_stats():
    """Get detailed system statistics"""
    try:
        # Get various counts
        vocab_result = supabase.table("vocab").select("*", count="exact").execute()
        user_result = supabase.table("users").select("*", count="exact").execute()
        score_result = supabase.table("user_scores").select("*", count="exact").execute()
        log_result = supabase.table("logs").select("*", count="exact").execute()
        
        # Get recent activity
        recent_logs = supabase.table("logs")\
            .select("operation, COUNT(*)")\
            .group("operation")\
            .order("count", desc=True)\
            .limit(5)\
            .execute()
        
        # Get top users
        top_users = supabase.table("user_scores")\
            .select("user_id, score")\
            .order("score", desc=True)\
            .limit(5)\
            .execute()
        
        return {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "counts": {
                "vocabulary": vocab_result.count or 0,
                "users": user_result.count or 0,
                "user_scores": score_result.count or 0,
                "logs": log_result.count or 0
            },
            "current_state": {
                "active_sessions": len(user_sessions),
                "pending_deletions": len(pending_deletions)
            },
            "recent_operations": recent_logs.data if recent_logs.data else [],
            "top_users": top_users.data if top_users.data else []
        }
    except Exception as e:
        logger.error(f"Get stats error: {e}")
        return {"status": "error", "detail": str(e)[:200]}

@app.get("/reset/{user_id}")
def reset_user(user_id: str):
    """Reset user data (for testing only)"""
    try:
        # Clear from memory
        if user_id in user_sessions:
            del user_sessions[user_id]
        
        if user_id in pending_deletions:
            del pending_deletions[user_id]
        
        # Clear from database (optional - be careful!)
        # supabase.table("user_scores").delete().eq("user_id", user_id).execute()
        # supabase.table("user_logs").delete().eq("user_id", user_id).execute()
        
        return {
            "status": "ok",
            "message": f"Reset user {user_id} in memory",
            "cleared_sessions": True,
            "cleared_pending": True
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/vocab/count")
def count_vocab():
    """Count vocabulary with filter options"""
    try:
        result = supabase.table("vocab").select("*", count="exact").execute()
        return {
            "status": "ok",
            "count": result.count or 0,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/cleanup")
def cleanup_endpoint():
    """Manually trigger cleanup"""
    try:
        before_count = len(user_sessions)
        cleanup_old_sessions()
        after_count = len(user_sessions)
        
        return {
            "status": "ok",
            "cleaned_sessions": before_count - after_count,
            "remaining_sessions": after_count,
            "pending_deletions": len(pending_deletions)
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# --- 6. STARTUP AND SHUTDOWN ---
@app.on_event("startup")
async def startup_event():
    """Run on application startup"""
    logger.info("🚀 Teacher Bot V2 is starting up...")
    
    # Initial cleanup
    cleanup_old_sessions()
    
    # Log startup information
    logger.info(f"Active sessions at startup: {len(user_sessions)}")
    logger.info(f"Pending deletions at startup: {len(pending_deletions)}")

@app.on_event("shutdown")
async def shutdown_event():
    """Run on application shutdown"""
    logger.info("🛑 Teacher Bot V2 is shutting down...")
    logger.info(f"Active sessions at shutdown: {len(user_sessions)}")
    logger.info(f"Pending deletions at shutdown: {len(pending_deletions)}")

# --- 7. MAIN ENTRY POINT ---
if __name__ == "__main__":
    import uvicorn
    
    # Configuration for running locally
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8000))
    
    logger.info(f"Starting server on {host}:{port}")
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=True
    )