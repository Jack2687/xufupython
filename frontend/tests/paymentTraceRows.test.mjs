import {test} from 'node:test'
import assert from 'node:assert/strict'
import {traceRows} from '../src/paymentTraceRows.ts'
const e=(id,state,phase='network',round=1)=>({id,state,phase,round,source:'worker',label:'分配办理网络',created_at:`2026-11-04T09:00:0${id}Z`})
test('operation start and finish share one row with raw evidence',()=>{
 const rows=traceRows([e(1,'running'),e(2,'completed')],true)
 assert.equal(rows.length,1);assert.equal(rows[0].raw.length,2);assert.equal(rows[0].spinning,false);assert.equal(rows[0].started_at,'2026-11-04T09:00:01Z')
})
test('only current unfinished operation spins; completed orders stop',()=>{
 const events=[e(1,'running','checkout'),e(2,'completed','checkout'),e(3,'running','payment')]
 assert.deepEqual(traceRows(events,true).map(r=>r.spinning),[false,true])
 assert.ok(traceRows(events,false).every(r=>!r.spinning))
 assert.equal(traceRows(events,false).at(-1).displayState,'recorded')
})
test('different rounds and different request ids stay separate',()=>{
 const events=[{...e(1,'running','payment'),request_id:'a'},{...e(2,'completed','payment'),request_id:'b'},e(3,'running','payment',2)]
 const rows=traceRows(events,true);assert.equal(rows.length,3);assert.deepEqual(rows.map(r=>r.spinning),[false,false,true])
})

test('payment wrappers and business notices share the actual request, preserving all raw events',()=>{
 const events=[{...e(1,'recorded','business'),source:'business',code:'confirming'},e(2,'running','confirming'),
 {...e(3,'running','confirming'),code:'request_started',diagnostics:{request_kind:'payment_confirm'}},
 {...e(4,'completed','confirming'),code:'response_received',http_status:200,diagnostics:{request_kind:'payment_confirm'}}]
 const rows=traceRows(events,false)
 assert.equal(rows.length,1);assert.equal(rows[0].raw.length,4)
 assert.equal(rows[0].started_at,events[2].created_at);assert.equal(rows[0].ended_at,events[3].created_at)
 assert.equal(rows[0].duration_ms,1000);assert.equal(rows[0].spinning,false)
 assert.deepEqual(rows.flatMap(r=>r.raw.map(e=>e.id)).sort(),events.map(e=>e.id).sort())
})

test('401 recovery preserves failed and successful requests as distinct operations',()=>{
 const req=(id,state,kind,status=0)=>({...e(id,state,kind==='session_read'?'session_refresh':'renewal'),diagnostics:{request_kind:kind},http_status:status})
 const events=[req(1,'running','renewal_read'),req(2,'failed','renewal_read',401),req(3,'running','session_read'),req(4,'completed','session_read',200),req(5,'running','renewal_read'),req(6,'completed','renewal_read',200)]
 const rows=traceRows(events,false)
 assert.deepEqual(rows.map(r=>r.http_status),[401,200,200]);assert.equal(rows[0].displayState,'failed')
 assert.equal(rows.flatMap(r=>r.raw).length,events.length);assert.ok(rows.every(r=>!r.spinning))
})

test('post payment wait is never labeled as an authorization refresh',()=>{
 const rows=traceRows([{...e(1,'waiting','session_refresh'),code:'post_payment_wait'},{...e(2,'completed','session_refresh'),code:'post_payment_wait_completed'}],false)
 assert.equal(rows.length,1);assert.equal(rows[0].displayLabel,'付款后等待');assert.equal(rows[0].duration_ms,1000)
})

test('unpaired records have no invented start, finish or duration',()=>{
 const rows=traceRows([{...e(1,'recorded','business'),source:'business'},e(2,'completed','complete')],false)
 assert.equal(rows[0].started_at,null);assert.equal(rows[0].ended_at,null);assert.equal(rows[0].duration_ms,null)
 assert.equal(rows[1].started_at,null);assert.equal(rows[1].ended_at,rows[1].created_at)
})
