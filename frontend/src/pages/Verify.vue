<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)

async function load() {
  const list = await getJson('/api/verify-reports')
  rows.value = list.map((r) => {
    let detail = {}
    try {
      detail = r.detail_json ? JSON.parse(r.detail_json) : {}
    } catch {
      detail = {}
    }
    return { ...r, detail }
  })
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runVerify() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/verify-reports')
    msg.value = r.consistent
      ? `互证一致：流水净额 ${(r.ledger_net_cents / 100).toFixed(2)} = 余额合计`
      : `互证不一致：流水 ${(r.ledger_net_cents / 100).toFixed(2)} vs 余额 ${(r.balance_sum_cents / 100).toFixed(2)}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>净额互证</h1>
    <p class="lead">核对「已归并流水净额」与「账户余额合计」是否一致（隔离项不计入净额），并校验敞口守恒：原额 − 已冲累计 = 剩余敞口。</p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runVerify">跑净额互证</button>
    </div>
    <p v-if="msg" :class="msg.includes('一致') && !msg.includes('不一致') ? 'ok' : 'msg'">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>互证报告</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>归并#</th><th>流水净额</th><th>余额合计</th><th>已冲累计</th>
            <th>剩余敞口（本轮/当前）</th><th>敞口不符</th><th>超额隔离</th><th>隔离数</th><th>一致</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.settle_run_id }}</td>
            <td>{{ yuan(r.ledger_net_cents) }}</td>
            <td>{{ yuan(r.balance_sum_cents) }}</td>
            <td>{{ yuan((r.detail || {}).已冲累计分) }}</td>
            <td>
              {{ yuan((r.detail || {}).剩余敞口合计分) }}
              <span class="muted-sub">/ {{ yuan((r.detail || {}).当前剩余敞口合计分) }}</span>
            </td>
            <td>{{ (r.detail || {}).敞口不符数 }}</td>
            <td>{{ (r.detail || {}).本轮超额隔离笔数 }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>
              <span class="tag" :class="r.consistent ? 'on' : 'bad'">
                {{ r.consistent ? '是' : '否' }}
              </span>
            </td>
            <td>{{ r.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="11">尚无报告</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
