<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import QQStatus from './QQStatus.vue'
import PanelHeader from './PanelHeader.vue'
import SideNavItem from './SideNavItem.vue'
import PixelControl from './PixelControl.vue'
import { applyCompanion, companion, companionOptions } from '../companion'
import { applyScenery, sceneryOptions } from '../scenery'

const emit = defineEmits<{ scroll: [event: Event] }>()
const props = defineProps<{ server?: string }>()
const isJp = computed(() => props.server === 'jp')
const selected = ref<'broadcast' | 'appearance' | 'emulator' | 'connection'>(isJp.value ? 'appearance' : 'broadcast')
const loaded = ref(false)
const connecting = ref(false)
const probe = ref<Awaited<ReturnType<typeof api.jpBrowserProbe>> | null>(null)
const probeBusy = ref(false)
const clickProbe = ref<Awaited<ReturnType<typeof api.jpClickProbe>> | null>(null)
const calibration = ref('')
const testPoint = ref<{ x: number; y: number } | null>(null)
const clickBusy = ref(false)
async function clickTest(action?: 'prepare' | 'cancel') {
  clickBusy.value = true
  try {
    const result = await api.jpClickProbe(action)
    if (result.image) { calibration.value = result.image; testPoint.value = null }
    else clickProbe.value = result
  } catch (error) { message.value = error instanceof Error ? error.message : '测试未能连接' }
  finally { clickBusy.value = false }
}
function markTestPoint(event: MouseEvent) {
  if (clickBusy.value) return
  const rect = (event.currentTarget as HTMLImageElement).getBoundingClientRect()
  testPoint.value = { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height }
}
async function startClickTest(immediate = false) {
  if (!testPoint.value || clickBusy.value) return
  clickBusy.value = true
  try { clickProbe.value = await api.jpClickProbe(immediate ? 'immediate' : 'start', testPoint.value); calibration.value = ''; testPoint.value = null }
  catch (error) { message.value = error instanceof Error ? error.message : '测试未能启动' }
  finally { clickBusy.value = false }
}
async function browserProbe(start = false) {
  probeBusy.value = true
  try { probe.value = await api.jpBrowserProbe(start) }
  catch (error) { message.value = error instanceof Error ? error.message : '诊断未能连接' }
  finally { probeBusy.value = false }
}
const bot = ref<any>(null)
const emu = ref<any>(null)
const ready = computed(() => loaded.value && (isJp.value || !!(bot.value && emu.value)))
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
  if (!isJp.value) [bot.value, emu.value] = await Promise.all([api.botConfig(), api.emulatorConfig()])
  if (bot.value?.telegram && bot.value.telegram.enabled == null) {
    bot.value.telegram.enabled = bot.value.platform === 'telegram' && bot.value.enabled
  }
  const saved = await api.settings(props.server)
  scenery.value = saved.scenery ?? 'spring'
  if (saved.backdrop && /^#[0-9a-fA-F]{6}$/.test(saved.backdrop)) backdrop.value = saved.backdrop
  loaded.value = true
}
async function connect() {
  connecting.value = true
  try {
    await api.jpListenerStart()
    message.value = '日服浏览器已打开，进入游戏后自动更新本丸数据。'
  } catch (error) { message.value = error instanceof Error ? error.message : '连接失败，请重试' }
  finally { connecting.value = false }
}
async function save() {
  message.value = '正在保存……'
  try {
    if (selected.value === 'appearance') {
      await api.saveBackdrop(backdrop.value, props.server)
      await api.saveScenery(scenery.value, props.server)
      await api.saveCompanion(companion.value, props.server)
    } else if (isJp.value) {
      return
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
  try { await load() } catch { message.value = '设置读取失败，请重试' }
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
  <p v-if="!ready && message" role="alert">{{ message }} <button @click="load">重试</button></p>
  <section v-if="ready" class="system-panel" @scroll="emit('scroll', $event)">
    <PanelHeader variant="page" title="系统设置"  :subtitle="isJp ? '外观与连接' : '播报、外观与连接'"><template #actions><button v-if="selected !== 'connection'" class="primary" @click="save">保存设置</button></template></PanelHeader>
    <div class="system-layout"><nav class="system-nav"><template v-if="ready"><SideNavItem v-if="!isJp" :active="selected === 'broadcast'" @click="selected = 'broadcast'">播报</SideNavItem><SideNavItem :active="selected === 'appearance'" @click="selected = 'appearance'">外观</SideNavItem><SideNavItem v-if="!isJp" :active="selected === 'emulator'" @click="selected = 'emulator'">模拟器</SideNavItem><SideNavItem v-if="isJp" :active="selected === 'connection'" @click="selected = 'connection'">连接</SideNavItem></template></nav>
      <div class="system-form" :class="`${selected}-form`">
        <template v-if="selected === 'appearance'"><h3>景趣</h3><label>舞台景趣<PixelControl v-model="scenery" as="select" @update:model-value="value => applyScenery(String(value))"><option v-for="option in sceneryOptions" :key="option.value" :value="option.value">{{ option.label }}</option><option value="random">随机景趣</option></PixelControl></label><p>选择后立刻预览，点「保存设置」记住选择。随机景趣每次打开面板选一张，使用期间保持不变。</p><h3>舞台小人</h3><label>刀剑男士<PixelControl :model-value="companion" as="select" @update:model-value="value => applyCompanion(String(value))"><option v-for="option in companionOptions" :key="option.value" :value="option.value">{{ option.label }}</option></PixelControl></label><p>选择后立刻上岗，点「保存设置」记住选择。小狐狸会继续陪着他。</p><h3>庭院背景色</h3><div class="swatch-row"><button v-for="preset in backdropPresets" :key="preset.value" type="button" class="swatch" :class="{ active: backdrop === preset.value }" :style="{ background: preset.value }" :title="preset.name" :aria-label="preset.name" @click="pickBackdrop(preset.value)"></button><label class="swatch-custom"><input v-model="backdrop" type="color" @input="pickBackdrop(backdrop)" />自定义</label></div><p>点色块立刻试穿，点「保存设置」让它记住。和纸、像素两个主题共用这块背景。</p></template>
        <template v-else-if="selected === 'emulator'"><h3>模拟器</h3><label>ADB 地址<PixelControl v-model="emu.adb_address" :placeholder="emu.default_address" /></label><p>留空表示自动探测；手动填写后脚本会固定连接这个地址。MuMu 多开时端口是 16384 + 32 × 实例号（例如二号实例是 16416）。改动在下次运行脚本时生效。</p></template>
        <template v-else-if="selected === 'connection'"><h3>日服连接</h3><p>打开日服浏览器，进入游戏后自动更新家底、刀账与本丸近况。这里只更新数据，不会替你出阵或消耗资源。</p><button class="primary" :disabled="connecting" @click="connect">{{ connecting ? '连接中…' : '打开日服浏览器' }}</button>
          <h3>熄屏画面诊断</h3><p>先进入游戏本丸，再开始诊断。接电后放开鼠标键盘，让屏幕按你的设置自行关闭，约 7 分钟后回来查看结果。期间不点击游戏、不改电源设置、不保存截图。</p>
          <button class="secondary" :disabled="probeBusy || probe?.state === 'running'" @click="browserProbe(true)">开始画面诊断</button>
          <button class="secondary" :disabled="probeBusy" @click="browserProbe(false)">查看诊断结果</button>
          <p v-if="probe" role="status">{{ probe.state === 'running' ? '诊断进行中' : probe.state === 'done' ? '诊断已结束' : '尚未开始' }} · 成功截图 {{ probe.successful_frames ?? 0 }} 次 · 失败 {{ probe.failed_frames ?? 0 }} 次 · 画面变化 {{ probe.changed_frames ?? 0 }} 次 · 近黑画面 {{ probe.near_black_frames ?? 0 }} 次</p>
          <p v-if="probe?.last_error">{{ probe.last_error }}</p><p>静止画面不等于卡死，画面变化也不代表操作成功。此诊断不检测屏幕是否真的关闭、不测试点击或拖动，不能单独证明熄屏自动化可用。</p>
          <h3>单次点击测试：先亮屏，再熄屏</h3>
          <p>先标定「结成」，选择亮屏立即测试，约 10 秒后查看结果并核对游戏。亮屏确认能进入结成后，手动回本丸、重新获取截图，再进行第 6 分钟定时测试。两种模式都只点一次。</p>
          <p>游戏停在本丸，获取截图后在图上点击「结成」并确认。之后保持游戏画面、窗口尺寸不变，接电并放开鼠标键盘，确认屏幕自行熄灭。约 7 分钟后查看结果。画面变化会跳过点击；不会激活窗口或移动系统鼠标。</p>
          <button class="secondary" :disabled="clickBusy || clickProbe?.state === 'running'" @click="clickTest('prepare')">获取本丸截图并标定</button>
          <button class="secondary" :disabled="clickBusy" @click="clickTest()">查看点击结果</button>
          <button class="secondary" :disabled="clickBusy || clickProbe?.state !== 'running'" @click="clickTest('cancel')">取消点击测试</button>
          <div v-if="calibration" style="position:relative;margin-top:12px;width:fit-content;max-width:100%">
            <img :src="calibration" alt="请在当前本丸截图上点击结成按钮" style="display:block;max-width:100%;height:auto;cursor:crosshair" @click="markTestPoint">
            <span v-if="testPoint" aria-hidden="true" :style="{position:'absolute',left:`${testPoint.x * 100}%`,top:`${testPoint.y * 100}%`,transform:'translate(-50%,-50%)',color:'red',fontSize:'24px',pointerEvents:'none'}">⊕</span>
          </div>
          <p v-if="testPoint">已选位置：横向 {{ Math.round(testPoint.x * 100) }}% · 纵向 {{ Math.round(testPoint.y * 100) }}%。确认这里是「结成」按钮后再开始。<button class="primary" :disabled="clickBusy" @click="startClickTest(true)">确认结成，亮屏立即测试</button><button class="secondary" :disabled="clickBusy" @click="startClickTest(false)">确认结成，第 6 分钟测试</button></p>
          <p v-if="clickProbe" role="status">{{ clickProbe.detail }} · 点击已发送：{{ clickProbe.click_sent ? '是' : '否' }} · 画面变化：{{ clickProbe.picture_changed ? '是' : '否' }}</p>
          <p>截图只在标定时显示，不写入磁盘。画面变化不自动判为进入结成，测试结束后请亲自核对。关闭麻麻露后测试不会继续。</p>
        </template>
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
