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
