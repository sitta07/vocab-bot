import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv

# โหลด Environment Variables
load_dotenv()

app = FastAPI()

# ดึงค่า Key จาก Environment (ห้าม Hardcode เด็ดขาด!)
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')

# เช็คว่า Key มาครบไหม (กันพลาด)
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise ValueError("กรุณาใส่ LINE_CHANNEL_ACCESS_TOKEN และ LINE_CHANNEL_SECRET ใน .env หรือ System Env")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.get("/")
def health_check():
    return {"status": "ok", "msg": "Bot is alive!"}

@app.post("/callback")
async def callback(request: Request):
    # รับ Header จาก Line
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_decode = body.decode('utf-8')

    try:
        # ยืนยันว่ามาจาก Line จริงๆ
        handler.handle(body_decode, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"

# ฟังก์ชันตอบกลับข้อความ (Logic อยู่ตรงนี้)
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    # ตอบกลับแบบ Echo (พิมพ์อะไร ตอบอันนั้น)
    reply_msg = f"ได้รับข้อความ: {user_msg}"
    
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_msg)
    )