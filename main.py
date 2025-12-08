# gemini-flash-latest
import os
import random
import json
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
# ปรับ model เป็น flash เพื่อความไวและประหยัด
model = genai.GenerativeModel('gemini-flash-latest') 

# Setup Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Error: {e}")

# 🔥 GLOBAL STATE (RAM)
# Structure: { 'user_id': {'word': 'revise', 'meaning': '...'} }
# ตัด attempts ออกเพราะใช้ logic ตอบรอบเดียวจบ
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
    return {"status": "ok", "msg": "Teacher Bot V2 (Senior Logic) is ready!"}

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
                # เก็บ session (ไม่ต้องมี attempts แล้ว)
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
    if user_msg in ["คำสั่ง", "เมนู", "menu"]:
        score, learned = get_user_score(user_id)
        reply_text = (f"🤖 คู่มือครูพี่ Bot V2:\n\n"
                      f"1. เริ่มเกม -> เริ่มทายคำศัพท์\n"
                      f"2. คะแนน -> ดูคะแนน\n"
                      f"3. คำใบ้ -> ขอคำใบ้ (ลด -2 คะแนน)\n"
                      f"4. เพิ่ม: [ศัพท์] -> เพิ่มคำใหม่\n"
                      f"5. ลบ: [ศัพท์] -> ลบคำ\n"
                      f"6. คลัง -> ดูศัพท์ทั้งหมด\n\n"
                      f"📊 คะแนน: {score} | 📚 จำได้: {len(learned)} คำ")

    # === MENU 2: คะแนน ===
    elif user_msg in ["คะแนน", "score", "สถิติ"]:
        score, learned = get_user_score(user_id)
        reply_text = (f"📊 สถิติความเทพ:\n\n"
                      f"⭐ คะแนนรวม: {score} XP\n"
                      f"📚 คำศัพท์ที่แม่นแล้ว: {len(learned)} คำ")

    # === MENU 3: เริ่มเกม ===
    elif user_msg in ["เริ่มเกม", "เริ่ม", "start", "play"]:
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
                'hint_given': False
            }
            
            reply_text = (f"🎮 เริ่มกันเลย!\n\n"
                          f"❓ คำว่า '{word}' แปลว่าอะไร?\n\n"
                          f"💡 ตอบภาษาไทยมาเลย (ตอบผิดมีเฉลยให้ทันที)")

    # === MENU 4: คำใบ้ ===
    elif user_msg in ["คำใบ้", "hint"]:
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
    elif user_msg in ["คลังคำศัพท์", "คลัง", "vocab"]:
        try:
            response = supabase.table("vocab").select("word").order("id", desc=True).limit(20).execute()
            words = response.data
            if not words:
                reply_text = "📭 คลังว่างเปล่าครับ"
            else:
                word_list = "\n".join([f"- {item['word']}" for item in words])
                reply_text = f"📚 ศัพท์ 20 คำล่าสุด:\n\n{word_list}"
        except: 
            reply_text = "⚠️ ดึงข้อมูลไม่ได้ครับ เช็ค DB แป๊บ"

    # === MENU 6: ลบคำศัพท์ ===
    elif user_msg.startswith(("ลบคำศัพท์:", "ลบ:")):
        try:
            target = user_msg.split(":", 1)[1].strip()
            if target:
                supabase.table("vocab").delete().ilike("word", target).execute()
                reply_text = f"🗑️ ลบ '{target}' เรียบร้อยครับ"
            else: 
                reply_text = "อย่าลืมใส่คำที่ต้องการลบหลัง : ด้วยนะครับ"
        except: 
            reply_text = "⚠️ ระบบลบมีปัญหา ลองใหม่ครับ"

    # === MENU 7: เพิ่มคำศัพท์ (ปรับปรุงใหม่ด้วย JSON) ===
    elif user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
            if word:
                # Prompt ขอ JSON เพื่อความแม่นยำ
                prompt = (f"I want to learn the word '{word}'. "
                          f"Provide the Thai meaning and 1 short English example sentence. "
                          f"Response strictly in JSON format: "
                          f'{{"meaning": "...", "example": "..."}}')
                
                res = model.generate_content(prompt)
                
                # Cleaning JSON string
                clean_text = res.text.strip().replace("```json", "").replace("```", "")
                data = json.loads(clean_text)

                meaning = data.get("meaning", "-")
                example = data.get("example", "-")

                supabase.table("vocab").insert({
                    "word": word, 
                    "meaning": meaning, 
                    "example_sentence": example
                }).execute()
                
                reply_text = f"✅ จดศัพท์ใหม่แล้ว!\n🔤 {word}\n📖 {meaning}\n🗣️ {example}"
            else: 
                reply_text = "ใส่คำศัพท์หลัง : ด้วยนะครับ เช่น 'เพิ่ม: Resilience'"
        except Exception as e:
            print(f"Add vocab error: {e}")
            reply_text = "⚠️ AI งงเล็กน้อย ลองพิมพ์ใหม่อีกรอบครับ เช็คตัวสะกดนิดนึง"

    # === MENU 8: ตรวจคำตอบ (ปรับปรุงใหม่ ตอบทีเดียวจบ) ===
    else:
        if user_id not in user_sessions:
            reply_text = "🤔 อยากเล่นเกมพิมพ์ 'เริ่มเกม' ได้เลยครับ\nหรือพิมพ์ 'คำสั่ง' เพื่อดูเมนู"
        else:
            session = user_sessions[user_id]
            word = session['word']
            correct_meaning = session['meaning']
            
            try:
                # Prompt ชุดเดียว ได้ครบทุกอย่าง (ตรวจ, เหตุผล, ตัวอย่าง)
                prompt = (f"User is learning vocabulary. Word: '{word}' (Meaning: {correct_meaning}).\n"
                          f"User answered: '{user_msg}'\n\n"
                          f"1. Check if the answer is correct (accept synonyms).\n"
                          f"2. Explain why in Thai (short and encouraging).\n"
                          f"3. Create 3 distinct, simple English example sentences using '{word}'.\n\n"
                          f"Response strictly in JSON format:\n"
                          f'{{"is_correct": boolean, "reason_thai": "...", "examples": ["Ex1", "Ex2", "Ex3"]}}')
                
                res = model.generate_content(prompt)
                
                # Cleaning & Parsing
                clean_text = res.text.strip().replace("```json", "").replace("```", "")
                result = json.loads(clean_text)
                
                is_correct = result.get("is_correct", False)
                reason = result.get("reason_thai", "ไม่มีคำอธิบาย")
                examples = result.get("examples", [])
                
                # จัด Format ตัวอย่างประโยค
                example_txt = "\n".join([f"• {ex}" for ex in examples])

                # ล้าง Session ทันที (One-shot Logic)
                del user_sessions[user_id]

                if is_correct:
                    # ✅ ถูกต้อง
                    new_score = update_score(user_id, 10)
                    mark_word_learned(user_id, word)
                    
                    reply_text = (f"🎉 สุดยอด! ถูกต้องครับ (+10 คะแนน)\n\n"
                                  f"💬 {reason}\n"
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
            
            except Exception as e:
                print(f"Check answer error: {e}")
                reply_text = "😵‍💫 ระบบประมวลผลผิดพลาด ลองตอบใหม่อีกทีนะครับ"

    # ส่งข้อความกลับ Line
    if reply_text:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            print(f"LINE Reply Error: {e}")