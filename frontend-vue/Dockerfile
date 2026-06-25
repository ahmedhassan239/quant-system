# استخدام بيئة Node.js
FROM node:20-alpine

# تحديد مسار العمل
WORKDIR /app

# نسخ ملفات الـ package أولاً لتسريع عملية البناء (Caching)
COPY package*.json ./

# تسطيب الحزم والمكتبات (بما فيها vite) داخل الحاوية
RUN npm install

# نسخ باقي ملفات المشروع
COPY . .

# تشغيل سيرفر التطوير
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]