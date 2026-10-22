import { describe, expect, it } from 'vitest'
import { compileDaily, dailyRows, withRecommendedRuns } from './dailySettingsModel'
import type { WorkflowPreset, WorkflowNodeDef } from '../types'
const definitions = ['boot_emulator','login','signin','free_gift','practice','naihanka','forge','dismantle','synthesize','expedition','yosari','raid','task_rewards','inbox_supplies','sugar'].map(type => ({type,label:type,desc:'',category:'chore',params:[]})) as WorkflowNodeDef[]
describe('日课分组执行', () => {
 it('合并项目编辑后编译与重开一致且不改原配置', () => {
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[
   {type:'signin',params:{}},
   {type:'free_gift',params:{}},
  ]}
  const rows = dailyRows(preset,definitions,'')
  const signin = rows.find(row=>row.id==='signin')!
  expect(signin.nodes.map(node=>node.type)).toEqual(['signin','free_gift'])
  signin.nodes[0]!.params = {touched:'yes'}
  const nodes = compileDaily(rows,[])
  expect(nodes.map(node=>node.type)).toEqual(['boot_emulator','login','signin','free_gift'])
  const reopened = dailyRows({...preset,nodes,daily_ui:{startTime:'',rows}},definitions,'')
  expect(reopened.find(row=>row.id==='signin')!.nodes.map(node=>node.type)).toEqual(['signin','free_gift'])
  expect(preset.nodes[0]!.params).toEqual({})
 })
 it('换期替换旧活动草稿，只保留部队和勾选状态，不修改保存源', () => {
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[],daily_ui:{startTime:'',rows:[
   {id:'activity',label:'当期活动',enabled:true,nodes:[{type:'raid',params:{team_no:'3',runs:20,map_no:4,auto_refill:true}}]},
  ]}}
  const defs = [...definitions,{type:'osaka',label:'大阪城',desc:'',category:'battle',params:[{key:'floors',type:'number',label:'次数',default:1}]}] as WorkflowNodeDef[]
  const row = dailyRows(preset,defs,['osaka']).find(r=>r.id==='activity')!
  expect(row.enabled).toBe(true)
  expect(row.nodes).toEqual([{type:'osaka',params:{floors:1,team_no:'3'}}])
  expect(preset.daily_ui!.rows[0]!.nodes[0]!.type).toBe('raid')
 })
 it('重开保留已保存的大阪城参数，活动信息未知时也不擅改', () => {
  const node = {type:'osaka',params:{team_no:'2',floors:60,target_floor:88,repair_threshold:'heavy'}}
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[node],daily_ui:{startTime:'',rows:[{id:'activity',label:'当期活动',enabled:false,nodes:[node]}]}}
  expect(dailyRows(preset,definitions,['osaka']).find(r=>r.id==='activity')).toEqual(preset.daily_ui!.rows[0])
  expect(dailyRows(preset,definitions,[]).find(r=>r.id==='activity')).toEqual(preset.daily_ui!.rows[0])
  expect(dailyRows(preset,definitions,['raid','osaka']).find(r=>r.id==='activity')).toEqual(preset.daily_ui!.rows[0])
 })
 it('无清单的旧活动也在原位置换期，不残留到其他步骤继续执行', () => {
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[{type:'raid',params:{team_no:'3',runs:20}}]}
  const rows = dailyRows(preset,definitions,'osaka')
  const compiled = compileDaily(rows,[])
  expect(compiled.map(n=>n.type)).toEqual(['boot_emulator','login','osaka'])
  expect(rows.some(r=>r.id==='custom')).toBe(false)
 })
 it('按约定顺序合并签到与锻刀，炼糖默认关闭', () => {
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[{type:'login',params:{}},{type:'free_gift',params:{}},{type:'forge',params:{times:7}}]}
  const rows = dailyRows(preset,definitions,'raid')
  expect(rows.map(r=>r.label)).toEqual(['签到','演练','内番','锻刀刀解','合成','指定远征','异去','当期活动','任务奖励','领杂物箱','炼糖'])
  expect(rows.find(r=>r.id==='sugar')?.enabled).toBe(false)
  expect(compileDaily(rows,[]).map(n=>n.type)).toEqual(['boot_emulator','login','signin','free_gift','forge','dismantle'])
  expect(preset.nodes[2]?.params.times).toBe(7)
 })
 it('旧清单的合成从其他步骤移到锻刀刀解后面，保留开关和参数', () => {
  const node = {type:'synthesize',params:{custom:'kept'}}
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[],daily_ui:{startTime:'20:00',rows:[
    {id:'forge',label:'锻刀刀解',enabled:true,nodes:[{type:'dismantle',params:{}}]},
    {id:'custom',label:'原有其他步骤',enabled:false,nodes:[node,{type:'snapshot',params:{}}]},
  ]}}
  const rows = dailyRows(preset,definitions,'raid')
  expect(rows.map(r=>r.id)).toEqual(['forge','synthesize','custom'])
  expect(rows[1]).toMatchObject({label:'合成',enabled:false,nodes:[node]})
  expect(rows[2]?.nodes.map(n=>n.type)).toEqual(['snapshot'])
  expect(preset.daily_ui?.rows[1]?.nodes).toHaveLength(2)
 })
 it('建议圈数强制补充手形，保留手动配置供恢复', () => {
  const manual = {type:'raid',params:{runs:3,auto_refill:false,team_no:'2'}}
  expect(withRecommendedRuns(manual,12).params).toEqual({runs:12,auto_refill:true,team_no:'2'})
  expect(manual.params).toEqual({runs:3,auto_refill:false,team_no:'2'})
  expect(() => withRecommendedRuns(manual,0)).toThrow()
 })
 it('旧异去参数转换为独立节点，清单与执行参数不共享引用', () => {
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[{type:'daily_sortie',params:{sortie_mode:'yosari',yosari_map_no:4,yosari_runs:8,team_no:'2'}}]}
  const rows = dailyRows(preset,definitions,'raid')
  const sortie = rows.find(r=>r.id==='yosari')!
  expect(sortie.enabled).toBe(true)
  expect(sortie.nodes[0]?.params).toMatchObject({loops:8,map_no:4,team_no:'2'})
  const compiled = compileDaily(rows,[])
  compiled.find(n=>n.type==='yosari')!.params.loops=1
  expect(sortie.nodes[0]?.params.loops).toBe(8)
 })
 it('保留取消勾选项目的配置和其他自定义步骤', () => {
  const preset: WorkflowPreset = {id:'builtin-daily',name:'日课',nodes:[{type:'synthesize',params:{}}]}
  const rows = dailyRows(preset,definitions,'raid')
  expect(rows.find(r=>r.id==='synthesize')?.nodes[0]?.type).toBe('synthesize')
  const metadata = {...preset,daily_ui:{rows,startTime:'20:00'}}
  const reopened = dailyRows(metadata,definitions,'raid')
  reopened[0]!.enabled=true
  expect(rows[0]?.enabled).toBe(false)
 })
})
