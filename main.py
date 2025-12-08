# vocab-flashcard-bot-fixed.py
import os
import random
import json
from datetime import datetime
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

# 🔥 FLASHCARD STATE (RAM)
user_flashcards = {}

# 🔥 DEFAULT VOCABULARY LIST
DEFAULT_WORDS = [
    {
        "word": "learn",
        "meaning": "เรียนรู้",
        "example_sentence": "I want to learn English."
    },
    {
        "word": "study", 
        "meaning": "ศึกษา",
        "example_sentence": "He studies at university."
    },
    {
        "word": "practice",
        "meaning": "ฝึกฝน", 
        "example_sentence": "Practice makes perfect."
    },
    {
        "word": "happy",
        "meaning": "มีความสุข",
        "example_sentence": "I am very happy today."
    },
    {
        "word": "friend",
        "meaning": "เพื่อน",
        "example_sentence": "He is my best friend."
    },
    {
        "word": "book",
        "meaning": "หนังสือ",
        "example_sentence": "This is an interesting book."
    },
    {
        "word": "water",
        "meaning": "น้ำ",
        "example_sentence": "Drink more water."
    },
    {
        "word": "time",
        "meaning": "เวลา",
        "example_sentence": "Time is valuable."
    },
    {
        "word": "home",
        "meaning": "บ้าน",
        "example_sentence": "I will go home soon."
    },
    {
        "word": "food",
        "meaning": "อาหาร",
        "example_sentence": "Thai food is delicious."
    }
]

