<script setup lang="ts">
import {computed,ref} from 'vue'
import {Search} from 'lucide-vue-next'
import {cash,date,labels,type Row} from '../lib'
import {deltaTone,signed} from '../record-format'
import RecordsTable from './RecordsTable.vue'
defineEmits<{cancel:[id:string]}>()
const props=defineProps<{rows:Row[];loading:boolean;busy:boolean}>()
const query=ref(''),source=ref('')
const filtered=computed(()=>props.rows.filter(r=>(!source.value||r.source===source.value)&&`${r.related} ${r.description}`.toLowerCase().includes(query.value.trim().toLowerCase())))
const columns=[{key:'type',label:'类型'},{key:'source',label:'充值来源'},{key:'fees',label:'支付费用'},{key:'available',label:'可用变动'},{key:'frozen',label:'冻结变动'},{key:'balance',label:'变动后余额'},{key:'related',label:'关联记录',wide:true},{key:'description',label:'说明',wide:true},{key:'time',label:'时间'},{key:'action',label:'操作'}]
function reset(){query.value='';source.value=''}
</script>
<template><section class="panel record-panel">
 <div class="records-heading"><div><h2>充值记录</h2><p>查看兑换码入账与人工充值申请，到账记录与资金流水一致。</p></div><span class="pill">账户单位 · 积分</span></div>
 <div class="records-filters"><label class="records-search"><Search :size="16"/><input v-model="query" type="search" placeholder="兑换码标识 / 申请编号 / 说明" aria-label="搜索充值记录"/></label><select v-model="source" aria-label="充值来源"><option value="">全部来源</option><option value="redeem">CDK 兑换</option><option value="manual">人工充值</option></select><button v-if="query||source" class="text-btn" @click="reset">重置</button></div>
 <RecordsTable :rows="filtered" :columns="columns" :loading="loading" :filtered="!!(query||source)" scope="最近 300 条充值记录" @reset="reset">
  <template #type="{row}"><span class="badge" :class="row.status==='approved'?'active':row.status">{{row.status==='approved'?'积分充值':labels[row.status]||row.status}}</span></template>
  <template #source="{row}"><b>{{row.source==='redeem'?'CDK 兑换':'人工充值'}}</b><small>{{row.status==='approved'?'到账':'申请'}} {{cash(row.amount)}} 积分</small></template>
  <template #fees="{row}"><b>{{row.source==='redeem'?'CDK':'人工核实'}}</b><small>{{row.source==='redeem'?'不产生现金费用':'付款信息以实际转账凭证为准'}}</small></template>
  <template #available="{row}"><b class="record-money" :class="deltaTone(row.available_delta)">{{row.available_delta==null?'—':signed(row.available_delta)}}</b></template>
  <template #frozen="{row}"><b class="record-money" :class="deltaTone(row.frozen_delta)">{{row.frozen_delta==null?'—':signed(row.frozen_delta)}}</b></template>
  <template #balance="{row}"><template v-if="row.balance_after!=null"><b class="record-money">{{cash(row.balance_after-row.frozen_after)}} 积分</b><small>冻结 {{cash(row.frozen_after)}} 积分</small></template><span v-else class="muted">尚未入账</span></template>
  <template #related="{row}"><span class="mono">{{row.related}}</span><small>{{row.source==='redeem'?'兑换码（已脱敏）':'充值申请'}}</small></template>
  <template #description="{row}"><p class="record-description">{{row.description||'等待核实到账'}}</p></template>
  <template #time="{row}"><time>{{date(row.created_at)}}</time><small>{{row.status==='approved'?'入账时间':'申请时间'}}</small></template>
  <template #action="{row}"><button v-if="row.source==='manual'&&row.status==='pending'" class="text-btn" :disabled="busy" @click="$emit('cancel',row.topup_id)">撤回申请</button><span v-else class="muted">—</span></template>
 </RecordsTable>
</section></template>
