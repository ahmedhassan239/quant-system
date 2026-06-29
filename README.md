# 🚀 Quant Trading System - Documentation

## 1. Project Overview
نظام تداول كمي آلي يعتمد على التحليل الفني (SMC + RSI) لاصطياد صفقات (Intraday) بهدف ربح يومي (1.5% - 2%) مع إغلاق كافة الصفقات بنهاية اليوم لحماية الأرباح.

## 2. Tech Stack Architecture
- **Database:** PostgreSQL (Persistent Storage)
- **Cache / Live Data:** Redis (High-speed temporary storage)
- **Quant Engine:** Python (Pandas, Pandas-TA, Binance API)
- **Backend API:** PHP Laravel (System management, API endpoints)
- **Frontend Dashboard:** Vue.js (Real-time monitoring and analytics)
- **Infrastructure:** Docker Compose (Fully containerized)

## 3. Completed Milestones (✅)
- [x] بناء الهيكل الأساسي للحاويات (Docker Compose).
- [x] إعداد قاعدة البيانات (PostgreSQL) وواجهة الإدارة (pgAdmin).
- [x] إعداد خادم التخزين المؤقت (Redis).
- [x] تهيئة بيئة Laravel و Vue.js المبدئية.
- [x] تجهيز بيئة تشغيل محرك Python وتحديد استراتيجية التداول.

## 4. Current Milestone (⏳)
- [ ] بناء نظام سحب البيانات (Data Ingestion Pipeline) من Binance وحفظها في قاعدة البيانات.