<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref} from 'vue'
import {ArrowUpRight,ArrowRight,Check,Copy,KeyRound,LogOut,Pencil,RefreshCw,ShieldCheck,Wallet,ReceiptText,Clock3,TrendingUp} from 'lucide-vue-next'
import {api,action,cash,date,labels,orderLabel,orderTone,renewalAttention,copy,notify,type Row} from '../lib'
import DataTable from './DataTable.vue'
import Modal from './Modal.vue'

const props=defineProps<{user:Row}>()
const emit=defineEmits<{refresh:[];logout:[];order:[Row]}>()
const summary=ref<Row>({}),membership=ref<Row|null>(null),orders=ref<Row[]>([]),loading=ref(false),loaded=ref(false),error=ref('')
const tab=ref('all'),product=ref(''),modal=ref(''),busy=ref(false),formError=ref(''),name=ref(''),oldPassword=ref(''),newPassword=ref(''),confirmPassword=ref('')
const tabs=[['all','全部记录'],['pending','待完成'],['succeeded','已完成'],['attention','需关注'],['cancelled','已撤回']]
const products=computed(()=>[...new Map(orders.value.map(o=>[o.product_id,o.product_name])).entries()])
const filtered=computed(()=>orders.value.filter(o=>(!product.value||o.product_id===product.value)&&(tab.value==='all'||(tab.value==='pending'?['draft','queued','processing','awaiting_payment'].includes(o.status):tab.value==='attention'?(['failed','review'].includes(o.status)||renewalAttention(o)):o.status===tab.value))))
const columns=[{key:'account',label:'订阅账号 / 订单'},{key:'product_name',label:'服务方案'},{key:'fee',label:'服务费报价'},{key:'status',label:'当前状态'},{key:'updated_at',label:'最近更新'},{key:'action',label:'操作'}]
const metrics=computed(()=>[
 {key:'orders',name:'订阅订单',value:summary.value.orders,unit:'笔',icon:ReceiptText,note:'全部订阅记录'},
 {key:'succeeded',name:'已完成办理',value:summary.value.succeeded,unit:'笔',icon:Check,note:'已确认完成的服务'},
 {key:'pending',name:'待完成订单',value:summary.value.pending,unit:'笔',icon:Clock3,note:'草稿、排队及办理中的方案'},
 {key:'used',name:'累计服务消费',value:cash(summary.value.used),unit:'R',icon:TrendingUp,note:'按实际服务扣费统计'}
])
let controller:AbortController|undefined,timer:ReturnType<typeof setInterval>
async function load(){
 if(loading.value)return
 controller=new AbortController();loading.value=true;error.value=''
 const timeout=setTimeout(()=>controller?.abort(),15000)
 try{
  const [overview,list,member]=await Promise.all([api('/overview','GET',undefined,{},controller.signal),api('/orders','GET',undefined,{},controller.signal),api('/membership','GET',undefined,{},controller.signal)])
  summary.value=overview.stats;orders.value=list.items;membership.value=member.membership;loaded.value=true
 }catch(e){if(!controller.signal.aborted)error.value=(e as Error).message;else error.value='账户信息加载超时，请刷新重试。'}
 finally{clearTimeout(timeout);loading.value=false}
}
function open(kind:string){name.value=props.user.name;oldPassword.value='';newPassword.value='';confirmPassword.value='';formError.value='';modal.value=kind}
function close(){if(busy.value)return;modal.value='';oldPassword.value='';newPassword.value='';confirmPassword.value='';formError.value=''}
async function save(){
 if(busy.value)return
 formError.value=''
 if(modal.value==='password'&&newPassword.value!==confirmPassword.value){formError.value='两次输入的新密码不一致。';return}
 busy.value=true
 try{
  const result=await api('/me','PATCH',modal.value==='password'?{name:props.user.name,old_password:oldPassword.value,password:newPassword.value}:{name:name.value.trim()})
  oldPassword.value='';newPassword.value='';confirmPassword.value='';modal.value=''
  if(result.relogin){notify('密码已更新，请重新登录');emit('logout')}else{notify('称呼已更新');emit('refresh')}
 }catch(e){formError.value=(e as Error).message}finally{busy.value=false}
}
onMounted(()=>{load();timer=setInterval(()=>{if(document.visibilityState==='visible'&&!modal.value)load()},15000)})
onUnmounted(()=>{clearInterval(timer);controller?.abort()})
</script>

