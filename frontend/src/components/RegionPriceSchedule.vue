<script setup lang="ts">
import {ref} from 'vue'
import {Clock3,RefreshCw} from 'lucide-vue-next'
import {api,notify,type Row} from '../lib'
import {priceTime} from '../priceReference'
import Modal from './Modal.vue'
const open=ref(false),loading=ref(false),busy=ref(false),error=ref(''),form=ref<Row>({prices_enabled:true,price_interval_hours:1,fx_enabled:true,fx_interval_days:1}),status=ref<Row>({})
async function load(){loading.value=true;error.value='';try{const r=await api('/admin/region-prices');form.value=r.config;status.value=r.status}catch(e){error.value=(e as Error).message}finally{loading.value=false}}
async function show(){open.value=true;await load()}
async function save(){busy.value=true;error.value='';try{const r=await api('/admin/region-prices','PUT',form.value);form.value=r.config;status.value=r.status;notify('地区价格定时配置已保存')}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function refresh(kind:string){busy.value=true;error.value='';try{await api('/admin/region-prices/refresh','POST',{kind});notify('更新已安排，请稍后刷新查看结果');await load()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
</script>
<template>
<button type="button" class="btn secondary" @click="show"><Clock3 :size="16"/>地区价格定时配置</button>
<Modal v-if="open" title="地区价格定时配置" @close="!busy&&(open=false)">
<p class="muted">查询范围跟随订阅地区列表，新增、删除自动生效。报价仅作参考，不影响实际下单价格。</p>
<p v-if="error" class="alert danger" role="alert">{{error}}</p>
<form @submit.prevent="save">
<div class="schedule-section"><label class="checkbox-label"><input type="checkbox" v-model="form.prices_enabled"/>定时更新地区套餐价格</label><label>价格更新周期（小时）<input type="number" min="1" max="168" step="1" required v-model.number="form.price_interval_hours"/><small>默认每 1 小时更新一次，各地区依次获取全部套餐。</small></label></div>
<div class="schedule-section"><label class="checkbox-label"><input type="checkbox" v-model="form.fx_enabled"/>定时更新参考汇率</label><label>汇率更新周期（天）<input type="number" min="1" max="30" step="1" required v-model.number="form.fx_interval_days"/><small>默认每天 1 次，所有地区共用同一份汇率。</small></label></div>
<div class="schedule-status"><div><span>订阅地区 / 已有报价</span><b>{{status.countries??'—'}} / {{status.quoted_countries??'—'}}</b></div><div><span>待查询地区</span><b>{{status.pending??'—'}}</b></div><div><span>最近价格获取成功</span><b>{{priceTime(status.last_price_success_at)}}</b></div><div><span>下次价格更新</span><b>{{form.prices_enabled?priceTime(status.next_price_at):'已关闭定时'}}</b></div><div><span>汇率数据时间</span><b>{{priceTime(status.fx_updated_at)}}</b></div><div><span>下次汇率更新</span><b>{{form.fx_enabled?priceTime(status.next_fx_at):'已关闭定时'}}</b></div></div>
<p v-if="status.fx_error" class="alert warning">汇率更新：{{status.fx_error}}。保留上次成功数据。</p>
<details v-if="status.errors?.length" class="schedule-errors"><summary>{{status.errors.length}} 个地区待重试</summary><p v-for="r in status.errors" :key="r.country">{{r.name}}（{{r.country}}）：{{r.message}}</p></details>
<div class="schedule-actions"><button type="button" class="btn secondary" :disabled="busy||loading" @click="refresh('prices')">立即更新价格</button><button type="button" class="btn secondary" :disabled="busy||loading" @click="refresh('fx')">立即更新汇率</button><button type="button" class="text-btn" :disabled="busy||loading" @click="load"><RefreshCw :size="14"/>刷新状态</button></div>
<div class="form-footer"><button class="btn primary" :disabled="busy||loading">{{busy?'保存中…':'保存配置'}}</button></div>
</form>
</Modal>
</template>
<style scoped>
.schedule-section{padding:16px 0;border-bottom:1px solid var(--line)}.schedule-section label:last-child{margin-bottom:0}.schedule-status{margin:20px 0;display:grid;gap:12px}.schedule-status>div{display:flex;justify-content:space-between;gap:15px;font-size:12px}.schedule-status span{color:var(--muted)}.schedule-status b{text-align:right;font-weight:500}.schedule-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.schedule-errors{font-size:12px;margin:12px 0}.schedule-errors p{margin:8px 0;color:var(--muted)}
</style>

