import os
import random
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client
from dotenv import load_dotenv

# โหลดตัวแปรจากไฟล์ .env
load_dotenv()

app = FastAPI()

# --- 1. CONFIGURATION ---
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# เช็คว่าใส่ Key ครบไหม
if not all([LINE_ACCESS_TOKEN, LINE_SECRET, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("⚠️ Warning: Environment variables are missing! Check .env file.")

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# ตั้งค่า Gemini (แนะนำใช้ model นี้เพราะเสถียรสุด)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

# เชื่อมต่อ Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Error: {e}")

# --- 2. HELPER FUNCTIONS ---
def save_user(user_id):
    """บันทึก User ID ลงฐานข้อมูลถ้ายังไม่มี"""
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except Exception as e:
        print(f"Save user error: {e}")

# --- 3. API ROUTES ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Bot is active!"}

@app.get("/broadcast-quiz")
def broadcast_quiz():
    """ฟังก์ชันยิงคำศัพท์สุ่มไปหาทุกคน"""
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
               f"👉 จงแต่งประโยคภาษาอังกฤษโดยใช้คำนี้ส่งกลับมา!")

        for user in users:
            try:
                line_bot_api.push_message(user['user_id'], TextSendMessage(text=msg))
            except:
                continue
            
        return {"status": "success", "word": word}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/callback")
async def callback(request: Request):
    """รับ Webhook จาก LINE"""
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

# --- 4. MAIN BOT LOGIC ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id
    save_user(user_id) # เก็บ user id ทุกครั้งที่ทักมา
    
    reply_text = "" # ตัวแปรสำหรับเก็บข้อความที่จะตอบกลับ

    # --- MENU: คำสั่ง ---
    if user_msg == "คำสั่ง":
        reply_text = (f"🤖 คู่มือการใช้งาน:\n\n"
                      f"1. เพิ่ม: [ศัพท์] -> เช่น เพิ่ม: Cat\n"
                      f"2. ลบคำศัพท์: [ศัพท์] -> เช่น ลบคำศัพท์: Cat\n"
                      f"3. คลังคำศัพท์ -> ดูรายการศัพท์ทั้งหมด\n"
                      f"4. (พิมพ์ประโยคภาษาอังกฤษ) -> ส่งการบ้านให้ตรวจ")

    # --- ACTION: ดูคลังคำศัพท์ ---
    elif user_msg == "คลังคำศัพท์":
        try:
            response = supabase.table("vocab").select("word").order("id", desc=True).limit(20).execute()
            words = response.data
            if not words:
                reply_text = "📭 คลังว่างเปล่าครับ ลองพิมพ์ 'เพิ่ม: Hello' ดูสิ"
            else:
                word_list = "\n".join([f"- {item['word']}" for item in words])
                reply_text = f"📚 คำศัพท์ล่าสุด ({len(words)}):\n\n{word_list}"
        except Exception as e:
            reply_text = f"❌ Error ดูคลัง: {str(e)}"

    # --- ACTION: ลบคำศัพท์ ---
    elif user_msg.startswith("ลบคำศัพท์:"):
        try:
            word_to_delete = user_msg.split(":", 1)[1].strip()
            if not word_to_delete:
                reply_text = "⚠️ กรุณาพิมพ์ศัพท์ที่ต้องการลบหลัง : ด้วยครับ"
            else:
                # 1. หา ID ของคำศัพท์
                search_res = supabase.table("vocab").select("id, word").ilike("word", word_to_delete).execute()
                
                if not search_res.data:
                    reply_text = f"❌ ไม่พบคำว่า '{word_to_delete}' ในระบบครับ"
                else:
                    target_id = search_res.data[0]['id']
                    real_word = search_res.data[0]['word']

                    # 2. ลบ Logs ที่เกี่ยวข้องก่อน (เพื่อไม่ให้ติด Foreign Key)
                    supabase.table("user_logs").delete().eq("vocab_id", target_id).execute()

                    # 3. ลบ Vocab
                    supabase.table("vocab").delete().eq("id", target_id).execute()
                    
                    reply_text = f"🗑️ ลบคำว่า '{real_word}' เรียบร้อยแล้วครับ"
        except Exception as e:
            reply_text = f"❌ Error ลบศัพท์: {str(e)}"

    # --- ACTION: เพิ่มคำศัพท์ ---
    elif user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
        except:
            word = ""  
            
        if not word:
            reply_text = "⚠️ อย่าลืมใส่คำศัพท์หลังเครื่องหมาย : นะครับ"
        else:
            try:
                # ให้ AI หาความหมายและตัวอย่างประโยค
                prompt = (f"Word: '{word}'. Provide Meaning (Thai) and Example sentence (English). "
                          f"Format:\nMeaning: ...\nExample: ...")
                
                res = model.generate_content(prompt)
                text = res.text.strip()
                meaning, example = "-", "-"
                
                # แกะ response จาก AI
                for line in text.split('\n'):
                    if line.startswith("Meaning:"): meaning = line.replace("Meaning:", "").strip()
                    elif line.startswith("Example:"): example = line.replace("Example:", "").strip()

                # บันทึกลง Supabase
                supabase.table("vocab").insert({
                    "word": word, 
                    "meaning": meaning, 
                    "example_sentence": example  # <-- เช็คชื่อ column ใน DB ว่าตรงกับตรงนี้ไหม
                }).execute()
                
                reply_text = f"✅ จดศัพท์เรียบร้อย!\n🔤 {word}\n📖 {meaning}\n🗣️ {example}"
            
            except Exception as e:
                # แจ้ง Error ตัวแดง ถ้าพัง
                reply_text = f"❌ Error เพิ่มศัพท์: {str(e)}"

    # --- ACTION: ตรวจการบ้าน (AI Teacher) ---
    else:
        # กรองข้อความสั้นๆ ทิ้งไปเลย เพื่อไม่ให้เปลือง AI
        if len(user_msg) < 3:
            return 

        try:
            # Prompt สั่งให้ AI ตรวจ + แก้ + แปล
            prompt = (f"User input: '{user_msg}'\n"
                      f"Role: English Teacher.\n"
                      f"Task: Check if input is a valid English sentence trying to use a vocabulary.\n"
                      f"Rules:\n"
                      f"1. If input is nonsense, greeting (Hi, Hello), or not a sentence -> Respond exactly 'SKIP'\n"
                      f"2. If Incorrect -> Provide 'Correction' (rewrite correctly) AND 'Feedback' (Explain in Thai why + Translate correction).\n"
                      f"3. If Correct -> 'Correction' is '-' AND 'Feedback' is praise in Thai.\n"
                      f"Format:\nWord: [Main word]\nCorrect: [Yes/No]\nCorrection: [Corrected Sentence]\nFeedback: [Thai explanation]")
            
            res = model.generate_content(prompt)
            ai_text = res.text.strip()

            # ถ้า AI บอกให้ข้าม ก็จบงาน
            if "SKIP" in ai_text:
                return

            detected_word, is_correct, correction, feedback = "Unknown", False, "-", "-"
            
            # แกะข้อมูลจาก AI
            for line in ai_text.split('\n'):
                line = line.strip()
                if line.startswith("Word:"): detected_word = line.replace("Word:", "").strip()
                elif line.startswith("Correct:"): is_correct = "Yes" in line
                elif line.startswith("Correction:"): correction = line.replace("Correction:", "").strip()
                elif line.startswith("Feedback:"): feedback = line.replace("Feedback:", "").strip()

            # หา ID ของศัพท์ที่ user พิมพ์มา (ถ้ามีในคลัง)
            vocab_data = supabase.table("vocab").select("id").ilike("word", detected_word).execute().data
            vocab_id = vocab_data[0]['id'] if vocab_data else None
            
            # บันทึกประวัติการตอบ
            supabase.table("user_logs").insert({
                "user_id": user_id,
                "vocab_id": vocab_id,
                "user_answer": user_msg,
                "is_correct": is_correct
            }).execute()

            # ตอบกลับผลลัพธ์
            if is_correct:
                reply_text = f"🎉 เก่งมาก! ({detected_word})\n✅ ถูกต้องเป๊ะเลย\n\n💬 {feedback}"
            else:
                reply_text = (f"🤏 นิดนึงนะ... ({detected_word})\n"
                              f"❌ ยังมีจุดแก้ครับ\n\n"
                              f"💡 ประโยคที่ถูก: {correction}\n"
                              f"💬 คำอธิบาย: {feedback}")

        except Exception as e:
            # แจ้ง Error ตัวแดง ถ้าพัง
            print(f"AI Check Error: {e}")
            reply_text = f"❌ Error ตรวจการบ้าน: {str(e)}"

    # ส่งข้อความกลับ LINE (ถ้ามีข้อความ)
    if reply_text:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))