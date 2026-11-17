<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/entries?kind=%E6%AD%A3%E5%90%91')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

function stateClass(s) {
  return s === '已闭口' ? 'on' : s === '部分冲' ? 'warn' : ''
}
</script>

<template>
  <div>
    <h1>正向流水</h1>
    <p class="lead">
      已装载的正向入账；归并后状态变为「已归并」。每行带该正向的持久剩余敞口
      （原金额 − 已成功冲正累计），闭口即剩余为 0。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>正向与剩余敞口</h2>
      <table>
        <thead>
          <tr>
            <th>流水键</th><th>账户</th><th>原金额（元）</th><th>已冲累计（元）</th>
            <th>剩余敞口（元）</th><th>敞口状态</th><th>最近冲正</th><th>流水状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in rows" :key="e.id">
            <td>{{ e.entry_key }}</td>
            <td>{{ e.account_code }}</td>
            <td>{{ yuan(e.amount_cents) }}</td>
            <td>{{ e.exposure_reversed_cents == null ? '—' : yuan(e.exposure_reversed_cents) }}</td>
            <td>
              <strong v-if="e.exposure_remaining_cents != null">{{ yuan(e.exposure_remaining_cents) }}</strong>
              <span v-else>—</span>
            </td>
            <td>
              <span v-if="e.exposure_state" class="tag" :class="stateClass(e.exposure_state)">
                {{ e.exposure_state }}
              </span>
              <span v-else>—</span>
            </td>
            <td>{{ e.exposure_last_reversal_key || '—' }}</td>
            <td><span class="tag" :class="{ on: e.status === '已归并' }">{{ e.status }}</span></td>
          </tr>
          <tr v-if="!rows.length"><td colspan="8">尚无正向流水</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
