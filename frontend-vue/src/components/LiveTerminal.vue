<template>
  <div class="bg-black rounded-2xl p-4 shadow-xl border border-gray-700 flex flex-col font-mono text-sm">
    <div class="flex items-center justify-between mb-2 border-b border-gray-800 pb-2">
      <div class="flex items-center gap-2 text-gray-400">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
        <span class="font-bold tracking-widest uppercase">Live Terminal</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="relative flex h-2 w-2">
          <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
          <span class="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
        </span>
        <span class="text-xs text-gray-500">Streaming</span>
      </div>
    </div>
    
    <div 
      ref="terminalContainer" 
      class="flex-1 overflow-y-auto overflow-x-hidden h-64 space-y-1 scrollbar-thin scrollbar-thumb-gray-700 scrollbar-track-transparent pr-2 w-full text-xs sm:text-sm"
    >
      <div v-if="logs.length === 0" class="text-gray-600 italic">
        Awaiting bot initialization...
      </div>
      <div 
        v-for="log in logs" 
        :key="log.id"
        class="flex items-start gap-2 sm:gap-3 hover:bg-gray-900/50 p-1 rounded transition-colors"
      >
        <span class="text-gray-500 shrink-0">[{{ formatTime(log.created_at) }}]</span>
        <span 
          :class="getLogColor(log.action)"
          class="font-bold shrink-0 w-12 sm:w-auto sm:min-w-[70px]"
        >
          {{ log.action }}
        </span>
        <span class="text-gray-300 font-bold shrink-0">[{{ log.symbol }}]</span>
        <span class="text-green-400 break-words flex-1 min-w-0">{{ log.message }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'

const logs = ref([])
const terminalContainer = ref(null)
let pollingInterval

const fetchLogs = async () => {
  try {
    const res = await fetch('/api/dashboard/logs', { headers: { 'Accept': 'application/json' } })
    if (res.ok) {
      const data = await res.json()
      // Only scroll if we actually got new logs
      const isNew = logs.value.length === 0 || (data.length > 0 && data[data.length-1].id !== logs.value[logs.value.length-1]?.id)
      logs.value = data
      
      if (isNew) {
        scrollToBottom()
      }
    }
  } catch (e) {
    console.error('Failed to fetch bot logs', e)
  }
}

const scrollToBottom = async () => {
  await nextTick()
  if (terminalContainer.value) {
    terminalContainer.value.scrollTop = terminalContainer.value.scrollHeight
  }
}

const formatTime = (isoStr) => {
  const date = new Date(isoStr)
  return date.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute:'2-digit', second:'2-digit' })
}

const getLogColor = (action) => {
  switch (action) {
    case 'ENTRY': return 'text-purple-400'
    case 'EXIT': return 'text-red-400'
    case 'INFO': return 'text-blue-400'
    case 'WARNING': return 'text-yellow-400'
    case 'ERROR': return 'text-red-500'
    default: return 'text-emerald-400'
  }
}

onMounted(() => {
  fetchLogs()
  pollingInterval = setInterval(fetchLogs, 3000)
})

onUnmounted(() => {
  clearInterval(pollingInterval)
})
</script>
