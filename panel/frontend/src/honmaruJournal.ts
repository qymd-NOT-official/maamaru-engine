import { shanghaiDate, signed } from './components/report/reportModel'
import expeditionData from '../../../touken/data/expedition_maps.json'

const expeditionMaps: Record<string, { name: string }> = expeditionData.maps

export function runPostText(run: any): string {
  const loops = Number(run.loops) || 0
  const session = run.koban_session
  const earned = session?.source === 'game_rewards' && Number.isSafeInteger(session.delta) && session.delta >= 0
    ? session.delta as number : null
  const harvest = earned == null ? '' : `已确认捞到 ${earned.toLocaleString('zh-CN')} 小判。`
  if (run.status === 'failed') return `这趟没能顺利收工。${harvest}${loops > 0 ? `已确认的 ${loops} 圈照常记下；` : ''}停在哪里，留在详细记录里了。`
  if (run.status === 'stopped') return `这趟按你的意思停下了。${harvest}${loops > 0 ? `已确认走完 ${loops} 圈，` : ''}后面的安排不会算作完成。`
  if (run.status !== 'completed') return '这趟的结果还没确认，先照原样留在记录里。'
  if (earned != null) {
    const floors = Number(session.floors) || 0
    return `大阪城这趟回来啦，捞到了 ${earned.toLocaleString('zh-CN')} 小判！${floors > 0 ? `一共挖了 ${floors} 层，收获已经记好。` : '收获已经记好。'}`
  }
  if (loops > 0) return `这趟确认走完 ${loops} 圈。走过的路与能核对的收获，都留在账房的记录里。`
  return '这趟执务已经收工，完成了哪些事，可以翻开账房记录看看。'
}

export interface JournalPost {
  key: string; ts: number; title: string; text: string; label: string
  scene: string; icon: string; facts: string[]
  author: string; avatar?: string; firstObtained?: boolean
}

