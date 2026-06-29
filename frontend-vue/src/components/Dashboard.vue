<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue';
import axios from 'axios';

const marketData = ref([]);
const latestSignals = ref([]);
const loading = ref(true);
const error = ref(null);
let pollInterval = null;

const fetchData = async () => {
  error.value = null;
  try {
    const response = await axios.get('http://localhost:8000/api/dashboard');
    marketData.value = response.data.market_data || [];
    latestSignals.value = response.data.latest_signals || [];
  } catch (err) {
    console.error("Error fetching data:", err);
    error.value = "Failed to connect to the backend API. Ensure the Laravel server is running.";
  } finally {
    loading.value = false;
  }
};

onMounted(() => {
  fetchData();
  // Fetch real-time data dynamically every 5 seconds
  pollInterval = setInterval(fetchData, 5000);
});

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval);
});

const latestSignal = computed(() => {
  return latestSignals.value.length > 0 ? latestSignals.value[0] : null;
});

const reversedMarketData = computed(() => {
  return [...marketData.value].reverse();
});

const formatNumber = (num, decimals = 2) => {
  if (num === null || num === undefined) return 'N/A';
  return Number(num).toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
};

const formatTime = (timestamp) => {
  if (!timestamp) return 'N/A';
  const date = new Date(timestamp);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};

const getSignalBadgeClass = (decision) => {
  if (!decision) return 'bg-gray-600 text-gray-300 border-gray-500';
  const d = decision.toUpperCase();
  if (d === 'BUY') return 'bg-green-900/40 text-green-400 border-green-500/50 shadow-[0_0_20px_rgba(34,197,94,0.15)]';
  if (d === 'SELL') return 'bg-red-900/40 text-red-400 border-red-500/50 shadow-[0_0_20px_rgba(239,68,68,0.15)]';
  return 'bg-gray-800 text-gray-300 border-gray-600';
};

const getRsiColor = (rsi, isBg = false) => {
  if (rsi === null || rsi === undefined) return isBg ? 'bg-gray-500' : 'text-gray-500';
  if (rsi >= 70) return isBg ? 'bg-red-500' : 'text-red-400';
  if (rsi <= 30) return isBg ? 'bg-green-500' : 'text-green-400';
  return isBg ? 'bg-blue-400' : 'text-blue-300';
};

const getPriceColor = (open, close) => {
  if (close > open) return 'text-green-400';
  if (close < open) return 'text-red-400';
  return 'text-gray-300';
};
</script>

