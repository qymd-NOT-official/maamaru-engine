<script setup lang="ts">
import { computed } from 'vue'
import type { ClientAsset, ClientInventory } from '../../types'
import { eventTime } from './reportModel'

const props = defineProps<{ stock: ClientInventory | null }>()
const groups = computed(() => {
  const assets = props.stock?.assets
  const swords = assets?.sword || []
  function owners(row: ClientAsset, treasure = false) {
    const fields = treasure ? ['artifact_serial_id1', 'artifact_serial_id2'] as const
      : ['horse_serial_id', 'equip_serial_id1', 'equip_serial_id2', 'equip_serial_id3'] as const
    if (row.serial_id == null) return []
    return swords.filter(s => fields.some(f => s[f] != null && String(s[f]) === String(row.serial_id)))
      .map(s => s.name || '刀剑名称未确认')
  }
  function equipment(rows: ClientAsset[], treasure = false) {
    const grouped = new Map<string, { name: string; count: number; detail: string[] }>()
    for (const row of rows) {
      const name = row.name || `名称待确认（编号 ${row.equip_id ?? row.artifact_id ?? '未知'}）`
      const key = treasure ? `${name}:${row.level ?? '?'}` : name
      const entry = grouped.get(key) || { name: treasure ? `${name} · 等级 ${row.level ?? '未读取'}` : name, count: 0, detail: [] }
      entry.count++
      entry.detail.push(...owners(row, treasure))
      grouped.set(key, entry)
    }
    return [...grouped.values()]
  }
  const items = Object.entries(props.stock?.items || {}).filter(([, v]) => v.count > 0)
  const charmNames = ['御守', '御守·极', '御守·桃']
  return [
    { title: '道具', rows: items.filter(([n]) => !charmNames.includes(n)).map(([name, v]) => ({ name, count: v.count, detail: [eventTime(v.observed_at), ...(v.expires_at ? [`期限 ${v.expires_at}`] : [])] })), known: !!props.stock && Object.keys(props.stock.items).length > 0 },
    { title: '刀装', rows: equipment((assets?.equip || []).filter(r => r.kind === 'troop')), known: !!assets?.equip },
    { title: '马匹', rows: equipment((assets?.equip || []).filter(r => r.kind === 'horse')), known: !!assets?.equip },
    { title: '宝物', rows: equipment(assets?.artifact || [], true), known: !!assets?.artifact },
    { title: '御守', rows: charmNames.map(name => ({ name, count: props.stock?.items[name]?.count ?? null,
      detail: swords.filter(s => s.charm_name === name).map(s => s.name || '刀剑名称未确认') })).filter(row => row.count == null || row.count > 0 || row.detail.length), known: !!props.stock && (Object.keys(props.stock.items).length > 0 || !!assets?.sword) },
    { title: '名称待确认', rows: equipment((assets?.equip || []).filter(r => !r.kind)), known: true },
  ].filter(g => g.title !== '名称待确认' || g.rows.length)
})
</script>

<template>
  <section class="stock-list" aria-label="所持物品">
    <header><h3>所持物品</h3><small v-if="stock?.assets">装备读取于 {{ eventTime(stock.assets.observed_at) }}</small></header>
    <details v-for="group in groups" :key="group.title">
      <summary><b>{{ group.title }}</b><span>{{ !group.known ? '未读取' : `${group.rows.length} 种` }}</span></summary>
      <p v-if="!group.known">未读取</p>
      <p v-else-if="!group.rows.length">这次读取没有记录到此类物品。</p>
      <details v-for="row in group.rows" :key="row.name" class="stock-entry">
        <summary class="stock-row"><span>{{ row.name }}</span><b>{{ group.title === '御守' ? '未装备 ' : '' }}{{ row.count == null ? '未读取' : row.count.toLocaleString() }}</b></summary>
        <p v-if="row.detail.length">{{ group.title === '道具' ? '读取于 ' : '已读到的装备去向：' }}{{ [...new Set(row.detail)].join('、') }}</p>
        <p v-else>{{ group.title === '道具' ? '没有更多信息。' : '这次读取未记录到装备去向。' }}</p>
      </details>
    </details>
  </section>
</template>

<style scoped>
.stock-list { padding: 20px; background: var(--paper-card); }
header, summary, .stock-row { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
header { margin-bottom: 12px; flex-wrap: wrap; }
h3 { margin: 0; }
small, summary span, p { color: var(--ink-dim); font-size: 12px; }
details { border-top: 1px solid var(--border-light, #ddd3c5); }
summary { padding: 14px 0; cursor: pointer; }
summary b::before { content: '▸'; margin-right: 8px; color: var(--fox-gold); }
details[open] summary b::before { content: '▾'; }
.stock-row { padding: 10px 8px; border-top: 1px dashed var(--border-light, #ddd3c5); }
.stock-row span { min-width: 0; overflow-wrap: anywhere; }
.stock-row small { display: block; margin-top: 4px; }
.stock-row > b { flex-shrink: 0; }
.stock-entry p { margin: 0 8px 12px; overflow-wrap: anywhere; }
@media (max-width: 600px) { .stock-list { padding: 14px; } }
</style>
