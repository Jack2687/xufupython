<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
import {ShieldCheck,ArrowRight,RefreshCw,Check,Clock3} from 'lucide-vue-next'
import {api,date,key,type Row} from '../lib'
import DataTable from './DataTable.vue'
const session=ref(''),info=ref<Row|null>(null),error=ref(''),refreshError=ref(''),checking=ref(false),busy=ref(false),accepted=ref(false),items=ref<Row[]>([]),detail=ref<Row|null>(null)
const names:Record<string,string>={queued:'排队中',processing:'检查中',checking:'核实中',disabled:'已关闭',no_subscription:'无需关闭',review:'待核实'}
const now=ref(Date.now()),expired=computed(()=>!!info.value&&new Date(info.value.expires_at).getTime()<=now.value)
const ready=computed(()=>!!info.value&&!expired.value&&!checking.value&&!busy.value&&accepted.value)
const active=(s:string)=>['queued','processing','checking'].includes(s)
const steps=['提交申请','检查订阅','关闭核实','处理完成']
const step=computed(()=>({queued:1,processing:1,checking:2,disabled:4,no_subscription:4,review:2}[detail.value?.task.status as string]??0))
let timer:ReturnType<typeof setTimeout>,poll:ReturnType<typeof setInterval>,revision=0,detailRevision=0,requestKey=key(),disposed=false,refreshing=false
let inspectController:AbortController|undefined
const lifetime=new AbortController()
async function inspect(){
 clearTimeout(timer);const rev=++revision;inspectController?.abort();info.value=null;error.value=''
 if(!session.value.trim()){checking.value=false;return}
 inspectController=new AbortController();const controller=inspectController;checking.value=true
 const timeout=setTimeout(()=>controller.abort(),15000)
 try{const parsed=JSON.parse(session.value);const v=await api('/session/inspect','POST',{session:parsed},{},controller.signal);if(rev===revision&&!disposed)info.value=v}
 catch(e){if(rev===revision&&!disposed)error.value=e instanceof SyntaxError?'请输入完整、有效的 Session JSON':controller.signal.aborted?'检查超时，请重新检查。':(e as Error).message}
 finally{clearTimeout(timeout);if(rev===revision&&!disposed)checking.value=false}
}
watch(session,()=>{revision++;inspectController?.abort();clearTimeout(timer);info.value=null;error.value='';checking.value=false;accepted.value=false;requestKey=key();if(session.value.trim())timer=setTimeout(inspect,600)})
async function show(id:string){const rev=++detailRevision;const v=await api('/renewals/'+id,'GET',undefined,{},AbortSignal.any([lifetime.signal,AbortSignal.timeout(10000)]));if(!disposed&&rev===detailRevision){detail.value=v;refreshError.value=''}}
async function select(id:string){try{await show(id)}catch(e){if(!disposed)refreshError.value=(e as Error).message}}
async function refresh(){
 if(refreshing)return;refreshing=true
 try{const v=await api('/renewals','GET',undefined,{},AbortSignal.any([lifetime.signal,AbortSignal.timeout(10000)]));if(disposed)return;items.value=v.items;if(detail.value&&(active(detail.value.task.status)||items.value.find(x=>x.id===detail.value?.task.id)?.updated_at!==detail.value.task.updated_at))await show(detail.value.task.id);refreshError.value=''}
 catch{if(!disposed)refreshError.value='记录暂时刷新失败，请重试。'}finally{refreshing=false}
}
async function submit(){
 now.value=Date.now();if(!ready.value)return;busy.value=true;error.value=''
 try{const result=await api('/renewals','POST',{session:JSON.parse(session.value),accepted:true},{'Idempotency-Key':requestKey},AbortSignal.any([lifetime.signal,AbortSignal.timeout(20000)]));if(disposed)return;session.value='';await show(result.id);await refresh()}
 catch(e){if(!disposed)error.value=(e as Error).name==='TimeoutError'?'提交结果未确认，可再次提交查询原任务。':(e as Error).message}
 finally{if(!disposed)busy.value=false}
}
onMounted(()=>{refresh();poll=setInterval(()=>{now.value=Date.now();if(!busy.value&&document.visibilityState==='visible')refresh()},4000)})
onUnmounted(()=>{disposed=true;revision++;detailRevision++;clearTimeout(timer);clearInterval(poll);inspectController?.abort();lifetime.abort();session.value='';info.value=null})
</script>

