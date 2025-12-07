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
model = genai.GenerativeModel('gemini-flash-latest')

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
        reply_text = (f"🤖 คู่มือการใช้งาน:\n\n"
                      f"1. เพิ่ม: [ศัพท์] -> จดศัพท์ใหม่\n"
                      f"2. ลบคำศัพท์: [ศัพท์] -> ลบศัพท์และประวัติทิ้ง\n"
                      f"3. คลังคำศัพท์ -> ดูรายการศัพท์\n"
                      f"4. พิมพ์ประโยคอังกฤษ -> ส่งการบ้าน")

    # 2. ดูคลัง
    elif user_msg == "คลังคำศัพท์":
        try:
            response = supabase.table("vocab").select("word").order("id", desc=True).limit(20).execute()
            words = response.data
            if not words:
                reply_text = "📭 คลังว่างเปล่า ลองพิมพ์ 'เพิ่ม: [ศัพท์]' ดูสิ!"
            else:
                word_list = "\n".join([f"- {item['word']}" for item in words])
                reply_text = f"📚 ศัพท์ล่าสุด ({len(words)} คำ):\n\n{word_list}"
        except:
            reply_text = "ดึงข้อมูลพลาด ลองใหม่นะครับ"

    # 3. ลบคำศัพท์ (แก้บั๊ก Foreign Key แล้ว ✅)
    elif user_msg.startswith("ลบคำศัพท์:"):
        try:
            word_to_delete = user_msg.split(":", 1)[1].strip()
            if not word_to_delete:
                reply_text = "⚠️ ระบุคำที่จะลบหลัง : ด้วยนะครับ"
            else:
                # Step 1: หา ID
                search_res = supabase.table("vocab").select("id, word").ilike("word", word_to_delete).execute()
                
                if not search_res.data:
                    reply_text = f"❌ หาคำว่า '{word_to_delete}' ไม่เจอครับ"
                else:
                    target_id = search_res.data[0]['id']
                    real_word = search_res.data[0]['word']

                    # Step 2: ลบ Logs ก่อน
                    supabase.table("user_logs").delete().eq("vocab_id", target_id).execute()

                    # Step 3: ลบ Vocab
                    supabase.table("vocab").delete().eq("id", target_id).execute()
                    
                    reply_text = f"🗑️ ล้างบาง! ลบคำว่า '{real_word}' เรียบร้อยครับ"
        except Exception as e:
            print(f"Delete Error: {e}")
            reply_text = f"❌ ระบบลบขัดข้อง: {str(e)}"

    # 4. เพิ่มศัพท์
    elif user_msg.lower().startswith(("เพิ่ม:", "add:")):
        try:
            word = user_msg.split(":", 1)[1].strip()
        except:
            word = ""  
        if not word:
            reply_text = "อย่าลืมใส่ศัพท์หลัง : นะครับ"
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
                print(e)
                reply_text = "ระบบรวนนิดหน่อย ลองใหม่นะครับ"

    # 5. ตรวจการบ้าน 
    else:
        reply_text = "ขอตรวจแป๊บ... 🧐"
        try:
            prompt = (f"User sentence: '{user_msg}'\n"
                      f"Task: Identify main word, Check context usage, IGNORE minor punctuation/caps.\n"
                      f"Format:\nWord: [Main word]\nCorrect: [Yes/No]\nFeedback: [Thai encouragement]")
            
            res = model.generate_content(prompt)
            ai_text = res.text.strip()
            
            detected_word, is_correct, feedback = "", False, ""
            for line in ai_text.split('\n'):
                if line.startswith("Word:"): detected_word = line.replace("Word:", "").strip()
                elif line.startswith("Correct:"): is_correct = "Yes" in line
                elif line.startswith("Feedback:"): feedback = line.replace("Feedback:", "").strip()

            vocab_data = supabase.table("vocab").select("id").ilike("word", detected_word).execute().data
            vocab_id = vocab_data[0]['id'] if vocab_data else None
            
            supabase.table("user_logs").insert({
                "user_id": user_id,
                "vocab_id": vocab_id,
                "user_answer": user_msg,
                "is_correct": is_correct
            }).execute()

            icon = "🎉 แจ๋วเลย!" if is_correct else "🤏 นิดนึงนะ..."
            reply_text = f"{icon}\nศัพท์: {detected_word}\nผล: {'✅ ผ่าน' if is_correct else '❌ แก้ไข'}\n\n💬 {feedback}"
        except Exception as e:
            reply_text = "ครู AI มึนหัวนิดหน่อย ส่งใหม่นะครับ"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))