// 同类、同一来源的一小批操作合成一张小报；保留真实时间，不随机改写旧动态。
export function buildJournalPosts(events: any[], departures: any[] = []): JournalPost[] {
  const batches = new Map<string, Array<{ ts: number; key: string; payload: any }>>()
  const seen = new Set<string>()
  for (const event of events) {
    const p = event.payload || {}
    const kind = event.event_type === 'forge.collected' ? 'forge'
      : event.event_type === 'sword.obtained' ? 'drop'
      : event.event_type === 'sword.inbox_received' ? 'inbox'
      : event.event_type === 'expedition.settled' ? 'expedition' : ''
    if (!kind || !Number.isFinite(Number(event.ts))) continue
    const key = String(p.receipt_key || `event-${event.id}`)
    if (seen.has(key)) continue
    seen.add(key)
    const source = kind === 'drop' ? `${p.source || ''}:${p.chapter || ''}:${p.map_no || ''}`
      : kind === 'expedition' ? `${p.team_no || ''}:${p.captain?.serial_id || ''}:${p.map_code || ''}` : ''
    const group = `${kind}|${source}|${shanghaiDate(Number(event.ts))}`
    const list = batches.get(group) || []
    list.push({ ts: Number(event.ts), key, payload: p })
    batches.set(group, list)
  }
  for (const item of departures) {
    if (!Number.isFinite(Number(item.ts)) || !['链结', '习合', '刀解'].includes(item.reason)) continue
    const key = `departure-${item.serial_id}`
    if (seen.has(key)) continue
    seen.add(key)
    const group = `tidy||${shanghaiDate(Number(item.ts))}`
    const list = batches.get(group) || []
    list.push({ ts: Number(item.ts), key, payload: item })
    batches.set(group, list)
  }
  const posts: JournalPost[] = []
  for (const [group, rows] of batches) {
    const kind = group.split('|')[0]!
    rows.sort((a, b) => a.ts - b.ts || a.key.localeCompare(b.key))
    const chunks: typeof rows[] = []
    for (const row of rows) {
      const chunk = chunks[chunks.length - 1]
      if (chunk && row.ts - chunk[0]!.ts <= 300) chunk.push(row)
      else chunks.push([row])
    }
    for (const chunk of chunks) {
      const first = chunk[0]!, last = chunk[chunk.length - 1]!
      const swords = chunk.flatMap(row => Array.isArray(row.payload.swords) ? row.payload.swords : [row.payload])
      const names = new Map<string, number>()
      for (const sword of swords) if (sword.name) names.set(sword.name, (names.get(sword.name) || 0) + 1)
      const nameFacts = [...names].map(([name, count]) => `${name}${count > 1 ? ` ×${count}` : ''}`)
      let title = '', text = '', label = '', icon = '', scene = 'honmaru_garden_stage.png', facts = nameFacts
      let author = '狐之助', avatar: string | undefined, firstObtained = false
      if (kind === 'forge') {
        author = '刀匠'; avatar = '/static/img/ui/forge-smith-avatar.png'
        const newcomers = [...new Set(swords.filter(s => s.is_first_get_sword === true && s.name).map(s => s.name))]
        firstObtained = newcomers.length > 0
        title = firstObtained ? `${newcomers.join('、')}，锻出来了！` : swords.length === 1 ? '这一炉，锻好了。' : '这批刀，锻好了。'
        label = '锻刀手记'; icon = 'forge.png'; scene = 'honmaru_forge_stage.png'
        text = firstObtained ? `第一次迎来${newcomers.map(name => `【${name}】`).join('、')}！${swords.length > 1 ? `这批一共收下 ${swords.length} 振刀。` : '快来看看吧。'}` : `收下了 ${swords.length} 振刀，炉边又忙完一阵。`
      } else if (kind === 'drop') {
        title = '归途中，迎来了新伙伴'; label = '出阵见闻'; icon = 'sortie.png'; scene = 'honmaru_sortie_stage.png'
        const route = first.payload.chapter && first.payload.map_no ? `${first.payload.chapter}-${first.payload.map_no}`
          : first.payload.source === 'raid.drop' ? '联队战' : '战斗'
        text = `在 ${route} 带回了 ${swords.length} 振刀，名字记在这里。`
      } else if (kind === 'inbox') {
        title = '收件箱里的刀，接回来了'; label = '本丸来信'; icon = 'sword.svg'
        text = `从收件箱接回 ${swords.length} 振刀。`
        facts = [...new Set(chunk.map(row => row.payload.origin_label).filter(Boolean)), ...nameFacts]
      } else if (kind === 'tidy') {
        title = '今天整理了一下刀剑'; label = '刀剑手记'; icon = 'sword.svg'
        facts = ['链结', '习合', '刀解'].map(reason => {
          const count = chunk.filter(row => row.payload.reason === reason).length
          return count ? `${reason} ${count} 振` : ''
        }).filter(Boolean)
        text = '整理妥当，留下一页小记。'
      } else {
        const p = first.payload
        const mapCode = p.map_code || (p.era >= 1 && p.era <= 5 && p.slot >= 1 && p.slot <= 4
          ? `${'ABCDE'[p.era - 1]}${p.slot}` : '')
        const destination = expeditionMaps[mapCode]?.name || p.map_name || ''
        title = first.payload.team_no ? `第 ${first.payload.team_no} 部队回来了` : '远征的部队回来了'
        label = '远征来信'; icon = 'expedition.png'
        text = '一路辛苦，带回来的收获收好了。'
        const rewards = new Map<string, number>()
        for (const row of chunk) for (const [name, amount] of Object.entries(row.payload.rewards || {})) {
          if (typeof amount === 'number' && Number.isFinite(amount)) rewards.set(name, (rewards.get(name) || 0) + amount)
        }
        facts = [...rewards].map(([name, value]) => `${name} ${signed(value)}`)
        const captain = first.payload.captain
        if (captain?.name && captain.serial_id > 0 && captain.sword_id > 0) {
          author = captain.name
          avatar = `/api/journal/avatar/${captain.sword_id}`
          text = destination ? `${destination}远征回来了。带回的东西都放在这里。`
            : '我们远征回来了。带回的东西都放在这里。'
        }
      }
      posts.push({ key: `journal-${kind}-${first.key}`, ts: last.ts, title, text, label, icon, scene, facts, author, avatar, firstObtained })
    }
  }
  return posts.sort((a, b) => b.ts - a.ts)
}