<template>
 <div class="profile-page" :aria-busy="loading&&!loaded">
  <div class="page-heading profile-heading">
   <div><span class="eyebrow">YOUR ACCOUNT, AT A GLANCE</span><h1>个人设置</h1><p>账户与权益集中查看，最近的每一笔安排都有迹可循。</p></div>
   <div class="actions"><button class="btn secondary" @click="open('password')"><KeyRound :size="16"/>修改密码</button><button class="icon-btn bordered" @click="emit('logout')" aria-label="退出当前账号" title="退出当前账号"><LogOut :size="17"/></button></div>
  </div>
  <div v-if="error" class="alert danger">{{error}}<button @click="load">重新加载</button></div>
  <div class="profile-overview">
   <section class="profile-identity">
    <div class="profile-person"><span class="profile-avatar">{{user.name.slice(0,1)}}</span><div><h2>{{user.name}}</h2><div class="profile-email"><span>{{user.email}}</span><button @click="action(()=>copy(user.email))" aria-label="复制账户邮箱"><Copy :size="13"/></button></div></div><button class="profile-edit" @click="open('name')" aria-label="编辑称呼" title="编辑称呼"><Pencil :size="16"/></button></div>
    <div class="profile-balance"><span>账户可用金额</span><strong>{{cash(user.balance)}}<small>R</small></strong><p>冻结金额 <b>{{cash(user.frozen)}} R</b></p></div>
    <div class="profile-identity-foot"><span><ShieldCheck :size="15"/>{{user.role==='admin'?'管理员账户':'客户账户'}}</span><a href="#/wallet">管理资金<ArrowUpRight :size="16"/></a></div>
   </section>
   <div class="profile-metrics"><article v-for="m in metrics" :key="m.key" class="panel profile-metric"><div><span>{{m.name}}</span><component :is="m.icon" :size="17"/></div><strong>{{loaded?m.value:'—'}}<small>{{m.unit}}</small></strong><p>{{m.note}}</p></article></div>
  </div>
  <section v-if="membership" class="panel profile-membership">
   <div class="profile-tier"><span class="profile-tier-emblem"><ShieldCheck :size="23"/><b>{{membership.current.code}}</b></span><div><span class="eyebrow">MEMBERSHIP</span><h2>{{membership.current.name}}</h2><p>{{membership.fee_override!=null?'专属服务单价 '+cash(membership.fee_override)+' R / 次':'服务费减免 '+(100-membership.current.discount)+'%'}}</p></div></div>
   <div class="profile-growth"><div><span>会员成长</span><b>{{cash(membership.spent)}} <small>R 有效消费</small></b></div><div class="profile-growth-track" role="progressbar" aria-label="账户会员成长" :aria-valuenow="membership.progress" aria-valuemin="0" aria-valuemax="100"><i :style="{width:membership.progress+'%'}"></i></div><p v-if="membership.next">距离自动升级至 <b>{{membership.next.name}}</b>，还需 {{cash(membership.remaining)}} R</p><p v-else>已达到最高自动等级</p><small v-if="membership.source==='manual'">当前为运营指定等级 · 自动等级 {{membership.automatic.name}}{{membership.until?' · 有效至 '+date(membership.until):''}}</small></div>
   <a class="profile-member-link" href="#/membership">查看权益<ArrowRight :size="16"/></a>
  </section>
  <section class="panel profile-records">
   <div class="profile-records-heading"><div><span class="eyebrow">YOUR ACTIVITY</span><h2>订阅记录</h2><p>最近 200 笔记录，可按状态和服务方案筛选。</p></div><button class="btn secondary" @click="load" :disabled="loading"><RefreshCw :size="15" :class="{spinning:loading}"/>刷新记录</button></div>
   <div class="profile-record-filters"><div class="tabs"><button v-for="[id,label] in tabs" :key="id" :class="{active:tab===id}" @click="tab=id">{{label}}</button></div><select v-model="product" aria-label="筛选服务方案"><option value="">所有服务方案</option><option v-for="[id,label] in products" :key="id" :value="id">{{label}}</option></select></div>
   <DataTable :searchable="true" :rows="filtered" :columns="columns"><template #account="{row}"><b>{{row.email}}</b><small class="mono">{{row.id.slice(0,16)}}</small></template><template #fee="{row}">{{cash(row.fee)}} R</template><template #status="{row}"><span class="badge order-status" :class="orderTone(row)">{{orderLabel(row)}}</span></template><template #updated_at="{row}">{{date(row.updated_at)}}</template><template #action="{row}"><button class="text-btn" @click="emit('order',row)">查看进度<ArrowUpRight :size="13"/></button></template></DataTable>
  </section>
  <Modal v-if="modal" :title="modal==='name'?'编辑称呼':'修改登录密码'" @close="close"><form @submit.prevent="save"><div v-if="formError" class="alert danger" role="alert">{{formError}}</div><template v-if="modal==='name'"><label>账户邮箱<input readonly :value="user.email"/></label><label>称呼<input v-model="name" required maxlength="80" autocomplete="nickname"/></label></template><template v-else><p class="muted small profile-password-note">使用至少 10 位的新密码，更新后需要重新登录。</p><label>当前密码<input v-model="oldPassword" type="password" required autocomplete="current-password"/></label><label>新密码<input v-model="newPassword" type="password" required minlength="10" maxlength="72" autocomplete="new-password"/></label><label>确认新密码<input v-model="confirmPassword" type="password" required minlength="10" maxlength="72" autocomplete="new-password"/></label></template><button class="btn primary full" :disabled="busy">{{busy?'保存中…':'保存修改'}}<Check :size="16"/></button></form></Modal>
 </div>
</template>
