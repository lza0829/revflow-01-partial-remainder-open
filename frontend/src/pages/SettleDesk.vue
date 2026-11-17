<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const runs = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)

async function load() {
  runs.value = await getJson('/api/settle-runs')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `归并完成 #${r.id}：净额 ${(r.net_cents / 100).toFixed(2)}，隔离 ${r.quarantine_count}（超额 ${r.excess_quarantine_count}），剩余敞口 ${(r.open_cents / 100).toFixed(2)}`
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
    <h1>归并台</h1>
    <p class="lead">对「待归并」正向与冲正跑一轮净额归并：配平落余额，无原单/超额进隔离。冲正额度只取自持久剩余敞口。</p>
    <p class="note-strip">
      本轮只处理状态为「待归并」的流水；隔离项不计入账户余额。超额整笔失败，不部分扣账。
    </p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runSettle">跑净额归并</button>
    </div>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>归并运行</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>正向</th><th>冲正</th><th>已归并</th><th>隔离</th>
            <th>净额（元）</th><th>已冲累计（元）</th><th>剩余敞口合计（元）</th>
            <th>闭口正向</th><th>超额隔离</th><th>敞口不符</th><th>提交</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.forward_count }}</td>
            <td>{{ r.reversal_count }}</td>
            <td>{{ r.settled_count }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>{{ yuan(r.net_cents) }}</td>
            <td>{{ yuan(r.reversed_total_cents) }}</td>
            <td>{{ yuan(r.open_cents) }}</td>
            <td>{{ r.closed_count }}</td>
            <td>{{ r.excess_quarantine_count }}</td>
            <td>
              <span class="tag" :class="r.exposure_mismatch_count ? 'bad' : 'on'">
                {{ r.exposure_mismatch_count }}
              </span>
            </td>
            <td>{{ r.commit_mode }}</td>
            <td>{{ r.finished_at || '—' }}</td>
          </tr>
          <tr v-if="!runs.length"><td colspan="14">尚无归并运行</td></tr>
        </tbody>
      </table>
    </div>
    <p class="hint-line">{{ runs.length ? '报告口径与敞口表、流水状态、账户余额同源；刷新不回退。' : '' }}</p>
  </div>
</template>
