<script setup lang="ts">
import {ref,onMounted} from 'vue'
import {Copy,Gift,RefreshCw} from 'lucide-vue-next'
import {api,action,copy,cash,date,type Row} from '../lib'
import DataTable from './DataTable.vue'
const props=defineProps<{user:Row}>()
const data=ref<Row>({items:[],stats:{},rules:{}}),page=ref(1),busy=ref(false),error=ref('')
const inviteURL=location.origin+'/?invite='+encodeURIComponent(props.user.invite_code)
const states:Row={waiting:'等待符合条件的消费',rewarded:'已结算',adjusted:'部分退回',reversed:'已撤回',recovering:'退款奖励待扣回'}
async function load(){busy.value=true;error.value='';try{data.value=await api('/invites?page='+page.value)}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
onMounted(load)
</script>
<template>
 <div class="page-heading"><div><span class="eyebrow">GROW TOGETHER</span><h1>伙伴计划</h1><p>分享订阅体验，获得可用于平台服务费的积分。</p></div><button class="btn secondary" :disabled="busy" @click="load"><RefreshCw :size="16" :class="{spinning:busy}"/>刷新</button></div>
 <div v-if="error" class="alert danger">{{error}}<button @click="load">重试</button></div>
 <section class="invite-banner"><Gift :size="40"/><div><h2>{{data.rules.enabled?'邀请伙伴，阶段内持续奖励':'伙伴奖励暂未开放'}}</h2><p v-for="(stage,i) in data.rules.stages" :key="i">注册满 {{Number(i)===0?0:data.rules.stages[Number(i)-1].until_days}} 天至未满 {{stage.until_days}} 天：返 {{stage.rate_percent}}%。</p><p v-if="data.rules.stages?.length">满 {{data.rules.stages[2].until_days}} 天后不再返积分。</p><div class="copy-field"><input readonly :value="inviteURL" aria-label="邀请链接"/><button class="btn primary" @click="action(()=>copy(inviteURL))"><Copy :size="16"/>复制邀请链接</button></div></div></section>
 <div class="stats-grid four"><article class="stat" v-for="s in [{k:'invited',n:'已邀请伙伴',u:'位'},{k:'qualified',n:'获奖伙伴',u:'位'},{k:'earned',n:'有效奖励',u:'积分',money:true},{k:'recovered',n:'已退回奖励',u:'积分',money:true}]" :key="s.k"><div class="stat-label">{{s.n}}</div><strong>{{s.money?cash(data.stats[s.k]):data.stats[s.k]||0}}<small>{{s.u}}</small></strong></article></div>
 <p v-if="data.stats.pending_recovery" class="alert">原服务费已退款，还有 {{cash(data.stats.pending_recovery)}} 积分奖励待扣回；可用余额充足后自动处理，冻结服务费不受影响。</p>
 <section class="panel partner-rules"><h2>奖励规则</h2><p>通过邀请链接注册即绑定关系，奖励从伙伴注册时间开始计算，阶段内每笔符合条件的消费均参与，只有直接邀请享有奖励，不设多级返利。</p><p>仅活动开启后新建的订阅订单参与，历史订单不补发，已发放奖励保留。下单时锁定比例；订阅已生效且服务费已结算后，通常一分钟内入账。未成功或仅生成链接不奖励。</p><p>奖励按实际服务费计算，保留两位小数并向下取整，不含银行卡订阅费用。服务费退款后按比例撤回，余额不足时保留待扣回金额；积分可用于平台服务费，不支持提现。</p></section>
 <div class="section-heading"><h2>邀请与奖励明细</h2><span class="pill">{{data.total||0}} 位伙伴</span></div>
 <div class="panel"><DataTable :rows="data.items" :searchable="false" hide-pagination :page-size="25" :columns="[{key:'email',label:'伙伴邮箱'},{key:'created_at',label:'加入时间'},{key:'reward_state',label:'奖励状态'},{key:'entitled',label:'有效奖励'},{key:'rewarded_at',label:'结算时间'}]"><template #created_at="{row}">{{date(row.created_at)}}</template><template #reward_state="{row}"><span class="badge">{{states[row.reward_state]}}</span></template><template #entitled="{row}">{{row.entitled==null?'—':cash(row.entitled)+' 积分'}}</template><template #rewarded_at="{row}">{{date(row.rewarded_at)}}</template></DataTable></div>
 <div class="pagination"><span>每页 25 位</span><div><button class="btn secondary" :disabled="page===1||busy" @click="page--;load()">上一页</button><span>{{page}}</span><button class="btn secondary" :disabled="!data.has_more||busy" @click="page++;load()">下一页</button></div></div>
</template>
<style scoped>.partner-rules{padding:22px;margin:22px 0}.partner-rules h2{font-size:15px}.partner-rules p{font-size:13px;line-height:1.9;color:var(--muted,#73828f)}.invite-banner>div{min-width:0;flex:1}.copy-field input{min-width:0}@media(max-width:600px){.invite-banner{padding:22px}.invite-banner>svg{display:none}.copy-field{flex-wrap:wrap}.copy-field input{width:100%}.copy-field button{width:100%;justify-content:center}}</style>
