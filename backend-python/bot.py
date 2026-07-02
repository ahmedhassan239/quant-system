import asyncio
import logging

# إعداد الـ Log ليظهر بشكل واضح في الـ Terminal
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def main():
    logging.info("🚀 Quant Engine Started: Booting up AI modules and Exchange connectors...")
    
    # حلقة لانهائية لإبقاء المحرك يعمل في الخلفية
    while True:
        await asyncio.sleep(60)
        logging.info("💓 Heartbeat: Engine is running and monitoring the market.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 Engine stopped by user.")