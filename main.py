import os
import random
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# --- CONFIG ---
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

if not all([LINE_ACCESS_TOKEN, LINE_SECRET, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("⚠️ Warning: Environment variables are missing!")

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

genai.configure(api_key=GEMINI_API_KEY)
# แนะนำให้ใช้ gemini-1.5-flash หรือ model ที่ใหม่ที่สุดที่มีเพื่อความแม่นยำทางภาษา
model = genai.GenerativeModel('gemini-1.5-flash') 

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Error: {e}")

# --- HELPER ---
def save_user(user_id):
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except Exception as e:
        print(f"Save user error: {e}")

# --- API ---
@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Bot is ready!"}

@app.get("/broadcast-quiz")
def broadcast_quiz():
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
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

# --- MAIN LOGIC ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id
    save_user(user_id)
    
    # 1. เมนูคำสั่ง
    if user_msg == "คำสั่ง":
        reply_text = (f"🤖 เมนูหลัก:\n\n"
                      f"1. เพิ่ม: [ศัพท์] -> จดศัพท์ใหม่\n"
                      f"2. ลบคำศัพท์: [ศัพท์] -> ลบศัพท์ทิ้ง\n"
                      f"3. คลังคำศัพท์ -> ดูรายการศัพท์\n"
                      f"4. (พิมพ์ประโยคอังกฤษ) -> ส่งการบ้าน")

    # 2. ดูคลัง
    elif user_msg == "คลังคำศัพท์":
        try:
            response = supabase.table("vocab").select("word").order("id", desc=True).limit(20).execute()
            words = response.data
            if not words:
                reply_text = "📭 คลังว่างเปล่า พิมพ์ 'เพิ่ม: [ศัพท์]' ได้เลย!"
            else:
                word_list = "\n".join([f"- {item['word']}" for item in words])
                reply_text = f"📚 ศัพท์ล่าสุด ({len(words)}):\n\n{word_list}"
        except:
            reply_text = "ดึงข้อมูลพลาด ลองใหม่นะครับ"

    # 3. ลบคำศัพท์
    elif user_msg.startswith("ลบคำศัพท์:"):
        try:
            word_to_delete = user_msg.split(":", 1)[1].strip()
            if not word_to_delete:
                reply_text = "⚠️ ระบุศัพท์หลัง : ด้วยนะครับ"
            else:
                search_res = supabase.table("vocab").select("id, word").ilike("word", word_to_delete).execute()
                if not search_res.data:
                    reply_text = f"❌ ไม่เจอคำว่า '{word_to_delete}' ครับ"
                else:
                    target_id = search_res.data[0]['id']
                    real_word = search_res.data[0]['word']
                    supabase.table("user_logs").delete().eq("vocab_id", target_id).execute()
                    supabase.table("vocab").delete().eq("id", target_id).execute()
                    reply_text = f"🗑️ ลบ '{real_word}' เรียบร้อย"
        except Exception as e:
            reply_text = f"❌ ลบไม่สำเร็จ: {str(e)}"

    # 4. เพิ่มศัพท์
    elif user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
        except:
            word = ""  
        if not word:
            reply_text = "⚠️ ใส่ศัพท์หลัง : ด้วยนะครับ"
        else:
            try:
                prompt = (f"Word: '{word}'. Translate (EN<->TH), Meaning, Example. "
                          f"Format:\nMeaning: ...\nExample: ...")
                res = model.generate_content(prompt)
                text = res.text.strip()
                meaning, example = "-", "-"
                for line in text.split('\n'):
                    if line.startswith("Meaning:"): meaning = line.replace("Meaning:", "").strip()
                    elif line.startswith("Example:"): example = line.replace("Example:", "").strip()

                supabase.table("vocab").insert({"word": word, "meaning": meaning, "example_sentence": example}).execute()
                reply_text = f"✅ จดแล้ว!\n🔤 {word}\n📖 {meaning}\n🗣️ {example}"
            except Exception as e:
                reply_text = "ระบบรวนนิดหน่อย ลองใหม่นะครับ"

    # 5. ตรวจการบ้าน (Logic ใหม่: แก้ไข + แปล)
    else:
        # กรองข้อความสั้นเกินไป
        if len(user_msg) < 3: 
            return

        try:
            # Prompt สั่งให้ AI ตรวจ + แก้ + แปล
            prompt = (f"User input: '{user_msg}'\n"
                      f"Role: English Teacher.\n"
                      f"Task: Evaluate if this is a valid English sentence trying to use a vocabulary word.\n"
                      f"Rules:\n"
                      f"1. If input is gibberish, greeting (Hi/Hello), or not a sentence -> Respond 'SKIP'\n"
                      f"2. If Incorrect -> Provide 'Correction' (rewrite correctly) AND 'Feedback' (Explain in Thai why it's wrong + Translate the correction).\n"
                      f"3. If Correct -> 'Correction' is '-' AND 'Feedback' is praise in Thai.\n"
                      f"Format:\nWord: [Main word]\nCorrect: [Yes/No]\nCorrection: [Corrected Sentence]\nFeedback: [Thai explanation]")
            
            res = model.generate_content(prompt)
            ai_text = res.text.strip()

            if "SKIP" in ai_text:
                return

            detected_word, is_correct, correction, feedback = "Unknown", False, "-", ""
            
            # Parsing Response
            for line in ai_text.split('\n'):
                line = line.strip()
                if line.startswith("Word:"): detected_word = line.replace("Word:", "").strip()
                elif line.startswith("Correct:"): is_correct = "Yes" in line
                elif line.startswith("Correction:"): correction = line.replace("Correction:", "").strip()
                elif line.startswith("Feedback:"): feedback = line.replace("Feedback:", "").strip()

            # Logging
            vocab_data = supabase.table("vocab").select("id").ilike("word", detected_word).execute().data
            vocab_id = vocab_data[0]['id'] if vocab_data else None
            
            supabase.table("user_logs").insert({
                "user_id": user_id,
                "vocab_id": vocab_id,
                "user_answer": user_msg,
                "is_correct": is_correct
            }).execute()

            # สร้างข้อความตอบกลับ
            if is_correct:
                reply_text = f"🎉 สุดยอด! ({detected_word})\n✅ ถูกต้องครับเป๊ะมาก\n\n💬 {feedback}"
            else:
                # กรณีผิด: แสดงสิ่งที่ถูก + คำแปล
                reply_text = (f"🤏 เกือบถูกแล้ว! ({detected_word})\n"
                              f"❌ ยังมีจุดแก้นิดหน่อยครับ\n\n"
                              f"💡 ประโยคที่ถูก: {correction}\n"
                              f"💬 คำแนะนำ: {feedback}")

        except Exception as e:
            print(f"AI Check Error: {e}")
            return # เงียบไปถ้า Error

    if reply_text:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))