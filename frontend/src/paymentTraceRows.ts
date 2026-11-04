import type {Row} from './lib'
const stamp=(e:Row)=>Date.parse(e.created_at)
const terminal=(e:Row)=>['completed','failed','review','skipped'].includes(e.state)
const request=(e:Row)=>e.diagnostics?.request_kind||''
const operation=(e:Row)=>e.code?.startsWith('post_payment_wait')?'post_payment_wait':request(e)||e.label||e.phase
const round=(e:Row)=>Number(e.round)||1

// Presentation only. Every original event remains once in raw evidence.
// Different attempts, rounds and request results must remain distinguishable.
export function traceRows(events:Row[],live:boolean){
 const rows:Row[]=[],pending=new Map<string,Row[]>()
 const ordered=[...events].sort((a,b)=>stamp(a)-stamp(b))
 const technical=ordered.filter(e=>e.source!=='business')
 const latest=technical.filter(e=>e.phase!=='funds').at(-1)
 const lastRound=Math.max(1,...technical.map(round))
 for(const e of technical){
  const key=`${round(e)}:${e.phase}:${operation(e)}`
  const starts=pending.get(key)||[]
  const starting=['running','waiting'].includes(e.state)&&!e.diagnostics?.retrying
  const matching=!starting?[...starts].reverse().find(r=>!r.ended&&(!r.diagnostics?.request_attempt||!e.diagnostics?.request_attempt||r.diagnostics.request_attempt===e.diagnostics.request_attempt)&&(!e.request_id||!r.request_id||e.request_id===r.request_id)):undefined
  if(matching){Object.assign(matching,e,{id:matching.id,started_at:matching.started_at,raw:[...matching.raw,e],ended:terminal(e)});continue}
  const row={...e,started_at:starting?e.created_at:null,raw:[e],ended:terminal(e)}
  rows.push(row)
  if(starting){starts.push(row);pending.set(key,starts)}
 }
 const removed=new Set<Row>()
 for(const wrapper of rows){
  if(request(wrapper)||!['checkout','confirming','card_prepare'].includes(wrapper.phase)||wrapper.code&&wrapper.code!=='completed')continue
  const start=Date.parse(wrapper.started_at||wrapper.created_at)
  const nextWrapper=rows.find(r=>r!==wrapper&&!request(r)&&r.phase===wrapper.phase&&round(r)===round(wrapper)&&Date.parse(r.started_at||r.created_at)>start)
  const until=wrapper.ended?stamp(wrapper)+1000:nextWrapper?Date.parse(nextWrapper.started_at||nextWrapper.created_at):Infinity
  const candidates=rows.filter(r=>request(r)&&r.phase===wrapper.phase&&round(r)===round(wrapper)&&Date.parse(r.started_at||r.created_at)>=start-1000&&stamp(r)<=until)
  // Attach only unambiguous stage wrappers. Retain the actual request clock.
  if(candidates.length===1){candidates[0].raw.push(...wrapper.raw);removed.add(wrapper)}
 }
 const visible=rows.filter(r=>!removed.has(r))
 const targets:Record<string,string[]>={queued:['queue'],processing:['proxy'],checkout:['checkout'],checkout_done:['checkout'],payment:['card_prepare'],confirming:['confirming'],activation:['activation'],activation_confirmed:['activation'],renewal_pending:['renewal'],renewal_disabled:['renewal'],renewal_kept:['renewal'],payment_succeeded:['result'],network_retry:['queue']}
 for(const e of ordered.filter(e=>e.source==='business')){
  const nearby=technical.find(t=>stamp(t)>=stamp(e))||technical.at(-1)
  const eventRound=nearby?round(nearby):1
  const sameCode=visible.filter(r=>e.code&&round(r)===eventRound&&r.code===e.code&&Math.abs(stamp(r)-stamp(e))<2000)
  const phases=targets[e.code]||[]
  const candidates=visible.filter(r=>round(r)===eventRound&&r.source!=='business'&&phases.includes(r.phase)&&Math.abs(Date.parse(r.started_at||r.created_at)-stamp(e))<60000)
  const next=candidates.filter(r=>Date.parse(r.started_at||r.created_at)>=stamp(e)-1000).sort((a,b)=>Math.abs(Date.parse(a.started_at||a.created_at)-stamp(e))-Math.abs(Date.parse(b.started_at||b.created_at)-stamp(e)))[0]
  const previous=e.code==='checkout_done'?candidates.filter(r=>r.ended&&Math.abs(stamp(r)-stamp(e))<2000).at(-1):undefined
  const target=sameCode.length===1?sameCode[0]:previous||next
  if(target)target.raw.push(e)
  else visible.push({...e,started_at:null,raw:[e],ended:false})
 }
 for(const row of visible){
  row.raw.sort((a:Row,b:Row)=>stamp(a)-stamp(b))
  row.spinning=live&&!row.ended&&row.source!=='business'&&['running','waiting'].includes(row.state)&&round(row)===lastRound&&row.raw.some((r:Row)=>r===latest||r.id&&r.id===latest?.id)
  row.displayState=row.spinning?'running':!row.ended&&['running','waiting'].includes(row.state)?'recorded':row.state
  row.ended_at=row.ended?row.created_at:null
  row.duration_ms=row.started_at&&row.ended_at?Math.max(0,Date.parse(row.ended_at)-Date.parse(row.started_at)):Number(row.elapsed_ms)||null
  if(row.code?.startsWith('post_payment_wait'))row.displayLabel='付款后等待'
  else if(row.phase==='funds')row.displayLabel=({hold:'冻结服务费',charge:'结算服务费',release:'释放冻结服务费'} as Row)[row.code]||row.label
  else if(row.code==='payment_succeeded')row.displayLabel='确认付款成功'
  else if(row.code==='activation_confirmed')row.displayLabel='确认订阅已生效'
  else if(row.code==='renewal_disabled')row.displayLabel='确认自动续费已关闭'
  else if(row.code==='renewal_kept')row.displayLabel='保留自动续费'
 }
 return visible.sort((a,b)=>Date.parse(a.started_at||a.created_at)-Date.parse(b.started_at||b.created_at))
}
