<script setup lang="ts">
import {ref,watch,onUnmounted} from 'vue';
import {api,cash as formatAmount} from '../lib';
import {type Period,periods,todayBeijing,shiftPeriod,beijingTime} from '../lib/report-period';
defineEmits<{back:[]}>();
type Item={id:string|number;prefix:string;email:string;name:string;note:string;amount:number;redeemed_at:string};
type Report={items:Item[];summary:{redemptions:number;codes:number;accounts:number;amount:number};total:number;start_date:string;end_date:string};
const period=ref<Period>('day'),anchor=ref(todayBeijing()),search=ref(''),query=ref(''),page=ref(1),loading=ref(false),error=ref(''),report=ref<Report>();
let controller:AbortController|undefined;
async function load(){
 controller?.abort();const current=new AbortController();controller=current;loading.value=true;error.value='';report.value=undefined;
 try{
  if(!anchor.value)throw Error('请选择统计日期');
  const params=new URLSearchParams({period:period.value,date:anchor.value,q:query.value,page:String(page.value),page_size:'50'});
  const data=await api(`/admin/codes/usage?${params}`,'GET',undefined,{},current.signal);
  if(controller===current)report.value=data;
 }catch(e){if(controller===current&&(e as Error).name!=='AbortError')error.value=(e as Error).message}
 finally{if(controller===current)loading.value=false}
}
watch([period,anchor,query],()=>{page.value=1},{flush:'sync'});
watch([period,anchor,query,page],load,{immediate:true});
onUnmounted(()=>controller?.abort());
function shift(n:number){anchor.value=shiftPeriod(anchor.value,period.value,n)}
function searchNow(){if(query.value===search.value.trim())load();else query.value=search.value.trim()}
</script>
<template>
 <section class="usage-report" :aria-busy="loading">
  <div class="usage-heading"><div><h2>兑换码使用情况</h2><p>按实际兑换时间查看入账明细与统计</p></div><div class="usage-nav"><button class="usage-btn" @click="$emit('back')">返回兑换码</button><button class="usage-btn" :disabled="loading" @click="load">刷新统计</button></div></div>
  <div class="usage-controls">
   <div class="usage-segments" role="group" aria-label="统计周期"><button v-for="p in periods" :key="p.key" :aria-pressed="period===p.key" @click="period=p.key">{{p.label}}</button></div>
   <label class="usage-date">统计日期<input v-model="anchor" aria-label="统计日期" type="date" min="1900-01-01" max="9998-12-31"/></label>
   <div class="usage-nav"><button class="usage-btn" @click="shift(-1)">上一期</button><button class="usage-btn" @click="anchor===todayBeijing()?load():anchor=todayBeijing()">本期</button><button class="usage-btn" @click="shift(1)">下一期</button></div>
   <form class="usage-search" @submit.prevent="searchNow"><input v-model="search" type="search" maxlength="200" aria-label="搜索兑换使用记录" placeholder="搜索码标识、备注 / 批次、兑换人"/><button class="usage-btn" type="submit">查询</button><button v-if="search||query" class="usage-btn" type="button" @click="search='';query=''">清空</button></form>
  </div>
  <div v-if="error" class="usage-error" role="alert">{{error}} <button class="usage-btn" @click="load">重试</button></div>
  <div v-if="loading" class="usage-loading" role="status">正在加载使用情况…</div>
  <template v-if="report">
   <div class="usage-caption"><strong>{{report.start_date}} 至 {{report.end_date}}</strong><span>北京时间 · 每周从周一开始 · 汇总覆盖全部筛选结果</span></div>
   <div class="usage-metrics"><article><span>兑换次数</span><strong>{{report.summary.redemptions}}</strong></article><article><span>已使用兑换码</span><strong>{{report.summary.codes}}</strong></article><article><span>使用人数</span><strong>{{report.summary.accounts}}</strong></article><article><span>入账积分</span><strong>{{formatAmount(report.summary.amount)}}</strong></article></div>
   <div class="usage-table-wrap"><table class="usage-table"><thead><tr><th>兑换时间（北京）</th><th>兑换码标识</th><th>兑换账户</th><th>入账积分</th><th>备注 / 批次</th></tr></thead><tbody><tr v-for="r in report.items" :key="r.id"><td>{{beijingTime(r.redeemed_at)}}</td><td>{{r.prefix}}</td><td>{{r.email||'已删除账户'}}<small>{{r.name}}</small></td><td>{{formatAmount(r.amount)}}</td><td>{{r.note||'—'}}</td></tr><tr v-if="!report.items.length"><td colspan="5" class="usage-empty">所选期间没有符合条件的兑换记录</td></tr></tbody></table></div>
   <div class="usage-pagination"><span>共 {{report.total}} 条 · 第 {{page}} 页</span><div class="usage-nav"><button class="usage-btn" :disabled="page===1" @click="page--">上一页</button><button class="usage-btn" :disabled="page*50>=report.total" @click="page++">下一页</button></div></div>
   <p class="usage-muted">按实际兑换入账统计，保留历史记录；完整兑换码不在此页面展示。</p>
  </template>
 </section>
