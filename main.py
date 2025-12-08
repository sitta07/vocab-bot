# vocab-flashcard-bot.py
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
user_vocab_scores = {}

# --- 2. HELPER FUNCTIONS ---
def save_user(user_id):
    """เก็บ User ID ลง DB"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except: 
        pass

def get_user_vocab_scores(user_id):
    """ดึงคะแนนคำศัพท์ของผู้ใช้"""
    try:
        result = supabase.table("vocab_scores").select("*").eq("user_id", user_id).execute()
        scores = {}
        if result.data:
            for item in result.data:
                scores[item['word']] = {
                    'yes': item.get('yes_count', 0),
                    'no': item.get('no_count', 0),
                    'last_reviewed': item.get('last_reviewed'),
                    'difficulty': item.get('difficulty', 0)  # 0 = ง่าย, 1 = ปานกลาง, 2 = ยาก
                }
        return scores
    except:
        return {}

def update_vocab_score(user_id, word, answer_is_yes):
    """อัพเดทคะแนนคำศัพท์เมื่อผู้ใช้ตอบ Yes/No"""
    try:
        scores = get_user_vocab_scores(user_id)
        current = scores.get(word, {'yes': 0, 'no': 0, 'difficulty': 0})
        
        if answer_is_yes:
            current['yes'] = current.get('yes', 0) + 1
            # ลดความยากเมื่อตอบถูกบ่อย
            if current['yes'] >= 3 and current['difficulty'] > 0:
                current['difficulty'] -= 1
        else:
            current['no'] = current.get('no', 0) + 1
            # เพิ่มความยากเมื่อตอบผิดบ่อย
            if current['no'] >= 2:
                current['difficulty'] = min(current.get('difficulty', 0) + 1, 2)
        
        # คำนวณคะแนนรวมสำหรับเรียงลำดับ (ยิ่งตอบผิดมาก ยิ่งควรทบทวนบ่อย)
        priority_score = current['no'] * 2 - current['yes']
        
        # บันทึกลงฐานข้อมูล
        supabase.table("vocab_scores").upsert({
            "user_id": user_id,
            "word": word,
            "yes_count": current['yes'],
            "no_count": current['no'],
            "difficulty": current['difficulty'],
            "priority_score": priority_score,
            "last_reviewed": datetime.now().isoformat()
        }, on_conflict=["user_id", "word"]).execute()
        
        return current
    except Exception as e:
        print(f"Update score error: {e}")
        return None

def get_random_flashcard(user_id):
    """สุ่ม flashcard โดยพิจารณาจากคะแนน (คำที่ตอบผิดบ่อยจะได้โอกาสมากกว่า)"""
    try:
        # ดึงคำศัพท์ทั้งหมด
        vocab_result = supabase.table("vocab").select("*").execute()
        if not vocab_result.data:
            return get_default_flashcard()
        
        vocab_list = vocab_result.data
        
        # ดึงคะแนนของผู้ใช้
        user_scores = get_user_vocab_scores(user_id)
        
        # คำนวณน้ำหนักสำหรับการสุ่ม
        weighted_vocab = []
        for item in vocab_list:
            word = item['word']
            score_data = user_scores.get(word, {'yes': 0, 'no': 0, 'difficulty': 0})
            
            # คำนวณน้ำหนัก: ยิ่งตอบผิดมาก ยิ่งได้น้ำหนักมาก
            weight = 1 + (score_data['no'] * 2) - (score_data['yes'] * 0.5)
            weight = max(1, min(weight, 10))  # จำกัดน้ำหนักระหว่าง 1-10
            
            # เพิ่มน้ำหนักสำหรับคำที่ไม่ได้ทบทวนนาน
            last_reviewed = score_data.get('last_reviewed')
            if last_reviewed:
                last_date = datetime.fromisoformat(last_reviewed.replace('Z', '+00:00'))
                days_since = (datetime.now() - last_date).days
                if days_since > 7:
                    weight *= 2
            
            weighted_vocab.extend([item] * int(weight))
        
        # ถ้ายังไม่มีคำศัพท์ใดๆ หรือ weight calculation ไม่ได้ผล
        if not weighted_vocab:
            weighted_vocab = vocab_list
        
        # สุ่มเลือกคำศัพท์
        selected = random.choice(weighted_vocab)
        
        # สุ่มรูปแบบคำถาม (ไทย->อังกฤษ หรือ อังกฤษ->ไทย)
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
        return get_default_flashcard()

def get_default_flashcard():
    """Default flashcard list"""
    default_words = [
        {
            "word": "learn",
            "meaning": "เรียนรู้",
            "example": "I want to learn English."
        },
        {
            "word": "study", 
            "meaning": "ศึกษา",
            "example": "He studies at university."
        },
        {
            "word": "practice",
            "meaning": "ฝึกฝน", 
            "example": "Practice makes perfect."
        },
        {
            "word": "happy",
            "meaning": "มีความสุข",
            "example": "I am very happy today."
        },
        {
            "word": "friend",
            "meaning": "เพื่อน",
            "example": "He is my best friend."
        }
    ]
    
    selected = random.choice(default_words)
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
        'example': selected['example']
    }

def get_review_words(user_id, count=3):
    """ดึงคำศัพท์ที่ยังไม่แม่น (ตอบ No บ่อย) มาทบทวน"""
    try:
        scores = get_user_vocab_scores(user_id)
        
        # กรองคำที่ตอบผิดบ่อย (no_count > yes_count)
        weak_words = []
        for word, data in scores.items():
            if data.get('no', 0) > data.get('yes', 0):
                weak_words.append({
                    'word': word,
                    'no_count': data.get('no', 0),
                    'yes_count': data.get('yes', 0)
                })
        
        # เรียงลำดับตามจำนวนครั้งที่ตอบผิด
        weak_words.sort(key=lambda x: x['no_count'], reverse=True)
        
        # จำกัดจำนวนคำ
        review_words = weak_words[:count]
        
        # ถ้าไม่มีคำที่ตอบผิดบ่อย ให้เลือกคำที่ยังไม่ได้เรียนหรือเรียนน้อยครั้ง
        if not review_words:
            # ดึงคำศัพท์ทั้งหมด
            vocab_result = supabase.table("vocab").select("word, meaning").execute()
            all_words = vocab_result.data if vocab_result.data else []
            
            # กรองคำที่ยังไม่มีคะแนนหรือมีคะแนนน้อย
            for word_data in all_words:
                word = word_data['word']
                if word not in scores or scores[word].get('yes', 0) + scores[word].get('no', 0) < 2:
                    review_words.append({
                        'word': word,
                        'meaning': word_data.get('meaning', 'ไม่ระบุ')
                    })
                    if len(review_words) >= count:
                        break
        
        return review_words
    except:
        return []

# --- 3. API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Flashcard Bot is ready!"}

@app.get("/daily-review")
def daily_review():
    """ส่งคำศัพท์ให้ทบทวนตามเวลา (Cron Job)"""
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
                    # ดึงตัวอย่างประโยค
                    try:
                        vocab_result = supabase.table("vocab").select("example_sentence").eq("word", word_data['word']).execute()
                        example = vocab_result.data[0]['example_sentence'] if vocab_result.data else "ไม่มีตัวอย่าง"
                    except:
                        example = "ไม่มีตัวอย่าง"
                    
                    review_text += f"{i}. {word_data['word']} = {word_data.get('meaning', 'ไม่ระบุ')}\n"
                    review_text += f"   📝 ตัวอย่าง: {example}\n\n"
                
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
        
        reply_text = (f"📚 Flashcard Bot - คำสั่ง\n\n"
                      f"1. เริ่มเกม : เริ่มเล่นทายคำศัพท์\n"
                      f"2. สถิติ : ดูสถิติการเรียน\n"
                      f"3. ทบทวน : แสดงคำศัพท์ที่ควรทบทวน\n"
                      f"4. เพิ่มคำศัพท์:[คำอังกฤษ]:[คำไทย] : เพิ่มคำศัพท์ใหม่\n"
                      f"5. ตัวอย่าง : ขอดูตัวอย่างการใช้\n\n"
                      f"📊 สถิติ: รู้แล้ว {known_words}/{total_words} คำ")
    
    # === MENU: สถิติ ===
    elif user_msg in ["สถิติ", "stat", "stats", "score"]:
        scores = get_user_vocab_scores(user_id)
        total_words = len(scores)
        
        if total_words == 0:
            reply_text = "📊 คุณยังไม่ได้เริ่มเรียนคำศัพท์เลย พิมพ์ 'เริ่มเกม :' เพื่อเริ่มต้น吧!"
        else:
            # คำนวณสถิติ
            known_words = sum(1 for data in scores.values() if data.get('yes', 0) > data.get('no', 0))
            difficult_words = sum(1 for data in scores.values() if data.get('no', 0) >= 3)
            total_yes = sum(data.get('yes', 0) for data in scores.values())
            total_no = sum(data.get('no', 0) for data in scores.values())
            
            # คำที่ควรทบทวน (ตอบผิดมากกว่าตอบถูก)
            need_review = []
            for word, data in scores.items():
                if data.get('no', 0) > data.get('yes', 0):
                    need_review.append(word)
            
            reply_text = (f"📊 สถิติการเรียน\n\n"
                         f"📚 เรียนทั้งหมด: {total_words} คำ\n"
                         f"✅ รู้แล้ว: {known_words} คำ\n"
                         f"❌ ยาก: {difficult_words} คำ\n"
                         f"📈 ตอบถูก: {total_yes} ครั้ง\n"
                         f"📉 ตอบผิด: {total_no} ครั้ง\n"
                         f"📝 ต้องทบทวน: {len(need_review)} คำ")
            
            if need_review:
                reply_text += f"\n\nคำที่ควรทบทวน:\n"
                for i, word in enumerate(need_review[:5], 1):
                    reply_text += f"{i}. {word}\n"
                if len(need_review) > 5:
                    reply_text += f"... และอีก {len(need_review)-5} คำ"
    
    # === MENU: ทบทวน ===
    elif user_msg in ["ทบทวน", "review", "weak"]:
        review_words = get_review_words(user_id, 5)
        
        if not review_words:
            reply_text = "🎉 ยินดีด้วย! ตอนนี้คุณยังไม่มีคำศัพท์ที่ต้องทบทวนพิเศษ"
        else:
            reply_text = f"📝 คำศัพท์ที่ควรทบทวน ({len(review_words)} คำ)\n\n"
            
            for i, word_data in enumerate(review_words, 1):
                # ดึงข้อมูลเพิ่มเติม
                try:
                    vocab_result = supabase.table("vocab").select("*").eq("word", word_data['word']).execute()
                    if vocab_result.data:
                        vocab_info = vocab_result.data[0]
                        example = vocab_info.get('example_sentence', 'ไม่มีตัวอย่าง')
                        meaning = vocab_info.get('meaning', 'ไม่ระบุ')
                    else:
                        example = "ไม่มีตัวอย่าง"
                        meaning = word_data.get('meaning', 'ไม่ระบุ')
                except:
                    example = "ไม่มีตัวอย่าง"
                    meaning = word_data.get('meaning', 'ไม่ระบุ')
                
                reply_text += f"{i}. {word_data['word']} = {meaning}\n"
                reply_text += f"   📝 ตัวอย่าง: {example}\n\n"
            
            reply_text += "พิมพ์ 'เริ่มเกม :' เพื่อฝึกฝนคำศัพท์เหล่านี้"
    
    # === MENU: เริ่มเกม : ===
    elif user_msg.startswith("เริ่มเกม :"):
        # สุ่ม flashcard ใหม่
        flashcard = get_random_flashcard(user_id)
        
        # บันทึก flashcard ปัจจุบัน
        user_flashcards[user_id] = {
            'word': flashcard['word'],
            'meaning': flashcard['meaning'],
            'question_type': flashcard['question_type'],
            'correct_answer': flashcard['correct_answer']
        }
        
        reply_text = f"🎮 Flashcard\n\n{flashcard['question']}\n\nตอบได้ = Yes, ตอบไม่ได้ = No"
    
    # === MENU: ตัวอย่าง ===
    elif user_msg in ["ตัวอย่าง", "example", "ex"]:
        if user_id in user_flashcards:
            current_card = user_flashcards[user_id]
            
            # ดึงตัวอย่างประโยค
            try:
                result = supabase.table("vocab").select("example_sentence").eq("word", current_card['word']).execute()
                if result.data and result.data[0].get('example_sentence'):
                    example = result.data[0]['example_sentence']
                else:
                    example = "ไม่มีตัวอย่างประโยคสำหรับคำนี้"
            except:
                example = "ไม่มีตัวอย่างประโยคสำหรับคำนี้"
            
            reply_text = f"📝 ตัวอย่างการใช้ '{current_card['word']}':\n\n{example}"
        else:
            reply_text = "⚠️ กรุณาพิมพ์ 'เริ่มเกม :' เพื่อเริ่มเกมก่อน"
    
    # === MENU: เพิ่มคำศัพท์ ===
    elif user_msg.startswith("เพิ่มคำศัพท์:"):
        try:
            # แยกคำศัพท์และความหมาย
            parts = user_msg.split(":", 1)[1].strip()
            if ":" in parts:
                english_word, thai_meaning = parts.split(":", 1)
                english_word = english_word.strip()
                thai_meaning = thai_meaning.strip()
                
                # เพิ่มลงฐานข้อมูล
                try:
                    supabase.table("vocab").upsert({
                        "word": english_word.lower(),
                        "meaning": thai_meaning,
                        "example_sentence": f"ตัวอย่างประโยคสำหรับ '{english_word}'"
                    }, on_conflict="word").execute()
                    
                    reply_text = f"✅ เพิ่มคำศัพท์สำเร็จ!\n\n{english_word} = {thai_meaning}"
                except Exception as e:
                    print(f"Add vocab error: {e}")
                    reply_text = "⚠️ ไม่สามารถเพิ่มคำศัพท์ได้ในขณะนี้"
            else:
                reply_text = "⚠️ รูปแบบไม่ถูกต้อง\nใช้: เพิ่มคำศัพท์:[คำอังกฤษ]:[คำไทย]\nเช่น: เพิ่มคำศัพท์:apple:แอปเปิ้ล"
        except:
            reply_text = "⚠️ รูปแบบไม่ถูกต้อง\nใช้: เพิ่มคำศัพท์:[คำอังกฤษ]:[คำไทย]\nเช่น: เพิ่มคำศัพท์:apple:แอปเปิ้ล"
    
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
        try:
            result = supabase.table("vocab").select("example_sentence").eq("word", word).execute()
            if result.data and result.data[0].get('example_sentence'):
                example = result.data[0]['example_sentence']
                reply_text += f"📝 ตัวอย่าง: {example}\n\n"
        except:
            pass
        
        reply_text += "พิมพ์ 'เริ่มเกม :' เพื่อเล่นต่อ"
        
        # ลบ flashcard ปัจจุบัน
        del user_flashcards[user_id]
    
    # === DEFAULT RESPONSE ===
    else:
        if user_id in user_flashcards:
            # ถ้ามี flashcard กำลังเล่นอยู่
            reply_text = "⚠️ กรุณาตอบ Yes หรือ No สำหรับ flashcard ปัจจุบัน\nหรือพิมพ์ 'ตัวอย่าง' เพื่อดูตัวอย่างประโยค"
        else:
            reply_text = ("🤖 Flashcard Bot\n\n"
                         "พิมพ์ 'เริ่มเกม :' เพื่อเริ่มทายคำศัพท์\n"
                         "พิมพ์ 'คำสั่ง' เพื่อดูคำสั่งทั้งหมด\n"
                         "พิมพ์ 'สถิติ' เพื่อดูสถิติการเรียน")
    
    # ส่งข้อความกลับ Line
    if reply_text:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            print(f"LINE Reply Error: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)