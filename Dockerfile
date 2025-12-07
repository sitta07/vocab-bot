# ใช้ Python Image ตัวเล็กๆ จะได้เบา
FROM python:3.10-slim
# ตั้ง Folder ทำงาน
WORKDIR /app

# ลง Library
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# เอา Code ใส่เข้าไป
COPY . .

# เปิด Port 8080 (Cloud Run ชอบ Port นี้)
ENV PORT=8080

# คำสั่ง Run App
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]