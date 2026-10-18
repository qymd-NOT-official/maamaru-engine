// 日服浏览器启动的诚实提示：start 接口会等启动出结果才返回，
// 这里按真实状态给玩家看结论，不许先报喜。
import { api } from './api'

export async function startJpBrowser(): Promise<string> {
  let status
  try {
    status = await api.jpListenerStart()
  } catch {
    return '日服浏览器没有打开，请重试。'
  }
  if (status.state === 'error') return status.detail || '日服浏览器没有打开，请重试。'
  if (status.state === 'listening' || status.browser_alive) {
    return '日服浏览器已打开，进入游戏后自动更新本丸数据。'
  }
  return '日服浏览器正在启动，稍等片刻它会自己接上。'
}
