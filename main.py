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

# Config
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Db Error: {e}")

def save_user(user_id):
    try:
        supabase.table("users").upsert({"user_id": user_id}, on_conflict="user_id").execute()
    except:
        pass

@app.get("/")
def health_check():
    return {"status": "ok"}

@app.get("/broadcast-quiz")
def broadcast_quiz():
    try:
        users = supabase.table("users").select("user_id").execute().data
        if not users: return {"msg": "No users"}

        vocab_list = supabase.table("vocab").select("*").limit(100).execute().data
        if not vocab_list: return {"msg": "No vocab"}
            
        selected = random.choice(vocab_list)
        word = selected['word']
        meaning = selected.get('meaning', '-')

        msg = (f"🔥 ภารกิจเช้านี้!\n\n"
               f"คำศัพท์: {word}\n"
               f"ความหมาย: {meaning}\n\n"
               f"👉 จงแต่งประโยคภาษาอังกฤษโดยใช้คำนี้ส่งกลับมา!")

        for user in users:
            line_bot_api.push_message(user['user_id'], TextSendMessage(text=msg))
            
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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id
    save_user(user_id)
    
    # --- 1️⃣ เมนูคำสั่ง ---
    if user_msg == "คำสั่ง":
        reply_text = (f"🤖 คู่มือการใช้งานบอท:\n\n"
                      f"1. เพิ่ม: [คำศัพท์]\n"
                      f"   👉 จดศัพท์ใหม่พร้อมคำแปล\n"
                      f"   ตัวอย่าง: เพิ่ม: Resilience\n\n"
                      f"2. ลบคำศัพท์: [คำศัพท์]\n"
                      f"   👉 ลบคำศัพท์ออกจากคลัง\n"
                      f"   ตัวอย่าง: ลบคำศัพท์: Cat\n\n"
                      f"3. คลังคำศัพท์\n"
                      f"   👉 ดูรายการศัพท์ล่าสุด\n\n"
                      f"4. พิมพ์ประโยคภาษาอังกฤษมาเลย\n"
                      f"   👉 เพื่อให้ AI ตรวจแกรมมาร์")

    # --- 2️⃣ ดูคลังคำศัพท์ ---
    elif user_msg == "คลังคำศัพท์":
        try:
            # ดึง 20 คำล่าสุด (เรียงจากใหม่ไปเก่า)
            response = supabase.table("vocab").select("word").order("id", desc=True).limit(20).execute()
            words = response.data
            
            if not words:
                reply_text = "📭 คลังคำศัพท์ยังว่างอยู่ครับ ลองพิมพ์ 'เพิ่ม: [ศัพท์]' ดูสิ!"
            else:
                word_list = "\n".join([f"- {item['word']}" for item in words])
                count = len(words)
                reply_text = f"📚 คำศัพท์ล่าสุด ({count} คำ):\n\n{word_list}"
        except Exception as e:
            reply_text = "ดึงข้อมูลไม่สำเร็จ ลองใหม่อีกทีนะครับ"

    # --- 3️⃣ ลบคำศัพท์ ---
    elif user_msg.startswith("ลบคำศัพท์:"):
        try:
            word_to_delete = user_msg.split(":", 1)[1].strip()
            if not word_to_delete:
                reply_text = "ระบุคำที่จะลบหลัง : ด้วยครับ"
            else:
                # สั่งลบจาก DB (ใช้ ilike เพื่อให้ case-insensitive เช่น Cat กับ cat ถือว่าเหมือนกัน)
                # หมายเหตุ: Supabase delete จะไม่ return error ถ้าไม่เจอ record แต่เราเช็ค count ได้
                result = supabase.table("vocab").delete().ilike("word", word_to_delete).execute()
                
                # เช็คว่าลบไปกี่แถว
                if len(result.data) > 0:
                    reply_text = f"🗑️ ลบคำว่า '{word_to_delete}' ออกจากคลังแล้วครับ"
                else:
                    reply_text = f"หาคำว่า '{word_to_delete}' ไม่เจอครับ (อาจจะลบไปแล้วหรือสะกดผิด)"
        except Exception as e:
            print(e)
            reply_text = "ระบบลบขัดข้องครับ"

    # --- 4️⃣ เพิ่มคำศัพท์ ---
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
                reply_text = "Error, try again."

    # --- 5️⃣ โหมดตรวจการบ้าน (Default) ---
    else:
        reply_text = "กำลังตรวจการบ้านครับ... 📝"
        try:
            prompt = (f"Check grammar: '{user_msg}'. "
                      f"Format:\nWord: [Main vocab]\nCorrect: [Yes/No]\nFeedback: [Comment]")
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

            icon = "🎉 เก่งมาก!" if is_correct else "💪 สู้ๆ เกือบถูกแล้ว!"
            reply_text = f"{icon}\nศัพท์หลัก: {detected_word}\nผล: {'✅ ผ่าน' if is_correct else '❌ แก้ไข'}\n\nคอมเมนต์: {feedback}"
        except Exception as e:
            reply_text = "คุณครู AI มึนหัวนิดหน่อย ลองส่งใหม่นะครับ"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))