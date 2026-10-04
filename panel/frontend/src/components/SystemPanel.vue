<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import QQStatus from './QQStatus.vue'
import PanelHeader from './PanelHeader.vue'
import SideNavItem from './SideNavItem.vue'
import PixelControl from './PixelControl.vue'
import { applyScenery, sceneryOptions } from '../scenery'

const emit = defineEmits<{ scroll: [event: Event] }>()
const selected = ref<'broadcast' | 'appearance' | 'emulator'>('broadcast')
const bot = ref<any>(null)
const emu = ref<any>(null)
const ready = computed(() => !!(bot.value && emu.value))
const telegramToken = ref('')
const message = ref('')
const backdrop = ref('#c4c6b5')
const scenery = ref('spring')
const backdropPresets = [
  { name: '庭园灰绿', value: '#c4c6b5' },
  { name: '暖樱粉', value: '#ddbec2' },
  { name: '藕紫', value: '#c5afbd' },
  { name: '深松绿', value: '#43503f' },
  { name: '暖木棕', value: '#6a5138' },
  { name: '暮池蓝', value: '#4e5a68' },
]
function pickBackdrop(color: string) {
  backdrop.value = color
  document.body.style.setProperty('--space-backdrop', color)
}
async function load() {
  [bot.value, emu.value] = await Promise.all([api.botConfig(), api.emulatorConfig()])
  if (bot.value?.telegram && bot.value.telegram.enabled == null) {
    bot.value.telegram.enabled = bot.value.platform === 'telegram' && bot.value.enabled
  }
  const saved = await api.settings()
  scenery.value = saved.scenery ?? 'spring'
  if (saved.backdrop && /^#[0-9a-fA-F]{6}$/.test(saved.backdrop)) backdrop.value = saved.backdrop
}
async function save() {
  message.value = '正在保存……'
  try {
    if (selected.value === 'appearance') {
      await api.saveBackdrop(backdrop.value)
      await api.saveScenery(scenery.value)
    } else if (selected.value === 'emulator') {
      await api.saveEmulatorConfig({ adb_address: emu.value.adb_address })
    } else {
      await api.saveBotConfig({
        enabled: bot.value.telegram.enabled || bot.value.qq.enabled,
        platform: bot.value.telegram.enabled ? 'telegram' : (bot.value.qq.enabled ? 'qq' : bot.value.platform),
        qq: bot.value.qq,
        telegram: { token: telegramToken.value, allowed_users: bot.value.telegram.allowed_users },
        broadcast: bot.value.broadcast,
      })
      telegramToken.value = ''
    }
    message.value = '设置已保存'
  } catch (e) { message.value = e instanceof Error && e.message ? e.message : '保存失败，请检查面板连接' }
}
onMounted(async () => {
  try { await load() } catch { /* 配置加载失败留给页面重试 */ }
})

const qqBroadcastEnabled = computed({
  get: () => Boolean(bot.value?.qq?.enabled && bot.value?.broadcast?.qq),
  set: (enabled: boolean) => {
    bot.value.qq.enabled = enabled
    bot.value.broadcast.qq = enabled
  },
})
</script>

<template>
  <section v-if="ready" class="system-panel" @scroll="emit('scroll', $event)">
    <PanelHeader variant="page" title="系统设置" subtitle="播报、外观与连接"><template #actions><button class="primary" @click="save">保存设置</button></template></PanelHeader>
    <div class="system-layout"><nav class="system-nav"><template v-if="ready"><SideNavItem :active="selected === 'broadcast'" @click="selected = 'broadcast'">播报</SideNavItem><SideNavItem :active="selected === 'appearance'" @click="selected = 'appearance'">外观</SideNavItem><SideNavItem :active="selected === 'emulator'" @click="selected = 'emulator'">模拟器</SideNavItem></template></nav>
      <div class="system-form" :class="`${selected}-form`">
        <template v-if="selected === 'appearance'"><h3>景趣</h3><label>舞台景趣<PixelControl v-model="scenery" as="select" @update:model-value="value => applyScenery(String(value))"><option v-for="option in sceneryOptions" :key="option.value" :value="option.value">{{ option.label }}</option><option value="random">随机景趣</option></PixelControl></label><p>选择后立刻预览，点「保存设置」记住选择。随机景趣每次打开面板选一张，使用期间保持不变。</p><h3>庭院背景色</h3><div class="swatch-row"><button v-for="preset in backdropPresets" :key="preset.value" type="button" class="swatch" :class="{ active: backdrop === preset.value }" :style="{ background: preset.value }" :title="preset.name" :aria-label="preset.name" @click="pickBackdrop(preset.value)"></button><label class="swatch-custom"><input v-model="backdrop" type="color" @input="pickBackdrop(backdrop)" />自定义</label></div><p>点色块立刻试穿，点「保存设置」让它记住。和纸、像素两个主题共用这块背景。</p></template>
        <template v-else-if="selected === 'emulator'"><h3>模拟器</h3><label>ADB 地址<PixelControl v-model="emu.adb_address" :placeholder="emu.default_address" /></label><p>留空表示自动探测；手动填写后脚本会固定连接这个地址。MuMu 多开时端口是 16384 + 32 × 实例号（例如二号实例是 16416）。改动在下次运行脚本时生效。</p></template>
        <template v-else><h3>运行播报</h3>
          <label class="check-label"><input v-model="qqBroadcastEnabled" type="checkbox" />QQ 播报</label>
          <div v-if="qqBroadcastEnabled" class="broadcast-channel-settings"><QQStatus /><label>协议端<PixelControl v-model="bot.qq.provider" as="select"><option value="napcat">NapCat</option><option value="snowluma">SnowLuma</option><option value="custom">其他 OneBot 实现</option></PixelControl></label><label>消息接口<PixelControl v-model="bot.qq.snowluma_http" /></label><label>管理页<PixelControl v-model="bot.qq.snowluma_gui_http" /></label><label>接收播报的 QQ<PixelControl :model-value="(bot.qq.admin_qq || []).join(', ')" @update:model-value="bot.qq.admin_qq = $event" /></label><p>QQ 配置修改后需要重启まあ丸。</p></div>
          <label class="check-label"><input v-model="bot.telegram.enabled" type="checkbox" />纸飞机渠道</label>
          <div v-if="bot.telegram.enabled" class="broadcast-channel-settings"><label>Bot Token<PixelControl v-model="telegramToken" type="password" :placeholder="bot.telegram.has_token ? `已配置（${bot.telegram.token_masked}），留空不改` : '输入 Token'" /></label><label>接收播报的用户 ID<PixelControl :model-value="(bot.telegram.allowed_users || []).join(', ')" @update:model-value="bot.telegram.allowed_users = $event" /></label><p>Token 留空不会改变现有配置。</p></div>
          <label class="check-label"><input v-model="bot.broadcast.ntfy" type="checkbox" />ntfy 手机推送</label>
          <p>脚本开工、收工或需要你处理时，会通过启用的渠道告诉你。</p>
        </template>
        <p v-if="message" class="inline-message">{{ message }}</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.system-panel .system-layout { height: 100%; }
.broadcast-channel-settings { margin: -2px 0 16px 18px; padding: 16px 18px; background: var(--paper-panel); border-left: 3px solid var(--fox-gold); }
.broadcast-channel-settings > p { margin: 8px 0 0; color: var(--ink-dim); font-size: 12px; }
@media (max-width: 720px) {
  .broadcast-channel-settings { margin-left: 0; padding: 14px; }
}
</style>
