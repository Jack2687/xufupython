<script setup lang="ts">
import {computed,ref} from 'vue'
import {Search,RefreshCw,Plus,Copy,ArrowUpRight,CreditCard} from 'lucide-vue-next'
import {cash,date,action,copy,orderLabel,orderTone,regionLabel,type Row} from '../lib'
import {feeStates,paymentStates,renewalStates,paymentState,renewalState,upstreamAmount,discountText} from '../record-format'
import RecordsTable from './RecordsTable.vue'
const props=defineProps<{rows:Row[];loading:boolean}>()
defineEmits<{order:[row:Row];refresh:[]}>()
const q=ref(''),payment=ref(''),renewal=ref(''),product=ref(''),status=ref('')
const products=computed(()=>[...new Set(props.rows.map(r=>r.product_name))])
const filtered=computed(()=>props.rows.filter(r=>{
 const attention=r.status==='review'||r.activation==='review'||r.renewal==='review'||r.payment?.state==='action_required'
 return (!q.value||`${r.id} ${r.email}`.toLowerCase().includes(q.value.trim().toLowerCase()))
  &&(!payment.value||paymentState(r)===payment.value)&&(!renewal.value||renewalState(r)===renewal.value)
  &&(!product.value||r.product_name===product.value)&&(!status.value||(status.value==='attention'?attention:r.status===status.value))
}))
const hasFilters=computed(()=>!!(q.value||payment.value||renewal.value||product.value||status.value))
function reset(){q.value='';payment.value='';renewal.value='';product.value='';status.value=''}
const columns=[{key:'order',label:'订单 / 订阅账号',wide:true},{key:'product',label:'套餐 / 地区'},{key:'fee',label:'服务费 / 优惠'},{key:'payment',label:'支付卡 / 上游金额'},{key:'status',label:'付款 / 办理状态',wide:true},{key:'renewal',label:'自动续费'},{key:'time',label:'更新时间'},{key:'action',label:'操作',wide:true}]
</script>
<template>
 <div class="page-heading"><div><span class="eyebrow">EVERY ORDER, IN ONE PLACE</span><h1>订单中心</h1><p>查订单、核付款、看续费，每一笔都有据可循。</p></div><a href="#/subscribe" class="btn primary"><Plus :size="17"/>办理订阅</a></div>
 <section class="panel record-panel">
  <div class="records-heading"><div><h2>全部订单</h2><p>付款、生效与续费分别记录，查看详情可跟进完整进度。</p></div><button class="btn secondary" :disabled="loading" @click="$emit('refresh')"><RefreshCw :size="15" :class="{'records-spin':loading}"/>刷新</button></div>
  <div class="records-filters">
   <label class="records-search"><span class="sr-only">订单号或订阅邮箱</span><Search :size="16"/><input v-model="q" type="search" placeholder="搜索订单号 / 订阅邮箱"/></label>
   <label><span class="sr-only">付款状态</span><select v-model="payment" aria-label="付款状态"><option value="">全部付款状态</option><option v-for="(name,value) in paymentStates" :key="value" :value="value">{{name}}</option></select></label>
   <label><span class="sr-only">续费状态</span><select v-model="renewal" aria-label="续费状态"><option value="">全部续费状态</option><option v-for="(name,value) in renewalStates" :key="value" :value="value">{{name}}</option></select></label>
   <label><span class="sr-only">订阅套餐</span><select v-model="product" aria-label="订阅套餐"><option value="">全部套餐</option><option v-for="name in products" :key="name" :value="name">{{name}}</option></select></label>
   <label><span class="sr-only">办理状态</span><select v-model="status" aria-label="办理状态"><option value="">全部办理状态</option><option value="queued">排队中</option><option value="processing">办理中</option><option value="succeeded">已完成</option><option value="failed">未完成</option><option value="attention">需要关注</option><option value="draft">草稿</option><option value="cancelled">已撤回</option></select></label>
   <button v-if="hasFilters" class="text-btn records-reset" @click="reset">重置</button>
  </div>
  <RecordsTable :rows="filtered" :columns="columns" :loading="loading" :filtered="hasFilters" scope="在最近 200 笔订单内筛选" @reset="reset">
   <template #order="{row}"><div class="record-id"><span class="mono">{{row.id}}</span><button class="icon-btn" :aria-label="`复制订单号 ${row.id}`" @click="action(()=>copy(row.id))"><Copy :size="13"/></button></div><b class="record-email">{{row.email}}</b><small>{{row.source==='api'?'API 接入':'工作台提交'}} · {{row.workflow==='subscription'?'订阅办理':row.workflow==='link'?'结账链接':'历史订单'}}</small></template>
   <template #product="{row}"><b>{{row.product_name}}</b><small>{{regionLabel(row.country)}}<template v-if="row.seats>1"> · {{row.seats}} 席</template></small></template>
   <template #fee="{row}"><b class="record-money">{{cash(row.fee)}} <em>R</em></b><small :class="{'positive':Number(row.price_snapshot?.standard)>row.fee}">{{discountText(row)}}</small><small>{{feeStates[row.payment?.fee_state]||'服务费未冻结'}}</small></template>
   <template #payment="{row}"><b class="record-money">{{upstreamAmount(row.payment).value}}</b><small>{{upstreamAmount(row.payment).label}}</small><span class="record-card" v-if="row.payment?.last4"><CreditCard :size="13"/>•••• {{row.payment.last4}}</span></template>
   <template #status="{row}"><span class="badge record-status" :class="'payment-'+paymentState(row)">{{paymentStates[paymentState(row)]||'等待处理'}}</span><small class="record-result" :class="orderTone(row)">{{orderLabel(row)}}</small><small v-if="row.payment?.message" class="record-message">{{row.payment.message}}</small></template>
   <template #renewal="{row}"><span class="badge record-status" :class="'renewal-'+renewalState(row)">{{renewalStates[renewalState(row)]||'等待处理'}}</span><small v-if="row.workflow==='subscription'" class="record-activation" :class="row.activation">{{row.activation==='active'?'订阅已生效':row.activation==='review'?'订阅生效待核实':row.activation==='not_checked'?'历史生效状态未记录':'订阅待生效'}}</small></template>
   <template #time="{row}"><time>{{date(row.updated_at)}}</time><small>创建 {{date(row.created_at)}}</small></template>
   <template #action="{row}"><button class="text-btn record-detail" @click="$emit('order',row)">查看详情<ArrowUpRight :size="14"/></button></template>
  </RecordsTable>
 </section>
</template>
