import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {schemaFields,codeSample,requestExamples} from '../src/apiDocs.ts'
const spec=JSON.parse(readFileSync(new URL('../../backend/openapi.json',import.meta.url),'utf8'))
const operations=Object.entries(spec.paths).flatMap(([path,v])=>Object.entries(v).map(([method,op])=>({path,method,op})))

test('nested references, union objects and arrays expose customer fields',()=>{
  const order=schemaFields(spec,{$ref:'#/components/schemas/CreateOrder'})
  for(const field of ['session.user.email','session.account.id','session.sessionToken','card.number','card.cvc'])assert.ok(order.some(f=>f.name===field),field)
  const list=schemaFields(spec,{$ref:'#/components/schemas/OrderPage'})
  assert.ok(list.some(f=>f.name==='items[].payment.state'))
})

test('all operations produce bounded, authenticated examples',()=>{
  for(const x of operations)for(const lang of ['curl','python','node']){
    const code=codeSample(x,'https://pay.example.test',lang)
    assert.ok(code.includes('XUFU_API_KEY'))
    assert.ok(!code.includes('{id}'))
    assert.ok(!code.includes('undefined'))
    assert.ok(code.includes(lang==='curl'?'--max-time':lang==='node'?'AbortSignal.timeout':'timeout=30'))
    if(x.op.parameters?.some(p=>p.name==='Idempotency-Key'))assert.ok(code.includes('Idempotency-Key'))
  }
})

test('link and subscription examples remain separate and all guides resolve',()=>{
  for(const guide of spec['x-guides']){
    const create=operations.find(x=>x.op.operationId===guide.create)
    assert.ok(create)
    assert.ok(requestExamples(create)[guide.example])
    assert.ok(operations.some(x=>x.op.operationId===guide.detail))
  }
  const create=operations.find(x=>x.op.operationId==='create_order')
  assert.equal(requestExamples(create).link.value.card,undefined)
  assert.equal(requestExamples(create).subscription.value.accepted,true)
  assert.ok(!codeSample(create,'https://pay.example.test','node','link').includes('YOUR_CARD_NUMBER'))
})
