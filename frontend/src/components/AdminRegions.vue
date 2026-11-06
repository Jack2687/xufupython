<script setup lang="ts">
import {ref,computed,onMounted} from 'vue'
import {Plus,Search,RefreshCw,Pencil,Trash2} from 'lucide-vue-next'
import {api,notify,type Row} from '../lib'
import Modal from './Modal.vue'
const emit=defineEmits<{'changed':[]}>()
const rows=ref<Row[]>([]),search=ref(''),status=ref(''),loading=ref(false),busy=ref(false),error=ref(''),modal=ref(''),editing=ref(''),form=ref<Row>({})
const selected=ref<string[]>([]),currencies=ref<string[]>([]),mode=ref('manual'),batchResult=ref<Row[]>([])
const visible=computed(()=>rows.value.filter(r=>(!status.value||(status.value==='missing'?!r.currency:status.value==='unavailable'?!r.proxy_available:r.enabled===(status.value==='enabled')))&&[r.code,r.name,r.currency].some(v=>String(v).toLowerCase().includes(search.value.trim().toLowerCase()))))
async function load(){loading.value=true;error.value='';try{const r=await api('/admin/regions');rows.value=r.items;currencies.value=r.currencies;mode.value=r.proxy_mode;selected.value=[]}catch(e){error.value=(e as Error).message}finally{loading.value=false}}
function edit(row?:Row){editing.value=row?.code||'';form.value=row?{...row}:{code:'',name:'',currency:'',sort:rows.value.length,enabled:true};error.value='';modal.value='edit'}
async function mutate(fn:()=>Promise<unknown>){if(busy.value)return;busy.value=true;error.value='';try{await fn();modal.value='';notify('订阅地区已更新');await load();emit('changed')}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function batch(enabled:boolean){await mutate(async()=>{const r=await api('/admin/regions/batch','POST',{codes:selected.value,enabled});batchResult.value=r.skipped;notify(`已处理 ${r.changed} 个地区，跳过 ${r.skipped.length} 个`)})}
function selectVisible(event:Event){selected.value=(event.target as HTMLInputElement).checked?visible.value.map(r=>r.code):[]}
function save(){return mutate(()=>api('/admin/regions'+(editing.value?'/'+editing.value:''),editing.value?'PUT':'POST',{...form.value,code:form.value.code.trim().toUpperCase(),currency:form.value.currency.trim().toUpperCase()}))}
onMounted(load)
</script>
<template>
  <div class="filter-strip"><div class="search-box"><Search :size="16"/><input v-model="search" placeholder="搜索国家、代码或币种" aria-label="搜索订阅地区"/></div><select v-model="status" aria-label="地区状态"><option value="">全部状态</option><option value="enabled">已启用</option><option value="disabled">已禁用</option><option value="missing">币种待配置</option><option value="unavailable">当前代理不支持</option></select><button class="btn secondary" :disabled="loading||busy" @click="load"><RefreshCw :size="16"/>刷新</button><button class="btn primary" :disabled="busy" @click="edit()"><Plus :size="16"/>新增地区</button><button class="btn secondary" :disabled="!selected.length||busy" @click="batch(true)">批量开放（{{selected.length}}）</button><button class="btn secondary" :disabled="!selected.length||busy" @click="batch(false)">批量关闭</button></div>
  <p class="callout">客户和 API 只能选择已启用的地区。修改、停用或删除后，已受理订单仍使用下单时的地区及币种；代理资源和历史记录会保留。</p>
  <div v-if="error&&!modal" class="alert danger" role="alert">{{error}}</div>
  <div v-if="batchResult.length" class="callout"><b>以下地区未更新</b><p v-for="r in batchResult" :key="r.code">{{r.code}}：{{r.reason}}</p></div>
  <p class="muted">共 {{rows.length}} 个地区 · 当前代理模式：{{mode==='country_pool'?'国家代理池':'手动代理'}}。币种来自参考配置，可单独调整；实际扣款以付款页面为准。</p>
  <div class="panel regions-table"><table><thead><tr><th><input type="checkbox" aria-label="选择筛选结果" :checked="!!visible.length&&visible.every(r=>selected.includes(r.code))" @change="selectVisible"/></th><th>地区</th><th>国家代码</th><th>结算币种</th><th>代理 / 流程</th><th>排序</th><th>状态</th><th>操作</th></tr></thead><tbody>
    <tr v-for="row in visible" :key="row.code"><td><input type="checkbox" v-model="selected" :value="row.code" :aria-label="'选择'+row.name"/></td><td>{{row.name}}</td><td class="mono">{{row.code}}</td><td class="mono">{{row.currency||'待配置'}}</td><td><small>{{row.proxy_available?'代理支持':'代理待配置或不支持'}}</small><small>{{row.flow_supported?'当前流程支持':'当前流程不支持'}}</small></td><td>{{row.sort}}</td><td><span class="badge" :class="row.enabled?'active':'disabled'">{{row.enabled?'已启用':'已禁用'}}</span></td><td><div class="actions"><button class="text-btn" :disabled="busy" @click="edit(row)"><Pencil :size="14"/>编辑</button><button class="text-btn" :disabled="busy" @click="mutate(()=>api('/admin/regions/'+row.code,'PUT',{...row,enabled:!row.enabled}))">{{row.enabled?'禁用':'启用'}}</button><button class="text-btn danger-text" :disabled="busy" @click="form={...row};modal='delete';error=''"><Trash2 :size="14"/>删除</button></div></td></tr>
    <tr v-if="!visible.length"><td colspan="8" class="empty">{{loading?'加载中…':'暂无符合条件的地区'}}</td></tr>
  </tbody></table></div>
  <Modal v-if="modal" :title="modal==='delete'?'删除订阅地区':editing?'编辑订阅地区':'新增订阅地区'" @close="!busy&&(modal='')">
    <div v-if="error" class="alert danger" role="alert">{{error}}</div>
    <template v-if="modal==='delete'"><p>删除 {{form.name}}（{{form.code}}）后，新订单将不能选择该地区。</p><div class="form-footer"><button class="btn secondary" :disabled="busy" @click="modal=''">返回</button><button class="btn primary" :disabled="busy" @click="mutate(()=>api('/admin/regions/'+form.code,'DELETE'))">确认删除</button></div></template>
    <form v-else @submit.prevent="save"><div class="form-grid"><label>地区名称<input v-model="form.name" maxlength="60" required/></label><label>国家代码<input v-model="form.code" maxlength="2" minlength="2" pattern="[A-Za-z]{2}" :readonly="!!editing" placeholder="例如 US" required/><small>使用两位国家代码，创建后固定。</small></label><label>结算币种<select v-model="form.currency" :required="form.enabled"><option value="">待配置</option><option v-for="c in currencies" :key="c" :value="c">{{c}}</option></select></label><label>排序<input v-model.number="form.sort" type="number" min="-10000" max="10000" required/><small>越小越靠前。</small></label></div><label class="checkbox-label"><input v-model="form.enabled" type="checkbox"/>启用该地区</label><div class="form-footer"><button class="btn primary" :disabled="busy">{{busy?'保存中…':'保存地区'}}</button></div></form>
  </Modal>
</template>
<style scoped>.regions-table{overflow-x:auto}.regions-table table{width:100%;border-collapse:collapse;min-width:650px}.regions-table th,.regions-table td{padding:16px;text-align:left;border-bottom:1px solid var(--line,#e7ebe8)}.regions-table th{font-size:12px;color:var(--muted,#66756e)}.regions-table .actions{white-space:nowrap}</style>