</template>
<style scoped>.usage-report{min-width:0}.usage-heading,.usage-controls,.usage-nav,.usage-search,.usage-caption,.usage-pagination{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.usage-heading{justify-content:space-between;margin-bottom:18px}.usage-heading h2{font-size:20px;margin:0 0 6px}.usage-heading p,.usage-caption,.usage-muted{color:#718078;font-size:12px}.usage-controls{padding:16px;border:1px solid #dee6e1;border-radius:12px;background:#fff;margin-bottom:16px}.usage-segments{display:flex;gap:3px;border-radius:8px;padding:4px;background:#f0f4f1}.usage-report button{cursor:pointer}.usage-segments button{border:0;background:transparent;color:#63796c;padding:9px 15px;border-radius:6px;font-size:13px}.usage-segments button[aria-pressed=true]{background:#245442;color:#fff}.usage-report button:disabled{opacity:.45;cursor:not-allowed}.usage-date{display:flex;align-items:center;gap:8px;font-size:13px}.usage-controls input{border:1px solid #dce5df;border-radius:6px;padding:9px;background:white;color:#263c31;min-width:0}.usage-search{margin-left:auto}.usage-search input{width:235px}.usage-report .usage-btn{padding:9px 13px;border:1px solid #d5dfd8;background:#fff;border-radius:7px;color:#294a39;font-size:13px;white-space:nowrap}.usage-caption{margin:14px 0}.usage-caption strong{color:#2c4939;font-size:14px}.usage-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0}.usage-metrics article{border:1px solid #e0e7e2;border-radius:12px;background:linear-gradient(135deg,#f4f9f5,#fff);padding:20px}.usage-metrics span{font-size:12px;color:#6c7e70}.usage-metrics strong{display:block;font-size:28px;color:#254d37;margin-top:12px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.usage-table-wrap{overflow:auto;border:1px solid #dfe6e1;border-radius:10px;background:white}.usage-table{width:100%;min-width:780px;border-collapse:collapse;text-align:left;font-size:13px}.usage-table th{background:#f5f8f5;color:#65796b;font-weight:500}.usage-table th,.usage-table td{padding:14px 16px;border-bottom:1px solid #e9edea;vertical-align:top}.usage-table small{display:block;color:#718078;margin-top:5px}.usage-table td{max-width:300px;overflow-wrap:anywhere}.usage-empty{padding:40px!important;text-align:center;color:#7b887e}.usage-pagination{justify-content:space-between;margin:16px 0;font-size:13px;color:#718078}.usage-error{padding:14px;background:#fff1ed;color:#a33b24;border-radius:8px;margin:14px 0}.usage-loading{padding:35px;text-align:center;color:#718078}@media(max-width:700px){.usage-metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.usage-metrics article{padding:14px}.usage-metrics strong{font-size:23px}.usage-controls{padding:12px}.usage-segments{width:100%}.usage-segments button{flex:1;padding:9px}.usage-search{width:100%;margin:0}.usage-search input{flex:1;width:0}.usage-heading{align-items:flex-start}}</style>
