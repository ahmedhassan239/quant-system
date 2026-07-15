<template>
  <div class="min-h-screen bg-gray-900 text-white p-8 font-sans">
    <div class="max-w-7xl mx-auto space-y-8">
      
      <!-- Header -->
      <header class="flex flex-col md:flex-row justify-between items-start md:items-center pb-6 border-b border-gray-700 gap-4">
        <h1 class="text-3xl font-extrabold bg-clip-text text-transparent bg-gradient-to-r from-blue-400 to-emerald-400">
          Quant Bot Control Panel
        </h1>
        <div class="px-4 py-2 bg-gray-800 rounded-full border border-gray-700 text-sm text-gray-300 shadow-sm flex items-center gap-2">
          Live Status
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.8)] animate-pulse"></span>
        </div>
      </header>

      <!-- Stats Overview Cards -->
      <div class="grid grid-cols-1 md:grid-cols-4 gap-6">
        
        <div class="bg-gray-800 rounded-2xl p-6 shadow-xl border border-gray-700 hover:border-emerald-500/50 transition-colors relative overflow-hidden group">
          <div class="absolute inset-0 bg-emerald-500/5 opacity-0 group-hover:opacity-100 transition-opacity"></div>
          <div class="text-gray-400 text-sm font-medium mb-2 tracking-wide uppercase relative z-10">Wallet Balance (USDT)</div>
          <div class="text-4xl font-bold text-white relative z-10 flex items-center gap-2">
            ${{ metrics.wallet_balance }}
          </div>
        </div>

        <div class="bg-gray-800 rounded-2xl p-6 shadow-xl border border-gray-700 hover:border-gray-600 transition-colors">
          <div class="text-gray-400 text-sm font-medium mb-2 tracking-wide uppercase">Total PNL</div>
          <div :class="getPnlColor(metrics.total_pnl)" class="text-4xl font-bold">
            ${{ metrics.total_pnl }}
          </div>
        </div>
        <div class="bg-gray-800 rounded-2xl p-6 shadow-xl border border-gray-700 hover:border-gray-600 transition-colors">
          <div class="text-gray-400 text-sm font-medium mb-2 tracking-wide uppercase">Win Rate</div>
          <div class="text-4xl font-bold text-blue-400">{{ metrics.win_rate }}%</div>
        </div>
        <div class="bg-gray-800 rounded-2xl p-6 shadow-xl border border-gray-700 hover:border-gray-600 transition-colors">
          <div class="text-gray-400 text-sm font-medium mb-2 tracking-wide uppercase">Active Positions</div>
          <div class="text-4xl font-bold text-purple-400">{{ metrics.active_positions.length }}</div>
        </div>
      </div>

      <!-- Macro Trends Grid (Kept from existing layout) -->
      <div class="bg-gray-800 rounded-2xl p-6 shadow-xl border border-gray-700">
        <h2 class="text-xl font-bold mb-6 text-gray-100 flex items-center gap-2">
          Macro Trends (15m)
        </h2>
        <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4">
          <div 
            v-for="trend in macroTrends" 
            :key="trend.symbol"
            :class="[
              'p-4 rounded-xl flex flex-col items-center justify-center transition-all duration-300 hover:scale-105',
              trend.macro_trend === 'UPTREND' 
                ? 'bg-emerald-500/10 border border-emerald-500/40 shadow-[0_0_15px_rgba(16,185,129,0.1)]' 
                : 'bg-red-500/10 border border-red-500/40 shadow-[0_0_15px_rgba(239,68,68,0.1)]'
            ]"
          >
            <span class="font-bold text-lg tracking-wider">{{ trend.symbol }}</span>
            <span :class="trend.macro_trend === 'UPTREND' ? 'text-emerald-400' : 'text-red-400'" class="text-xs font-bold mt-1 tracking-widest">
              {{ trend.macro_trend }}
            </span>
          </div>
          <div v-if="macroTrends.length === 0" class="col-span-full text-center text-gray-500 py-8 italic">
            Waiting for Macro Engine to broadcast trends...
          </div>
        </div>
      </div>

      <!-- Active Positions Grid -->
      <div class="bg-gray-800 rounded-2xl p-6 shadow-xl border border-gray-700">
        <h2 class="text-xl font-bold mb-6 text-gray-100 flex items-center gap-2">
          Active Positions
        </h2>
        <div class="overflow-x-auto">
          <table class="w-full text-left border-collapse">
            <thead>
              <tr class="text-gray-400 text-sm border-b border-gray-700 bg-gray-900/50">
                <th class="py-4 px-5 font-semibold uppercase tracking-wide rounded-tl-lg">Symbol</th>
                <th class="py-4 px-5 font-semibold uppercase tracking-wide">Direction</th>
                <th class="py-4 px-5 font-semibold uppercase tracking-wide">Entry Price</th>
                <th class="py-4 px-5 font-semibold uppercase tracking-wide">Current Price</th>
                <th class="py-4 px-5 font-semibold uppercase tracking-wide text-right">Unrealized PNL</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="pos in metrics.active_positions" :key="pos.symbol" class="border-b border-gray-700/50 hover:bg-gray-700/30 transition-colors">
                <td class="py-4 px-5">
                  <div class="font-bold text-lg text-white">{{ pos.symbol }}</div>
                  <div class="text-xs text-gray-400 mt-1 max-w-[200px] sm:max-w-xs truncate cursor-help" :title="pos.entry_reason">
                    {{ pos.entry_reason || 'Unknown strategy' }}
                  </div>
                </td>
                <td class="py-4 px-5">
                  <span :class="pos.direction === 'LONG' ? 'text-green-400 bg-green-500/10 border-green-500/20' : 'text-red-400 bg-red-500/10 border-red-500/20'" class="px-3 py-1 rounded-full text-xs font-bold tracking-wider border">
                    {{ pos.direction }}
                  </span>
                </td>
                <td class="py-4 px-5 text-gray-300 font-mono">${{ pos.entry_price }}</td>
                <td class="py-4 px-5 text-gray-300 font-mono">${{ pos.current_price }}</td>
                <td class="py-4 px-5 text-right font-bold font-mono" :class="getPnlColor(pos.unrealized_pnl)">
                  ${{ pos.unrealized_pnl }}
                </td>
              </tr>
              <tr v-if="metrics.active_positions.length === 0">
                <td colspan="5" class="py-12 text-center text-gray-500 italic">No active positions currently running.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Symbol Manager -->
      <div class="bg-gray-800 rounded-2xl p-6 shadow-xl border border-gray-700">
        <h2 class="text-xl font-bold mb-6 text-gray-100">Symbol Manager</h2>
        
        <form @submit.prevent="addSymbol" class="flex flex-col sm:flex-row gap-4 mb-8">
          <input 
            v-model="newSymbol" 
            type="text" 
            placeholder="e.g. SOLUSDT" 
            class="flex-1 bg-gray-900 border border-gray-600 rounded-xl px-5 py-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all uppercase placeholder-gray-500 tracking-wide"
            required
          />
          <button 
            type="submit"
            class="bg-blue-600 hover:bg-blue-500 text-white font-bold px-8 py-3 rounded-xl transition-colors shadow-lg shadow-blue-500/20"
          >
            Add Symbol
          </button>
        </form>

        <div class="flex flex-wrap gap-3">
          <span 
            v-for="sym in activeSymbols" 
            :key="sym.id"
            class="bg-gray-900 px-4 py-2 rounded-lg text-sm font-bold text-gray-300 flex items-center gap-3 border border-gray-700 shadow-inner"
          >
            {{ sym.symbol }}
            <span class="w-2 h-2 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.8)]"></span>
          </span>
          <div v-if="activeSymbols.length === 0" class="text-gray-500 italic py-2">
            No active symbols configured yet.
          </div>
        </div>
      </div>

    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'

