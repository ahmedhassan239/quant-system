<script setup>
import { ref, onMounted } from 'vue';
import axios from 'axios';

const tradeLogs = ref([]);
const loading = ref(true);

const fetchLogs = async () => {
  try {
    // هنا بنكلم الـ Laravel API بتاعنا
    const response = await axios.get('http://localhost:8000/api/trade-logs');
    tradeLogs.value = response.data.data;
  } catch (error) {
    console.error("Error fetching trade logs:", error);
  } finally {
    loading.value = false;
  }
};

onMounted(() => {
  fetchLogs();
});
</script>

<template>
  <div class="min-h-screen p-8 bg-gray-900 text-white font-sans">
    <header class="mb-10 border-b border-gray-700 pb-6">
      <h1 class="text-4xl font-bold text-blue-400 tracking-wide">Quant System Dashboard</h1>
      <p class="text-gray-400 mt-2 text-lg">Live AI Trading Decisions & XAI Rationale</p>
    </header>

    <div v-if="loading" class="text-center text-gray-400 py-20 text-xl animate-pulse">
      Loading Market Data...
    </div>

    <div v-else class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-8">
      <div v-for="log in tradeLogs" :key="log.id" class="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow-2xl hover:border-blue-500 transition-all duration-300">
        
        <div class="flex justify-between items-center mb-6">
          <span class="text-2xl font-black text-yellow-400">{{ log.symbol }}</span>
          <span class="px-4 py-1.5 rounded-full text-xs font-bold tracking-wider"
                :class="log.action_type === 'EXECUTE_MARKET_BUY' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'">
            {{ log.action_type }}
          </span>
        </div>

        <div class="mb-5 bg-gray-900 p-4 rounded-lg">
          <h3 class="text-xs font-bold text-gray-500 uppercase mb-2 tracking-widest">Market Structure</h3>
          <p class="text-sm text-gray-300 mb-1">
            Trend: <span class="text-green-400 font-semibold">{{ log.rationale.market_structure.status }}</span>
          </p>
          <p class="text-sm text-gray-300">
            Pattern: <span class="text-white">{{ log.rationale.market_structure.detected_pattern }}</span>
          </p>
        </div>

        <div class="mb-5 bg-gray-900 p-4 rounded-lg">
          <h3 class="text-xs font-bold text-gray-500 uppercase mb-2 tracking-widest">Order Flow Data</h3>
          <p class="text-sm text-gray-300 mb-1">
            Whale Wall At: <span class="font-mono text-blue-300">{{ log.rationale.order_flow.whale_wall_detected_at }}</span>
          </p>
          <p class="text-sm text-gray-300">
            Absorption: <span class="text-white">{{ log.rationale.order_flow.limit_order_absorption }}</span>
          </p>
        </div>

        <div class="pt-4 border-t border-gray-700 mt-auto">
          <h3 class="text-xs font-bold text-gray-500 uppercase mb-2 tracking-widest">AI News Sentiment</h3>
          <p class="text-sm italic text-gray-400 mb-3 border-l-2 border-indigo-500 pl-3">
            "{{ log.rationale.news_intelligence.headline }}"
          </p>
          <div class="flex justify-between items-center">
            <span class="text-xs text-gray-500">{{ log.rationale.news_intelligence.ai_model_used }}</span>
            <span class="text-sm font-bold text-indigo-400">Score: {{ log.rationale.news_intelligence.sentiment_score }}</span>
          </div>
        </div>

      </div>
    </div>
  </div>
</template>