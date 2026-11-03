<script setup lang="ts">
import {computed,ref} from 'vue'
import {Plus,RefreshCw,MapPin,Shuffle} from 'lucide-vue-next'
import DataTable from './DataTable.vue'
import Modal from './Modal.vue'
import {api,notify,type Row} from '../lib'

const props=defineProps<{value:Row}>()
const rows=ref<Row[]>(JSON.parse(JSON.stringify(props.value.address_templates||[])))
const busy=ref(false),error=ref(''),filter=ref('all'),modal=ref(''),original=ref(''),form=ref<Row>({}),formError=ref('')
const states='AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split(' ')
const enabled=(row:Row)=>row.enabled!==false
const liveCount=computed(()=>rows.value.filter(r=>enabled(r)).length)
const paymentCount=computed(()=>rows.value.filter(r=>r.enabled===true&&r.test_only===false).length)
const profileKey=(row:Row)=>row.id||row.email
const sample=ref<Row|null>(null)
const filtered=computed(()=>rows.value.filter(r=>filter.value==='all'||(filter.value==='enabled'&&enabled(r))||(filter.value==='disabled'&&!enabled(r))||(filter.value==='test'&&r.test_only)||(filter.value==='live'&&!r.test_only)))
const columns=[{key:'account',label:'资料名称'},{key:'holder',label:'持卡人'},{key:'address',label:'US 账单地址'},{key:'status',label:'状态 / 用途'},{key:'action',label:'操作'}]
function edit(row?:Row){original.value=row?profileKey(row):'';form.value=row?{...row,enabled:enabled(row),test_only:!!row.test_only}:{name:'',first_name:'',last_name:'',state:'',city:'',line1:'',postal_code:'',enabled:true,test_only:true};formError.value='';modal.value='edit'}
function close(){if(!busy.value){modal.value='';form.value={};formError.value=''}}
async function refresh(){if(busy.value)return;busy.value=true;error.value='';try{rows.value=(await api('/admin/billing-profiles')).items}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function change(method:string,endpoint:string,payload?:Row){if(busy.value)return;busy.value=true;error.value='';formError.value='';try{rows.value=(await api('/admin/billing-profiles'+endpoint,method,payload)).items;modal.value='';form.value={};notify(method==='DELETE'?'账单资料已删除':'账单资料已保存')}catch(e){if(modal.value)formError.value=(e as Error).message;else error.value=(e as Error).message}finally{busy.value=false}}
function save(){const payload={...form.value};for(const key of ['name','email','first_name','last_name','state','city','line1','postal_code'])if(typeof payload[key]==='string')payload[key]=payload[key].trim();if(payload.email)payload.email=payload.email.toLowerCase();change(original.value?'PUT':'POST',original.value?'/'+encodeURIComponent(original.value):'',payload)}
function toggle(row:Row){const payload={...row};payload.enabled=!enabled(row);payload.test_only=!!row.test_only;change('PUT','/'+encodeURIComponent(profileKey(row)),payload)}
async function drawSample(){if(busy.value)return;busy.value=true;error.value='';try{sample.value=(await api('/admin/billing-profiles/sample','POST',{})).profile;modal.value='sample'}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function remove(row:Row){form.value={...row};formError.value='';modal.value='delete'}
</script>

<template>
  <section class="billing-manager">
    <div class="billing-heading">
      <div><span class="eyebrow">BILLING PROFILES</span><h2>US 账单资料</h2><p>共 {{rows.length}} 条 · 已启用 {{liveCount}} 条 · 可用于付款 {{paymentCount}} 条</p></div>
      <div class="actions"><button class="btn secondary" :disabled="busy" @click="drawSample"><Shuffle :size="16"/>随机测试</button><button class="btn secondary" :disabled="busy" @click="refresh"><RefreshCw :size="16"/>刷新</button><button class="btn primary" :disabled="busy||rows.length>=100" @click="edit()"><Plus :size="16"/>新增账单资料</button></div>
    </div>
    <div class="billing-note"><MapPin :size="18"/><p>订阅付款会从已启用的正式资料中随机选择一条，无需绑定订阅邮箱；同一订单固定使用选中的资料。停用及仅供测试的资料不参与付款，随机测试按钮仅预览测试资料。</p></div>
    <div v-if="error" class="alert danger" role="alert">{{error}}</div>
    <div class="billing-filters"><label>筛选资料<select v-model="filter" aria-label="筛选资料"><option value="all">全部资料</option><option value="enabled">已启用</option><option value="disabled">已停用</option><option value="live">正式资料</option><option value="test">仅供测试</option></select></label><small>可按名称、姓名、城市、州或地址搜索，最多保存 100 条。</small></div>
    <DataTable :rows="filtered" :columns="columns" :searchable="true">
      <template #account="{row}"><b>{{row.name||row.first_name+' '+row.last_name}}</b><small>{{profileKey(row)}}</small></template>
      <template #holder="{row}">{{row.first_name}} {{row.last_name}}</template>
      <template #address="{row}"><span>{{row.line1}}</span><small>{{row.city}} · {{row.state}} {{row.postal_code}} · US</small></template>
      <template #status="{row}"><span class="badge" :class="enabled(row)?'active':'disabled'">{{enabled(row)?'已启用':'已停用'}}</span><small :class="{'test-label':row.test_only}">{{row.test_only?'仅供测试':'正式资料'}}</small></template>
      <template #action="{row}"><div class="actions"><button class="text-btn" :disabled="busy" @click="edit(row)">编辑</button><button class="text-btn" :disabled="busy" @click="toggle(row)">{{enabled(row)?'停用':'启用'}}</button><button class="text-btn danger-text" :disabled="busy" @click="remove(row)">删除</button></div></template>
    </DataTable>
    <Modal v-if="modal==='edit'" :title="original?'编辑账单资料':'新增账单资料'" wide @close="close">
      <form @submit.prevent="save" autocomplete="off">
        <div v-if="formError" class="alert danger" role="alert">{{formError}}</div>
        <div class="form-grid">
          <label class="span2">资料名称（选填）<input v-model="form.name" maxlength="200" placeholder="便于管理的备注名称"/></label>
          <label>持卡人名字<input v-model="form.first_name" required maxlength="200"/></label>
          <label>持卡人姓氏<input v-model="form.last_name" required maxlength="200"/></label>
          <label>州<select v-model="form.state" aria-label="州" required><option value="" disabled>选择州 / DC</option><option v-for="state in states" :key="state" :value="state">{{state}}</option></select></label>
          <label>城市<input v-model="form.city" required maxlength="200"/></label>
          <label class="span2">街道及门牌地址<input v-model="form.line1" required maxlength="200"/></label>
          <label>邮政编码<input v-model="form.postal_code" required pattern="[0-9]{5}(-[0-9]{4})?" maxlength="10" placeholder="5 位邮编或 ZIP+4"/></label>
          <label>国家<input value="US · 美国" readonly/></label>
        </div>
        <div class="billing-switches"><label><input v-model="form.enabled" type="checkbox"/>启用此资料</label><label><input v-model="form.test_only" type="checkbox"/>仅供测试，不用于真实付款</label></div>
        <p class="muted small">取消勾选“仅供测试”并启用后，此资料会加入正式付款随机池。请保存本人或已获授权使用的真实账单资料。</p>
        <div class="form-footer"><button type="button" class="btn secondary" :disabled="busy" @click="close">取消</button><button class="btn primary" :disabled="busy">{{busy?'保存中…':'保存账单资料'}}</button></div>
      </form>
    </Modal>
    <Modal v-if="modal==='delete'" title="删除账单资料" @close="close"><div v-if="formError" class="alert danger" role="alert">{{formError}}</div><p>确定删除 <b>{{form.name||form.first_name+' '+form.last_name}}</b>？删除后无法再选用此资料，已开始的任务保留原资料。</p><div class="form-footer"><button class="btn secondary" :disabled="busy" @click="close">取消</button><button class="btn danger" :disabled="busy" @click="change('DELETE','/'+encodeURIComponent(profileKey(form)))">确认删除</button></div></Modal>
    <Modal v-if="modal==='sample'&&sample" title="随机测试结果" @close="close"><p class="callout">仅预览测试资料，未创建订单或发起付款。</p><h3>{{sample.name||sample.first_name+' '+sample.last_name}}</h3><p>{{sample.first_name}} {{sample.last_name}}</p><p>{{sample.line1}}</p><p>{{sample.city}} · {{sample.state}} {{sample.postal_code}} · US</p><div class="form-footer"><button class="btn secondary" :disabled="busy" @click="drawSample">再抽取一次</button><button class="btn primary" @click="close">关闭</button></div></Modal>
  </section>
</template>

<style scoped>
.billing-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px}.billing-heading .actions{flex-wrap:wrap}.billing-heading h2{margin:7px 0}.billing-heading p{font-size:12px;color:var(--muted)}.billing-note{display:flex;align-items:flex-start;gap:10px;padding:15px 18px;background:#edf5f2;color:#437460;border:1px solid #d8e9e0;border-radius:10px;margin-bottom:22px}.billing-note svg{flex-shrink:0;margin-top:2px}.billing-note p{font-size:12px}.billing-filters{display:flex;align-items:center;gap:18px;margin-bottom:10px}.billing-filters label{min-width:160px;margin-bottom:0}.billing-filters small{color:var(--muted);line-height:1.6}.billing-switches{display:flex;gap:24px;flex-wrap:wrap}.billing-switches label{flex-direction:row;align-items:center;font-weight:400}.test-label{color:#a67521}.billing-manager :deep(.table-scroll){max-width:100%;overflow-x:auto}.billing-manager :deep(table){min-width:780px}
@media(max-width:760px){.billing-heading{align-items:flex-start;flex-direction:column}.billing-heading .actions{width:100%}.billing-filters{align-items:flex-start;flex-direction:column;gap:8px}.billing-filters label{width:100%}}
</style>
