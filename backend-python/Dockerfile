# استخدام نسخة بايثون خفيفة وسريعة
FROM python:3.10-slim

# منع بايثون من كتابة ملفات الـ cache المؤقتة لتقليل الحجم
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# تحديد مسار العمل داخل الـ Container
WORKDIR /app

# نسخ ملف الحزم وتثبيتها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي أكواد محرك البايثون
COPY . .

# أمر تشغيل البوت الأساسي
CMD ["python", "bot.py"]