<template>
<div class="renewal-layout">
 <form class="panel renewal-form" autocomplete="off" @submit.prevent="submit">
  <div class="renewal-heading"><span class="renewal-icon"><ShieldCheck :size="24"/></span><div><h2>单独关闭自动续费</h2><p>已有订阅也能操作，无需在序付下单。</p></div><span class="free-label">免费操作</span></div>
  <label for="renewal-session">账号 Session<textarea id="renewal-session" v-model="session" rows="6" maxlength="262144" required autocomplete="off" spellcheck="false" :disabled="busy" placeholder="粘贴完整 Session JSON，自动识别需要关闭续费的账号。"></textarea></label>
  <p class="muted small">授权仅用于本次处理，加密暂存，结束或到期后清除。</p>
  <p v-if="checking" class="muted" role="status">正在识别账号…</p>
  <div v-if="info&&!expired" class="session-result"><ShieldCheck :size="20"/><div><b>{{info.email}}</b><p>到期 {{date(info.expires_at)}}</p><small>格式检查通过，提交后核验在线状态</small></div></div>
  <p v-if="expired" class="alert danger" role="alert">Session 已过期，请重新获取。</p>
  <div v-if="error" class="alert danger" role="alert"><span>{{error}}</span><button v-if="!busy&&session" type="button" class="text-btn" @click="inspect">重新检查</button></div>
  <label class="checkbox-label renewal-consent"><input v-model="accepted" type="checkbox" :disabled="busy" required/>我确认关闭此账号的自动续费，保留当前已付费有效期。</label>
  <button class="btn primary" :disabled="!ready">{{busy?'正在提交…':'关闭自动续费'}}<ArrowRight :size="17"/></button>
 </form>
 <aside class="panel renewal-guide"><span class="eyebrow">RENEWAL CONTROL</span><h2>只管理下一次续费</h2><p>关闭后不再自动续订，当前订阅可继续使用至到期。本操作不申请退款。</p><ul><li><Check :size="16"/>无需填写银行卡或账单资料</li><li><Check :size="16"/>后台处理，离开页面也会继续</li><li><Check :size="16"/>确认最终续费状态后更新结果</li></ul><div class="callout">订阅成功但取消续费失败，也可在此粘贴同一账号对应空间的新 Session 继续处理，不会再次付款。独立任务待核实时重新提交授权可继续核实。</div></aside>
</div>
<section v-if="detail" class="panel renewal-progress" aria-live="polite">
 <div class="renewal-detail-heading"><div><h2>{{detail.task.email}}</h2><small>{{date(detail.task.created_at)}}</small></div><span class="badge" :class="{succeeded:detail.task.status==='disabled',review:detail.task.status==='review'}">{{names[detail.task.status]}}</span></div>
 <div class="progress-track renewal-track"><div v-for="(s,i) in steps" :key="s" class="progress-step" :class="{done:i<step,active:i===step}"><span><Check v-if="i<step" :size="15"/><Clock3 v-else-if="i===step" :size="15"/><template v-else>{{i+1}}</template></span><b>{{s}}</b></div></div>
 <p class="callout">{{detail.task.message}}</p>
 <details><summary>查看处理记录</summary><div class="timeline"><div v-for="(e,i) in detail.events" :key="i"><span class="timeline-dot"></span><small>{{date(e.created_at)}}</small><p>{{e.message}}</p></div></div></details>
</section>
<section class="panel renewal-history"><div class="renewal-detail-heading"><h2>独立处理记录</h2><button class="text-btn" @click="refresh"><RefreshCw :size="14"/>刷新</button></div><p v-if="refreshError" class="alert danger" role="alert">{{refreshError}}</p><DataTable :rows="items" :columns="[{key:'email',label:'订阅账号'},{key:'status',label:'处理状态'},{key:'created_at',label:'提交时间'},{key:'action',label:'操作'}]"><template #status="{row}"><span class="badge" :class="{succeeded:row.status==='disabled',review:row.status==='review'}">{{names[row.status]}}</span></template><template #created_at="{row}">{{date(row.created_at)}}</template><template #action="{row}"><button class="text-btn" @click="select(row.id)">查看进度</button></template></DataTable></section>
</template>

<style scoped>
.renewal-layout{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(280px,1fr);gap:22px;margin-bottom:22px;align-items:start}.renewal-form,.renewal-guide,.renewal-progress{padding:26px}.renewal-heading{display:flex;gap:12px;align-items:center;margin-bottom:24px}.renewal-heading h2,.renewal-guide h2,.renewal-detail-heading h2{font-size:18px;margin:0}.renewal-heading p{font-size:12px;color:var(--muted);margin:6px 0 0}.renewal-icon{padding:10px;border-radius:12px;color:#238169;background:#edf7f2;display:flex}.free-label{margin-left:auto;white-space:nowrap;color:#238169;font-size:11px}.renewal-form textarea{font-family:Consolas,monospace;font-size:12px;margin-top:9px;resize:vertical}.renewal-consent{margin:22px 0 16px;font-size:12px}.renewal-guide h2{margin:16px 0}.renewal-guide p,.renewal-guide li{font-size:13px;color:var(--muted);line-height:1.9}.renewal-guide ul{list-style:none;padding:0;margin:22px 0}.renewal-guide li{display:flex;align-items:center;gap:8px;margin:9px 0}.renewal-guide li svg{color:#238169}.renewal-guide .callout{font-size:12px}.renewal-detail-heading{display:flex;justify-content:space-between;align-items:center;gap:12px}.renewal-detail-heading h2{overflow-wrap:anywhere}.renewal-detail-heading small{color:var(--muted);display:block;margin-top:6px}.renewal-track{grid-template-columns:repeat(4,minmax(0,1fr))}.renewal-progress,.renewal-history{margin-bottom:22px}.renewal-progress summary{font-size:12px;color:var(--muted);cursor:pointer}.renewal-history>.renewal-detail-heading{padding:20px 24px}.renewal-history>.alert{margin:0 24px 16px}.renewal-history .text-btn{display:inline-flex;gap:6px;align-items:center}
@media(max-width:900px){.renewal-layout{grid-template-columns:1fr}.renewal-guide{display:none}.renewal-form,.renewal-progress{padding:20px}.renewal-heading{flex-wrap:wrap}.renewal-heading h2{font-size:16px}.free-label{margin-left:0}.renewal-detail-heading h2{font-size:15px}}
</style>
