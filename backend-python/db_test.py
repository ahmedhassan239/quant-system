import psycopg2
import json
from datetime import datetime

# إعدادات الاتصال بقاعدة البيانات (نستخدم localhost لأننا سنشغله يدوياً من خارج الـ Container الآن)
DB_CONFIG = {
    'dbname': 'quant_db',
    'user': 'quant_user',
    'password': 'secret123',
    'host': 'postgres_db', # التعديل هنا: استخدام اسم الحاوية بدلاً من 127.0.0.1
    'port': '5432'
}

# هذا الـ JSON يحاكي مخرجات الذكاء الاصطناعي (FinBERT) ومصفوفة القرار
rationale_data = {
    "market_structure": {
        "status": "BULLISH_SHIFT",
        "timeframe": "5m",
        "liquidity_pool_hit": True,
        "detected_pattern": "Fair Value Gap (FVG) Reversal"
    },
    "order_flow": {
        "delta_volume_spike_x": 3.4,
        "limit_order_absorption": "HIGH_BUY_PRESSURE",
        "whale_wall_detected_at": 149.50
    },
    "news_intelligence": {
        "source_scraped": "Binance Announcements / Bloomberg",
        "headline": "Network upgrade consensus achieved with major validators support",
        "ai_model_used": "FinBERT",
        "sentiment_score": 0.86,
        "classification": "STRONG_BULLISH"
    }
}

try:
    print("⏳ Connecting to PostgreSQL Database...")
    # الاتصال بالقاعدة
    conn = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # إنشاء حساب وهمي أولاً لكي نتجنب خطأ الـ Foreign Key
    cursor.execute("""
        INSERT INTO accounts (account_name, encrypted_api_key, encrypted_secret_key, balance, created_at, updated_at) 
        VALUES ('Test_Account_1', 'dummy_api_key', 'dummy_secret', 1000.00, NOW(), NOW())
        RETURNING id;
    """)
    account_id = cursor.fetchone()[0]
    print(f"✅ Dummy Account created with ID: {account_id}")

    # إدخال قرار البوت في جدول الـ Logs
    insert_query = """
        INSERT INTO trade_logs (account_id, symbol, action_type, rationale, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    
    # دمج البيانات مع كائن الـ JSONB
    cursor.execute(insert_query, (
        account_id,
        'SOLUSDT',
        'EXECUTE_MARKET_BUY',
        json.dumps(rationale_data), # تحويل البايثون ديكشنري إلى JSON String ليقبله الـ JSONB
        datetime.now(),
        datetime.now()
    ))
    
    conn.commit()
    print("🚀 SUCCESS: The Quant Engine successfully logged a trade rationale (JSONB) into the database!")

except Exception as e:
    print(f"❌ Error: {e}")

finally:
    if 'conn' in locals():
        cursor.close()
        conn.close()
        print("🔒 Database connection closed.")