const metrics = ref({
  total_pnl: '0.00',
  win_rate: 0,
  wallet_balance: '0.00',
  active_positions: []
})
const macroTrends = ref([])
const activeSymbols = ref([])
const newSymbol = ref('')

const API_BASE = '/api/dashboard'

// Dynamic PNL Color computed function
const getPnlColor = (val) => {
  const num = parseFloat(val)
  if (isNaN(num) || num === 0) return 'text-white'
  return num > 0 ? 'text-green-400' : 'text-red-400'
}

const fetchMetrics = async () => {
  try {
    // Calling the exact endpoint specified
    const res = await fetch(`/api/dashboard-metrics`, { headers: { 'Accept': 'application/json' } })
    if (res.ok) {
      metrics.value = await res.json()
    }
  } catch (e) { 
    console.error('Failed to fetch metrics', e) 
  }
}

const fetchMacroTrends = async () => {
  try {
    const res = await fetch(`${API_BASE}/macro-trends`, { headers: { 'Accept': 'application/json' } })
    if (res.ok) macroTrends.value = await res.json()
  } catch (e) { 
    console.error('Failed to fetch macro trends', e) 
  }
}

const fetchSymbols = async () => {
  try {
    const res = await fetch(`${API_BASE}/symbols`, { headers: { 'Accept': 'application/json' } })
    if (res.ok) activeSymbols.value = await res.json()
  } catch (e) { 
    console.error('Failed to fetch symbols', e) 
  }
}

const addSymbol = async () => {
  if (!newSymbol.value) return
  
  try {
    const res = await fetch(`${API_BASE}/symbols`, {
      method: 'POST',
      headers: { 
        'Content-Type': 'application/json',
        'Accept': 'application/json' 
      },
      body: JSON.stringify({ symbol: newSymbol.value.toUpperCase() })
    })
    
    if (res.ok) {
      newSymbol.value = ''
      await fetchSymbols()
    } else {
      const errorData = await res.json()
      alert(`Failed: ${errorData.message || 'Symbol may already exist.'}`)
    }
  } catch (e) { 
    console.error('Error adding symbol', e) 
  }
}

let pollingInterval

onMounted(() => {
  // Initial Fetch
  fetchMetrics()
  fetchMacroTrends()
  fetchSymbols()
  
  // 5-second Live Polling
  pollingInterval = setInterval(() => {
    fetchMetrics()
    fetchMacroTrends()
  }, 5000)
})

onUnmounted(() => {
  clearInterval(pollingInterval)
})
</script>
