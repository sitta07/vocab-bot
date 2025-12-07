FROM python:3.10-slim

# ตั้ง Folder งาน
WORKDIR /app

# ก๊อปไฟล์ requirements และติดตั้ง
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ก๊อปไฟล์ Code ทั้งหมด
COPY . .

# เปิด Port 8080
ENV PORT=8080

# คำสั่ง Run App
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]