# --- 2. HELPER FUNCTIONS ---
def save_user(user_id):
    """เก็บ User ID ลง DB"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except: 
        pass

def init_vocab_database():
    """ตรวจสอบและเพิ่มคำศัพท์พื้นฐานลงในฐานข้อมูล"""
    try:
        # ตรวจสอบว่ามีข้อมูลในตาราง vocab หรือไม่
        result = supabase.table("vocab").select("count", count="exact").execute()
        
        if result.count == 0:
            print("⚠️ ตาราง vocab ว่างเปล่า กำลังเพิ่มคำศัพท์พื้นฐาน...")
            
            # เพิ่มข้อมูลคำศัพท์พื้นฐาน
            for word_data in DEFAULT_WORDS:
                try:
                    supabase.table("vocab").upsert({
                        "word": word_data["word"],
                        "meaning": word_data["meaning"],
                        "example_sentence": word_data["example_sentence"]
                    }, on_conflict="word").execute()
                except Exception as e:
                    print(f"Error adding word {word_data['word']}: {e}")
            
            print(f"✅ เพิ่มคำศัพท์พื้นฐาน {len(DEFAULT_WORDS)} คำเรียบร้อยแล้ว")
        else:
            print(f"✅ ตาราง vocab มีคำศัพท์อยู่แล้ว: {result.count} คำ")
    except Exception as e:
        print(f"Init vocab database error: {e}")

def get_user_vocab_scores(user_id):
    """ดึงคะแนนคำศัพท์ของผู้ใช้จากตาราง user_scores"""
    try:
        result = supabase.table("user_scores").select("*").eq("user_id", user_id).execute()
        
        if not result.data:
            return {}
        
        user_data = result.data[0]
        
        if 'vocab_stats' in user_data and user_data['vocab_stats']:
            return user_data['vocab_stats']
        else:
            return {}
            
    except Exception as e:
        print(f"Get vocab scores error: {e}")
        return {}

def update_vocab_score(user_id, word, answer_is_yes):
    """อัพเดทคะแนนคำศัพท์เมื่อผู้ใช้ตอบ Yes/No"""
    try:
        # ดึงข้อมูลปัจจุบัน
        result = supabase.table("user_scores").select("*").eq("user_id", user_id).execute()
        
        if not result.data:
            # ถ้ายังไม่มีข้อมูลผู้ใช้
            vocab_stats = {}
            score = 0
            learned_words = []
        else:
            user_data = result.data[0]
            vocab_stats = user_data.get('vocab_stats', {})
            score = user_data.get('score', 0)
            learned_words = user_data.get('learned_words', [])
        
        # อัพเดทคะแนนสำหรับคำนี้
        if word not in vocab_stats:
            vocab_stats[word] = {
                'yes': 0,
                'no': 0,
                'difficulty': 0,
                'last_reviewed': datetime.now().isoformat()
            }
        
        current = vocab_stats[word]
        
        if answer_is_yes:
            current['yes'] = current.get('yes', 0) + 1
            # เพิ่มคะแนนรวมเมื่อตอบถูก
            score += 10
            # บันทึกว่าเรียนคำนี้แล้ว
            if word not in learned_words:
                learned_words.append(word)
            # ลดความยากเมื่อตอบถูกบ่อย
            if current['yes'] >= 3 and current.get('difficulty', 0) > 0:
                current['difficulty'] -= 1
        else:
            current['no'] = current.get('no', 0) + 1
            # ลดคะแนนเมื่อตอบผิด
            score -= 1
            # เพิ่มความยากเมื่อตอบผิดบ่อย
            if current['no'] >= 2:
                current['difficulty'] = min(current.get('difficulty', 0) + 1, 2)
        
        current['last_reviewed'] = datetime.now().isoformat()
        
        # คำนวณ priority score สำหรับการเรียงลำดับ
        current['priority_score'] = current['no'] * 2 - current['yes']
        
        # อัพเดทข้อมูลในฐานข้อมูล
        supabase.table("user_scores").upsert({
            "user_id": user_id,
            "score": score,
            "learned_words": learned_words,
            "vocab_stats": vocab_stats
        }, on_conflict="user_id").execute()
        
        return current
        
    except Exception as e:
        print(f"Update vocab score error: {e}")
        # สร้าง cache ใน memory ชั่วคราว
        return {
            'yes': 1 if answer_is_yes else 0,
            'no': 0 if answer_is_yes else 1,
            'difficulty': 0,
            'last_reviewed': datetime.now().isoformat(),
            'priority_score': (0 if answer_is_yes else 1) * 2 - (1 if answer_is_yes else 0)
        }

def get_random_flashcard(user_id):
    """สุ่ม flashcard โดยพิจารณาจากคะแนน"""
    try:
        # ดึงคำศัพท์ทั้งหมดจากฐานข้อมูล
        vocab_result = supabase.table("vocab").select("*").execute()
        
        # ถ้าฐานข้อมูลไม่มีข้อมูล ให้ใช้ DEFAULT_WORDS
        if not vocab_result.data or len(vocab_result.data) == 0:
            init_vocab_database()
            vocab_result = supabase.table("vocab").select("*").execute()
            if not vocab_result.data:
                # ถ้ายังไม่มี ให้ใช้ DEFAULT_WORDS โดยตรง
                vocab_list = DEFAULT_WORDS
            else:
                vocab_list = vocab_result.data
        else:
            vocab_list = vocab_result.data
        
        # ดึงคะแนนของผู้ใช้
        user_scores = get_user_vocab_scores(user_id)
        
        # ถ้าไม่มีคะแนนใดๆ เลย ให้สุ่มแบบปกติ
        if not user_scores:
            selected = random.choice(vocab_list)
        else:
            # คำนวณน้ำหนักสำหรับการสุ่ม
            weighted_vocab = []
            
            for item in vocab_list:
                word = item['word']
                score_data = user_scores.get(word, {'yes': 0, 'no': 0, 'difficulty': 0})
                
                # ยิ่งตอบผิดบ่อย ยิ่งมีน้ำหนักมาก
                weight = 1 + (score_data.get('no', 0) * 2) - (score_data.get('yes', 0) * 0.5)
                weight = max(1, min(weight, 10))
                
                # เพิ่มน้ำหนักสำหรับคำที่ไม่ได้ทบทวนนาน
                last_reviewed = score_data.get('last_reviewed')
                if last_reviewed:
                    try:
                        last_date = datetime.fromisoformat(last_reviewed.replace('Z', '+00:00'))
                        days_since = (datetime.now() - last_date).days
                        if days_since > 7:
                            weight *= 2
                    except:
                        pass
                
                # เพิ่มคำนี้ในลิสต์ตามน้ำหนัก
                weighted_vocab.extend([item] * int(weight))
            
            if weighted_vocab:
                selected = random.choice(weighted_vocab)
            else:
                selected = random.choice(vocab_list)
        
        # สุ่มรูปแบบคำถาม
        if random.choice([True, False]):
            # รูปแบบ: คำไทย -> อังกฤษ
            question = f"คำว่า '{selected.get('meaning', 'ไม่ระบุ')}' ภาษาอังกฤษ คืออะไร?"
            correct_answer = selected['word']
            question_type = "th_to_en"
        else:
            # รูปแบบ: อังกฤษ -> ไทย
            question = f"ภาษาอังกฤษ '{selected['word']}' ภาษาไทยคืออะไร?"
            correct_answer = selected.get('meaning', 'ไม่ระบุ')
            question_type = "en_to_th"
        
        return {
            'word': selected['word'],
            'meaning': selected.get('meaning', 'ไม่ระบุ'),
            'question': question,
            'correct_answer': correct_answer,
            'question_type': question_type,
            'example': selected.get('example_sentence', 'ไม่มีตัวอย่าง')
        }
        
    except Exception as e:
        print(f"Get flashcard error: {e}")
        # ใช้ default word ถ้ามีปัญหา
        selected = random.choice(DEFAULT_WORDS)
        
        if random.choice([True, False]):
            question = f"คำว่า '{selected['meaning']}' ภาษาอังกฤษ คืออะไร?"
            correct_answer = selected['word']
            question_type = "th_to_en"
        else:
            question = f"ภาษาอังกฤษ '{selected['word']}' ภาษาไทยคืออะไร?"
            correct_answer = selected['meaning']
            question_type = "en_to_th"
        
        return {
            'word': selected['word'],
            'meaning': selected['meaning'],
            'question': question,
            'correct_answer': correct_answer,
            'question_type': question_type,
            'example': selected['example_sentence']
        }

def get_review_words(user_id, count=3):
    """ดึงคำศัพท์ที่ยังไม่แม่นมาทบทวน"""
    try:
        scores = get_user_vocab_scores(user_id)
        
        if not scores:
            return []
        
        # กรองคำที่ตอบผิดบ่อยหรือยังไม่ค่อยได้ทบทวน
        weak_words = []
        
        for word, data in scores.items():
            yes_count = data.get('yes', 0)
            no_count = data.get('no', 0)
            
            # คำที่ตอบผิดมากกว่าถูก หรือยังไม่ได้ทบทวนนาน
            if no_count > yes_count or yes_count + no_count == 0:
                weak_words.append({
                    'word': word,
                    'no_count': no_count,
                    'yes_count': yes_count,
                    'priority': data.get('priority_score', 0)
                })
        
        # เรียงลำดับตาม priority (ยิ่งสูงยิ่งควรทบทวน)
        weak_words.sort(key=lambda x: x.get('priority', 0), reverse=True)
        
        # ดึงข้อมูลคำศัพท์เพิ่มเติม
        review_words = []
        for word_data in weak_words[:count]:
            word = word_data['word']
            
            # ดึงข้อมูลคำศัพท์จากตาราง vocab
            try:
                vocab_result = supabase.table("vocab").select("*").eq("word", word).execute()
                if vocab_result.data:
                    vocab_info = vocab_result.data[0]
                    review_words.append({
                        'word': word,
                        'meaning': vocab_info.get('meaning', 'ไม่ระบุ'),
                        'example': vocab_info.get('example_sentence', 'ไม่มีตัวอย่าง')
                    })
            except:
                # ถ้าไม่เจอในฐานข้อมูล ให้ใช้ข้อมูลจาก DEFAULT_WORDS
                for default_word in DEFAULT_WORDS:
                    if default_word['word'].lower() == word.lower():
                        review_words.append({
                            'word': word,
                            'meaning': default_word['meaning'],
                            'example': default_word['example_sentence']
                        })
                        break
        
        return review_words
        
    except Exception as e:
        print(f"Get review words error: {e}")
        return []

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Flashcard Bot (Fixed) is ready!"}

@app.get("/daily-review")
def daily_review():
    """ส่งคำศัพท์ให้ทบทวนตามเวลา"""
    try:
        users = supabase.table("users").select("user_id").execute().data
        if not users: 
            return {"msg": "No users found"}
        
        current_hour = datetime.now().hour
        
        # กำหนดข้อความตามเวลา
        if 5 <= current_hour < 12:
            time_greeting = "สวัสดีตอนเช้า"
        elif 12 <= current_hour < 17:
            time_greeting = "สวัสดีตอนกลางวัน"
        elif 17 <= current_hour < 21:
            time_greeting = "สวัสดีตอนเย็น"
        else:
            time_greeting = "สวัสดีตอนค่ำ"
        
        for user in users:
            user_id = user['user_id']
            
            # ดึงคำศัพท์ที่ควรทบทวน
            review_words = get_review_words(user_id, 3)
            
            if review_words:
                # สร้างข้อความทบทวน
                review_text = f"{time_greeting} : ทบทวนคำศัพท์กันหน่อย {len(review_words)} คำที่คุณยังไม่แม่น\n\n"
                
                for i, word_data in enumerate(review_words, 1):
                    review_text += f"{i}. {word_data['word']} = {word_data.get('meaning', 'ไม่ระบุ')}\n"
                    review_text += f"   📝 ตัวอย่าง: {word_data.get('example', 'ไม่มีตัวอย่าง')}\n\n"
                
                try:
                    line_bot_api.push_message(user_id, TextSendMessage(text=review_text))
                except Exception as e:
                    print(f"Push message error for {user_id}: {e}")
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
    
    # === MENU: คำสั่ง ===
    if user_msg in ["คำสั่ง", "เมนู", "menu", "help"]:
        scores = get_user_vocab_scores(user_id)
        total_words = len(scores)
        known_words = sum(1 for data in scores.values() if data.get('yes', 0) > data.get('no', 0))
        
        # ดึงคะแนนรวม
        try:
            result = supabase.table("user_scores").select("score").eq("user_id", user_id).execute()
            total_score = result.data[0]['score'] if result.data else 0
        except:
            total_score = 0
        
        reply_text = (f"📚 Flashcard Bot\n\n"
                     f"พิมพ์ 'เริ่มเกม :' เพื่อเริ่มทายคำศัพท์\n"
                     f"ตอบได้ = Yes, ตอบไม่ได้ = No\n\n"
                     f"📊 สถิติ: รู้แล้ว {known_words}/{total_words} คำ\n"
                     f"⭐ คะแนนรวม: {total_score}")
    
    # === MENU: เริ่มเกม : ===
    elif user_msg.startswith("เริ่มเกม :"):
        # สุ่ม flashcard ใหม่
        flashcard = get_random_flashcard(user_id)
        
        # บันทึก flashcard ปัจจุบัน
        user_flashcards[user_id] = {
            'word': flashcard['word'],
            'meaning': flashcard['meaning'],
            'question_type': flashcard['question_type'],
            'correct_answer': flashcard['correct_answer'],
            'example': flashcard['example']
        }
        
        reply_text = f"🎮 Flashcard\n\n{flashcard['question']}\n\nตอบได้ = Yes, ตอบไม่ได้ = No"
    
    # === การตอบ Yes/No สำหรับ flashcard ===
    elif user_id in user_flashcards and user_msg.lower() in ["yes", "no", "y", "n", "ใช่", "ไม่"]:
        current_card = user_flashcards[user_id]
        word = current_card['word']
        
        # แปลงคำตอบเป็น boolean
        answer_is_yes = user_msg.lower() in ["yes", "y", "ใช่"]
        
        # อัพเดทคะแนน
        update_vocab_score(user_id, word, answer_is_yes)
        
        # แสดงคำตอบที่ถูกต้อง
        if answer_is_yes:
            reply_text = f"✅ ดีมาก! คุณตอบถูกต้อง\n\n"
        else:
            reply_text = f"❌ ไม่เป็นไร มาดูคำตอบกัน\n\n"
        
        reply_text += f"คำตอบที่ถูกต้อง: {current_card['correct_answer']}\n\n"
        
        # แสดงตัวอย่างประโยคทุกครั้ง
        reply_text += f"📝 ตัวอย่าง: {current_card['example']}\n\n"
        
        reply_text += "พิมพ์ 'เริ่มเกม :' เพื่อเล่นต่อ"
        
        # ลบ flashcard ปัจจุบัน
        del user_flashcards[user_id]
    
    # === DEFAULT RESPONSE ===
    else:
        if user_id in user_flashcards:
            reply_text = "⚠️ กรุณาตอบ Yes หรือ No สำหรับ flashcard ปัจจุบัน"
        else:
            reply_text = "พิมพ์ 'เริ่มเกม :' เพื่อเริ่มทายคำศัพท์\nพิมพ์ 'คำสั่ง' เพื่อดูวิธีใช้"
    
    # ส่งข้อความกลับ Line
    if reply_text:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            print(f"LINE Reply Error: {e}")

# --- 5. INITIALIZATION ---
def init_app():
    """เตรียมข้อมูลเริ่มต้นเมื่อเริ่มแอป"""
    print("🚀 กำลังเตรียมข้อมูล Flashcard Bot...")
    
    # ตรวจสอบและเพิ่มคำศัพท์พื้นฐาน
    init_vocab_database()
    
    print("✅ Flashcard Bot พร้อมใช้งานแล้ว!")

if __name__ == "__main__":
    # เตรียมข้อมูลเริ่มต้น
    init_app()
    
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)