<template>
  <div class="min-h-screen bg-[#0b0e14] text-gray-100 p-4 md:p-8 font-sans">
    
    <div v-if="loading && !latestSignal" class="flex flex-col justify-center items-center h-screen space-y-4">
      <div class="animate-spin rounded-full h-14 w-14 border-t-2 border-b-2 border-blue-500"></div>
      <p class="text-gray-400 tracking-widest text-sm uppercase">Initializing Quant Engine...</p>
    </div>
    
    <div v-else-if="error && !latestSignal" class="bg-red-950/40 border border-red-900 text-red-200 p-8 rounded-2xl text-center max-w-2xl mx-auto mt-20 shadow-2xl">
      <svg class="w-12 h-12 text-red-500 mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
      <h3 class="text-xl font-bold mb-2">API Connection Failed</h3>
      <p class="text-red-400/80 mb-6">{{ error }}</p>
      <button @click="fetchData" class="px-8 py-2.5 bg-red-600/20 border border-red-600/50 hover:bg-red-600 hover:text-white text-red-400 rounded-lg transition-all duration-300 font-medium tracking-wide">Retry Connection</button>
    </div>

    <div v-else class="max-w-7xl mx-auto space-y-8 animate-fade-in">
      <!-- Header -->
      <header class="flex flex-col md:flex-row justify-between items-start md:items-center pb-6 border-b border-gray-800/80 gap-4">
        <div>
          <h1 class="text-3xl font-black bg-clip-text text-transparent bg-gradient-to-r from-blue-400 via-indigo-400 to-purple-400 tracking-tight">
            Market Intelligence
          </h1>
          <p class="text-gray-500 mt-1 text-sm font-medium tracking-wide">Real-time Quantitative Analysis Dashboard</p>
        </div>
        <div class="flex items-center space-x-2 text-xs font-semibold uppercase tracking-wider text-green-400 bg-green-950/30 px-4 py-2 rounded-full border border-green-900/50">
          <span class="relative flex h-2.5 w-2.5 mr-1">
            <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
            <span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-green-500"></span>
          </span>
          Live Feed Active
        </div>
      </header>

      <!-- Hero Section: Latest Signal -->
      <section v-if="latestSignal" class="bg-[#131722] rounded-3xl border border-gray-800 shadow-2xl overflow-hidden relative group">
        <div class="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-blue-500 via-indigo-500 to-purple-500 opacity-80 group-hover:opacity-100 transition-opacity"></div>
        <div class="p-8 md:p-10">
          <div class="flex flex-col lg:flex-row justify-between items-start lg:items-center gap-6 mb-10">
            <div class="flex items-end gap-5">
              <div class="text-5xl md:text-6xl font-black tracking-tighter text-white">{{ latestSignal.symbol || 'UNKNOWN' }}</div>
              <div class="text-2xl md:text-3xl font-bold text-gray-400 mb-1">${{ formatNumber(latestSignal.current_price, 4) }}</div>
            </div>
            <div :class="['px-8 py-3 rounded-xl border-2 text-xl font-black tracking-[0.2em] uppercase transition-all duration-300', getSignalBadgeClass(latestSignal.decision)]">
              {{ latestSignal.decision || 'WAIT' }}
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            <!-- RSI Card -->
            <div class="bg-gray-900/40 p-6 rounded-2xl border border-gray-800/60 hover:border-gray-700 transition-colors flex flex-col justify-between">
              <div class="text-gray-500 text-xs font-bold uppercase tracking-widest mb-4">Smoothed RSI</div>
              <div>
                <div class="flex items-end gap-2 mb-3">
                  <div class="text-4xl font-black tracking-tight" :class="getRsiColor(latestSignal.rsi)">{{ formatNumber(latestSignal.rsi, 1) }}</div>
                </div>
                <div class="w-full bg-gray-800/80 rounded-full h-2 overflow-hidden">
                  <div class="h-full rounded-full transition-all duration-1000 ease-out" :class="getRsiColor(latestSignal.rsi, true)" :style="`width: ${Math.min(Math.max(latestSignal.rsi, 0), 100)}%`"></div>
                </div>
              </div>
            </div>

            <!-- Bullish OB -->
            <div class="bg-gray-900/40 p-6 rounded-2xl border border-gray-800/60 hover:border-green-900/50 transition-colors flex flex-col justify-between">
              <div class="text-gray-500 text-xs font-bold uppercase tracking-widest mb-4 flex items-center gap-2">
                <span class="w-2.5 h-2.5 rounded-full bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]"></span> Bullish OB
              </div>
              <div class="space-y-3">
                <div class="flex justify-between items-center border-b border-gray-800/50 pb-2">
                  <span class="text-sm text-gray-500 font-medium">High</span>
                  <span class="text-base font-mono font-semibold text-green-400">${{ formatNumber(latestSignal.bullish_ob_high, 4) }}</span>
                </div>
                <div class="flex justify-between items-center">
                  <span class="text-sm text-gray-500 font-medium">Low</span>
                  <span class="text-base font-mono font-semibold text-green-500">${{ formatNumber(latestSignal.bullish_ob_low, 4) }}</span>
                </div>
              </div>
            </div>

            <!-- Bearish OB -->
            <div class="bg-gray-900/40 p-6 rounded-2xl border border-gray-800/60 hover:border-red-900/50 transition-colors flex flex-col justify-between">
              <div class="text-gray-500 text-xs font-bold uppercase tracking-widest mb-4 flex items-center gap-2">
                <span class="w-2.5 h-2.5 rounded-full bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.6)]"></span> Bearish OB
              </div>
              <div class="space-y-3">
                <div class="flex justify-between items-center border-b border-gray-800/50 pb-2">
                  <span class="text-sm text-gray-500 font-medium">High</span>
                  <span class="text-base font-mono font-semibold text-red-400">${{ formatNumber(latestSignal.bearish_ob_high, 4) }}</span>
                </div>
                <div class="flex justify-between items-center">
                  <span class="text-sm text-gray-500 font-medium">Low</span>
                  <span class="text-base font-mono font-semibold text-red-500">${{ formatNumber(latestSignal.bearish_ob_low, 4) }}</span>
                </div>
              </div>
            </div>

            <!-- Timeframe/Meta -->
            <div class="bg-gray-900/40 p-6 rounded-2xl border border-gray-800/60 flex flex-col justify-between">
              <div class="text-gray-500 text-xs font-bold uppercase tracking-widest mb-4">Metadata</div>
              <div class="space-y-4">
                <div class="flex flex-col">
                  <span class="text-xs text-gray-600 font-medium mb-1">Timeframe</span>
                  <span class="text-indigo-300 font-bold bg-indigo-900/20 px-3 py-1 rounded-lg w-fit border border-indigo-900/30">{{ latestSignal.timeframe || '15m' }}</span>
                </div>
                <div class="flex flex-col">
                  <span class="text-xs text-gray-600 font-medium mb-1">Last Update</span>
                  <span class="text-gray-300 text-sm font-mono bg-gray-800/50 px-3 py-1 rounded-lg w-fit border border-gray-700/50">{{ formatTime(latestSignal.timestamp) }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- Market Data Table -->
      <section class="bg-[#131722] rounded-3xl border border-gray-800 shadow-xl overflow-hidden">
        <div class="p-6 border-b border-gray-800/60 flex justify-between items-center bg-gray-900/40">
          <h2 class="text-lg font-bold text-gray-200 tracking-wide">Price Action History</h2>
          <span class="text-xs text-gray-500 font-mono font-medium px-3 py-1 bg-gray-800/50 rounded-full border border-gray-700/50">{{ marketData.length }} RECORDS</span>
        </div>
        
        <div class="overflow-x-auto">
          <table class="w-full text-sm text-left whitespace-nowrap">
            <thead class="text-xs text-gray-500 uppercase bg-gray-900/20 border-b border-gray-800/80">
              <tr>
                <th scope="col" class="px-8 py-5 font-bold tracking-widest">Time</th>
                <th scope="col" class="px-8 py-5 font-bold tracking-widest text-right">Open</th>
                <th scope="col" class="px-8 py-5 font-bold tracking-widest text-right">High</th>
                <th scope="col" class="px-8 py-5 font-bold tracking-widest text-right">Low</th>
                <th scope="col" class="px-8 py-5 font-bold tracking-widest text-right">Close</th>
                <th scope="col" class="px-8 py-5 font-bold tracking-widest text-right">Volume</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-800/40">
              <tr v-for="row in reversedMarketData" :key="row.timestamp || row.id" class="hover:bg-gray-800/40 transition-colors">
                <td class="px-8 py-4 font-mono text-gray-400">
                  {{ formatTime(row.timestamp) }}
                </td>
                <td class="px-8 py-4 font-mono text-right text-gray-300">
                  ${{ formatNumber(row.open, 4) }}
                </td>
                <td class="px-8 py-4 font-mono text-right text-gray-400">
                  ${{ formatNumber(row.high, 4) }}
                </td>
                <td class="px-8 py-4 font-mono text-right text-gray-400">
                  ${{ formatNumber(row.low, 4) }}
                </td>
                <td :class="['px-8 py-4 font-mono text-right font-bold', getPriceColor(row.open, row.close)]">
                  ${{ formatNumber(row.close, 4) }}
                </td>
                <td class="px-8 py-4 font-mono text-right text-gray-500">
                  {{ formatNumber(row.volume, 0) }}
                </td>
              </tr>
              <tr v-if="marketData.length === 0">
                <td colspan="6" class="px-8 py-16 text-center text-gray-500 font-medium tracking-wide">
                  No market data available in the current feed.
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      
    </div>
  </div>
</template>

<style scoped>
.animate-fade-in {
  animation: fadeIn 0.5s ease-out;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
