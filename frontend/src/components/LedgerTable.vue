<script setup lang="ts">
import {computed,ref} from 'vue'
import {Search,ArrowUpRight,ArrowDownLeft,ArrowUpRight as Outgoing,LockKeyhole,RotateCcw,ReceiptText} from 'lucide-vue-next'
import {cash,date,labels,type Row} from '../lib'
import {deltaTone,signed,upstreamAmount,discountText} from '../record-format'
import RecordsTable from './RecordsTable.vue'
const props=defineProps<{rows:Row[];loading:boolean}>()
defineEmits<{order:[row:Row]}>()
const q=ref(''),kind=ref(''),start=ref(''),end=ref('')
const kinds=['topup','redeem','freeze','unfreeze','charge','refund','adjustment']
const invalidDates=computed(()=>!!(start.value&&end.value&&start.value>end.value))
const hasFilters=computed(()=>!!(q.value||kind.value||start.value||end.value))
const filtered=computed(()=>{
 if(invalidDates.value)return []
 const from=start.value?new Date(`${start.value}T00:00:00`).getTime():-Infinity
 const until=end.value?new Date(`${end.value}T00:00:00`):null
 if(until)until.setDate(until.getDate()+1)
 return props.rows.filter(r=>{
  const when=new Date(r.created_at).getTime(),o=r.related_order
  return (!q.value||`${r.id} ${r.description} ${o?.id||''} ${o?.email||''} ${o?.product_name||''}`.toLowerCase().includes(q.value.trim().toLowerCase()))&&(!kind.value||r.kind===kind.value)&&when>=from&&when<(until?.getTime()??Infinity)
 })
})
const totals=computed(()=>filtered.value.reduce((s,r)=>{
 if(['topup','redeem'].includes(r.kind))s.credit+=Number(r.amount)
 if(r.kind==='charge')s.spent-=Number(r.amount)
 if(r.kind==='freeze')s.held+=Math.max(0,Number(r.frozen_delta||0))
 if(['unfreeze','refund'].includes(r.kind))s.returned+=Math.max(0,Number(r.available_delta||0))
 return s
},{credit:0,spent:0,held:0,returned:0}))
function reset(){q.value='';kind.value='';start.value='';end.value=''}
const source=(r:Row)=>r.related_order?'订阅订单':({topup:'人工充值',redeem:'兑换码入账',adjustment:'运营调整',refund:'服务退费',charge:'服务结算',freeze:'服务费冻结',unfreeze:'服务费解冻'} as Row)[r.kind]||'账户变动'
const columns=[{key:'kind',label:'流水类型'},{key:'source',label:'业务来源 / 服务费'},{key:'payment',label:'关联订单付款（当前）'},{key:'available',label:'可用变动'},{key:'frozen',label:'冻结变动'},{key:'balance',label:'变动后余额'},{key:'related',label:'关联记录',wide:true},{key:'description',label:'说明',wide:true},{key:'time',label:'发生时间'}]
</script>
<template>
 <section class="panel record-panel ledger-panel">
  <div class="records-heading"><div><h2>资金明细</h2><p>冻结转入待结算金额；确认扣费从冻结金额结算，不重复扣减可用余额。</p></div><span class="pill">账户单位 · R</span></div>
  <div class="ledger-totals"><div><span><ArrowDownLeft :size="15"/>充值入账</span><b>{{cash(totals.credit)}} <em>R</em></b></div><div><span><Outgoing :size="15"/>服务扣费</span><b>{{cash(totals.spent)}} <em>R</em></b></div><div><span><LockKeyhole :size="15"/>累计冻结</span><b>{{cash(totals.held)}} <em>R</em></b></div><div><span><RotateCcw :size="15"/>解冻 / 退费</span><b>{{cash(totals.returned)}} <em>R</em></b></div></div>
  <div class="records-filters">
   <label class="records-search"><span class="sr-only">搜索订单号、邮箱或流水说明</span><Search :size="16"/><input v-model="q" type="search" placeholder="订单号 / 邮箱 / 流水说明"/></label>
   <label><span class="sr-only">流水类型</span><select v-model="kind" aria-label="流水类型"><option value="">全部流水类型</option><option v-for="k in kinds" :key="k" :value="k">{{labels[k]}}</option></select></label>
   <div class="records-date-range"><label><span class="sr-only">开始日期</span><input v-model="start" type="date" aria-label="开始日期" :max="end||undefined"/></label><span>至</span><label><span class="sr-only">结束日期</span><input v-model="end" type="date" aria-label="结束日期" :min="start||undefined"/></label></div>
   <button v-if="hasFilters" class="text-btn records-reset" @click="reset">重置</button>
  </div>
  <p v-if="invalidDates" class="records-date-error" role="alert">结束日期不能早于开始日期。</p>
  <RecordsTable :rows="filtered" :columns="columns" :loading="loading" :filtered="hasFilters" scope="在最近 300 条流水内筛选；汇总随筛选更新" @reset="reset">
   <template #kind="{row}"><span class="ledger-kind" :class="'ledger-'+row.kind"><ReceiptText :size="13"/>{{labels[row.kind]||'账户变动'}}</span><small class="mono">#{{row.id}}</small></template>
   <template #source="{row}"><b>{{row.related_order?.product_name||source(row)}}</b><small v-if="row.related_order">服务费 {{cash(row.related_order.fee)}} R</small><small v-if="row.related_order" :class="{'positive':Number(row.related_order.price_snapshot?.standard)>row.related_order.fee}">{{discountText(row.related_order)}}</small><small v-else>账户资金</small></template>
   <template #payment="{row}"><b class="record-money">{{upstreamAmount(row.payment).value}}</b><small>{{row.related_order?upstreamAmount(row.payment).label:'不涉及上游付款'}}</small><small v-if="row.payment?.last4" class="mono">自备卡 · 尾号 {{row.payment.last4}}</small></template>
   <template #available="{row}"><b class="record-money" :class="deltaTone(row.available_delta)">{{signed(row.available_delta)}}</b></template>
   <template #frozen="{row}"><b class="record-money" :class="deltaTone(row.frozen_delta)">{{signed(row.frozen_delta)}}</b></template>
   <template #balance="{row}"><b class="record-money">{{cash(row.balance_after)}}</b><small>冻结 {{cash(row.frozen_after)}} R</small></template>
   <template #related="{row}"><template v-if="row.related_order"><button class="text-btn record-order-link mono" @click="$emit('order',row.related_order)">{{row.related_order.id}}<ArrowUpRight :size="13"/></button><small>{{row.related_order.email}}</small></template><span v-else class="muted">{{source(row)}}</span></template>
   <template #description="{row}"><p class="record-description">{{row.description||'—'}}</p></template>
   <template #time="{row}"><time>{{date(row.created_at)}}</time></template>
  </RecordsTable>
 </section>
</template>
