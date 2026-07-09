#!/bin/bash
set -e

echo "Deploying Vue Frontend..."
mkdir -p /var/www/quant-dashboard
cp -R frontend-vue/dist/* /var/www/quant-dashboard/
chown -R www-data:www-data /var/www/quant-dashboard

echo "Setting permissions for Laravel storage..."
cd api-laravel
chown -R www-data:www-data storage bootstrap/cache
chmod -R 775 storage bootstrap/cache

# Note: The following commands assume PHP is installed on the host. 
# If they fail due to a missing driver, you must install php-pgsql first:
# sudo apt-get install php-pgsql
php artisan optimize:clear
php artisan config:cache
php artisan route:cache
cd ..

echo "Setting up Systemd Service..."
cp quant-api.service /etc/systemd/system/quant-api.service

systemctl daemon-reload
systemctl enable quant-api
systemctl restart quant-api

echo "Setting up Nginx Reverse Proxy..."
cp quant-bot.conf /etc/nginx/sites-available/quant-bot
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/quant-bot /etc/nginx/sites-enabled/
nginx -t
systemctl restart nginx

echo "Deployment complete!"
