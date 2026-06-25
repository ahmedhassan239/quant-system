FROM php:8.4-cli

# تثبيت متطلبات النظام وإضافات قواعد البيانات
RUN apt-get update && apt-get install -y \
    libpq-dev \
    unzip \
    git \
    && docker-php-ext-install pdo pdo_pgsql

# نسخ Composer من أحدث صورة
COPY --from=composer:latest /usr/bin/composer /usr/bin/composer

# تحديد مسار العمل
WORKDIR /app

# نسخ ملفات المشروع
COPY . .

# تثبيت الحزم 
RUN composer install --no-dev --optimize-autoloader

# إعطاء الصلاحيات لمجلدات الكاش
RUN chown -R www-data:www-data storage bootstrap/cache

# تشغيل السيرفر
CMD ["php", "artisan", "serve", "--host=0.0.0.0", "--port=8000"]