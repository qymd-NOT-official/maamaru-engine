import { describe, expect, it } from 'vitest'
import { compileDaily, dailyRows } from './dailySettingsModel'
import type { WorkflowPreset, WorkflowNodeDef } from '../types'
const definitions = ['boot_emulator','login','signin','free_gift','practice','naihanka','forge','dismantle','synthesize','expedition','yosari','raid','task_rewards','inbox_supplies','sugar'].map(type => ({type,label:type,desc:'',category:'chore',params:[]})) as WorkflowNodeDef[]
describe('日课分组执行', () => {
 it('按约定顺序合并签到与锻刀，炼糖默认关闭', () => {
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[{type:'login',params:{},on_error:'stop'},{type:'free_gift',params:{},on_error:'continue'},{type:'forge',params:{times:7},on_error:'continue'}]}
  const rows = dailyRows(preset,definitions,'raid')
  expect(rows.map(r=>r.label)).toEqual(['签到','演练','内番','锻刀刀解','合成','指定远征','异去','当期活动','任务奖励','领杂物箱','炼糖'])
  expect(rows.find(r=>r.id==='sugar')?.enabled).toBe(false)
  expect(compileDaily(rows,[]).map(n=>n.type)).toEqual(['boot_emulator','login','signin','free_gift','forge','dismantle'])
  expect(preset.nodes[2]?.params.times).toBe(7)
 })
 it('旧清单的合成从其他步骤移到锻刀刀解后面，保留开关和参数', () => {
  const node = {type:'synthesize',params:{custom:'kept'},on_error:'continue' as const}
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[],daily_ui:{startTime:'20:00',rows:[
    {id:'forge',label:'锻刀刀解',enabled:true,nodes:[{type:'dismantle',params:{},on_error:'stop'}]},
    {id:'custom',label:'原有其他步骤',enabled:false,nodes:[node,{type:'snapshot',params:{},on_error:'stop'}]},
  ]}}
  const rows = dailyRows(preset,definitions,'raid')
  expect(rows.map(r=>r.id)).toEqual(['forge','synthesize','custom'])
  expect(rows[1]).toMatchObject({label:'合成',enabled:false,nodes:[node]})
  expect(rows[2]?.nodes.map(n=>n.type)).toEqual(['snapshot'])
  expect(preset.daily_ui?.rows[1]?.nodes).toHaveLength(2)
 })
 it('旧异去参数转换为独立节点，清单与执行参数不共享引用', () => {
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[{type:'daily_sortie',params:{sortie_mode:'yosari',yosari_map_no:4,yosari_runs:8,team_no:'2'},on_error:'continue'}]}
  const rows = dailyRows(preset,definitions,'raid')
  const sortie = rows.find(r=>r.id==='yosari')!
  expect(sortie.enabled).toBe(true)
  expect(sortie.nodes[0]?.params).toMatchObject({loops:8,map_no:4,team_no:'2'})
  const compiled = compileDaily(rows,[])
  compiled.find(n=>n.type==='yosari')!.params.loops=1
  expect(sortie.nodes[0]?.params.loops).toBe(8)
 })
 it('保留取消勾选项目的配置和其他自定义步骤', () => {
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[{type:'synthesize',params:{},on_error:'continue'}]}
  const rows = dailyRows(preset,definitions,'raid')
  expect(rows.find(r=>r.id==='synthesize')?.nodes[0]?.type).toBe('synthesize')
  const metadata = {...preset,daily_ui:{rows,startTime:'20:00'}}
  const reopened = dailyRows(metadata,definitions,'raid')
  reopened[0]!.enabled=true
  expect(rows[0]?.enabled).toBe(false)
 })
})
