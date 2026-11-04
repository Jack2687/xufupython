import type {Row} from './lib'
// Pair only matching operations in the same round; repeated requests remain separate.
export function traceRows(events:Row[],live:boolean){
 const rows:Row[]=[],pending=new Map<string,Row[]>()
 const ordered=[...events].sort((a,b)=>Date.parse(a.created_at)-Date.parse(b.created_at))
 const latest=ordered.filter(e=>e.source!=='business'&&!['funds','network'].includes(e.phase)).at(-1)
 const lastRound=Math.max(1,...ordered.map(e=>Number(e.round)||1))
 const operation=(e:Row)=>e.code?.startsWith('post_payment_wait')?'post_payment_wait':e.diagnostics?.request_kind||e.label||e.phase
 for(const e of ordered){
  const key=`${e.round||1}:${e.phase}:${operation(e)}`
  const starts=pending.get(key)||[]
  const starting=e.source!=='business'&&['running','waiting'].includes(e.state)&&!e.diagnostics?.retrying
  const matching=!starting&&e.source!=='business'?[...starts].reverse().find(r=>!r.ended&&(!e.request_id||!r.request_id||e.request_id===r.request_id)):undefined
  if(matching){Object.assign(matching,e,{id:matching.id,started_at:matching.started_at,raw:[...matching.raw,e],ended:true});continue}
  const row={...e,started_at:e.created_at,raw:[e],ended:false}
  rows.push(row)
  if(starting){starts.push(row);pending.set(key,starts)}
 }
 for(const row of rows){
  row.spinning=live&&!row.ended&&row.source!=='business'&&['running','waiting'].includes(row.state)&&(Number(row.round)||1)===lastRound&&row.raw.some((r:Row)=>r===latest||r.id&&r.id===latest?.id)
  if(!row.spinning&&!row.ended&&['running','waiting'].includes(row.state))row.displayState='recorded'
  else row.displayState=row.state
 }
 return rows
}
