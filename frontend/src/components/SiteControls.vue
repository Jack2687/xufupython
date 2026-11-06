<script setup lang="ts">
import {computed,onMounted,ref,watch} from 'vue'
import {Save,Mail,Send,LoaderCircle,RefreshCw,ShieldCheck,SlidersHorizontal} from 'lucide-vue-next'
import {api,date,notify,type Row} from '../lib'
const props=defineProps<{group:string;value:Row}>(),emit=defineEmits<{saved:[]}>()
const form=ref<Row>({}),domains=ref(''),busy=ref(false),error=ref(''),testEmail=ref(''),sending=ref(false),testResult=ref(''),logs=ref<Row[]>([]),logError=ref(''),logLoading=ref(false),page=ref(1),total=ref(0)
watch(()=>props.value,value=>{form.value=JSON.parse(JSON.stringify(value));domains.value=(value.email_domain_whitelist||[]).join('\n')},{immediate:true})
const sections=[
 {id:'home',title:'工作台',description:'客户首页、业务概览与快捷入口'},
 {id:'subscribe',title:'订阅办理',description:'创建站内付款订阅订单'},
 {id:'orders',title:'订单中心',description:'查询订单列表、详情及办理进度'},
 {id:'wallet',title:'资金账户',description:'余额、资金流水、充值和兑换'},
 {id:'links',title:'结账链接',description:'提交并生成结账链接'},
 {id:'renewal',title:'续费管理',description:'单独提交取消自动续费任务'},
 {id:'keys',title:'开发接入',description:'管理 API 密钥及调用用户 API；关闭后已有密钥也暂停使用'},
 {id:'invites',title:'伙伴计划',description:'查看邀请关系与邀请奖励'},
 {id:'membership',title:'会员中心',description:'查看会员成长及权益；已有等级计价仍按原规则执行'},
 {id:'docs',title:'API 文档',description:'访问接口文档和 OpenAPI 文件'}
]
const title=computed(()=>({features:'功能开关',security:'安全与认证',mail:'邮件设置'}[props.group]))
async function save(){
 busy.value=true;error.value=''
 try{const payload={...form.value};if(props.group==='security')payload.email_domain_whitelist=domains.value;if(props.group==='mail'){delete payload.ready;delete payload.password_configured}await api('/admin/settings/'+props.group,'PUT',payload);notify('配置已保存');emit('saved')}
 catch(e){error.value=(e as Error).message}finally{busy.value=false}
}
async function loadLogs(){logLoading.value=true;logError.value='';try{const result=await api('/admin/mail/logs?page='+page.value);logs.value=result.items;total.value=result.total}catch(e){logError.value=(e as Error).message}finally{logLoading.value=false}}
async function test(){sending.value=true;testResult.value='';error.value='';try{const result=await api('/admin/mail/test','POST',{email:testEmail.value});testResult.value=result.message}catch(e){error.value=(e as Error).message}finally{sending.value=false;await loadLogs()}}
onMounted(()=>{if(props.group==='mail')void loadLogs()})
</script>
<template>
 <div class="site-controls">
  <div class="control-heading"><component :is="group==='mail'?Mail:group==='security'?ShieldCheck:SlidersHorizontal" :size="23"/><div><h2>{{title}}</h2><p>{{group==='features'?'选择客户可以使用的服务，保存后立即生效。':group==='security'?'管理自主注册与邮箱身份验证。':'连接邮件服务，用于注册验证和找回密码。'}}</p></div></div>
  <div v-if="error" class="alert danger" role="alert">{{error}}</div>
  <form @submit.prevent="save">
   <template v-if="group==='features'">
    <div class="callout">关闭后菜单不显示，对应网页接口与用户 API 也会拒绝访问。管理员管理权限不受影响，已受理的任务继续完成。个人设置始终保留。</div>
    <div class="feature-grid"><label v-for="feature in sections" :key="feature.id" class="control-row"><span><b>{{feature.title}}</b><small>{{feature.description}}</small></span><input class="toggle" type="checkbox" v-model="form[feature.id]" :aria-label="feature.title"/></label></div>
    <p v-if="!form.orders&&(form.subscribe||form.links)" class="callout">订单中心已关闭：客户仍可提交已开放的服务，但无法查询订单记录和进度。</p>
   </template>
   <template v-else-if="group==='security'">
    <label class="control-row"><span><b>登录滑动验证</b><small>登录前完成滑块验证，验证结果由服务器校验且仅可使用一次。</small></span><input class="toggle" type="checkbox" v-model="form.login_slider_enabled"/></label>
    <label class="control-row"><span><b>开放注册</b><small>允许访客自行创建账户；关闭后管理员仍可创建客户账户。</small></span><input class="toggle" type="checkbox" v-model="form.registration_open"/></label>
    <label class="control-row"><span><b>邮箱验证</b><small>注册必须先验证邮箱。开启前，请保存邮件配置并发送测试邮件。</small></span><input class="toggle" type="checkbox" v-model="form.email_verify_enabled"/></label>
    <label class="domain-field">邮箱域名白名单<textarea v-model="domains" rows="6" placeholder="qq.com&#10;gmail.com&#10;*.example.com" maxlength="26000"></textarea><small>留空允许所有邮箱。支持换行、逗号或空格分隔；example.com 仅匹配该域名，*.example.com 匹配该域名及其子域名。</small></label>
    <div class="callout">白名单仅限制新注册。邮件服务启用后，已有用户可通过注册邮箱重置密码，关闭注册也不影响找回密码。</div>
   </template>
   <template v-else-if="group==='mail'">
    <label class="control-row"><span><b>启用邮件服务</b><small>开启后支持注册验证码和密码重置邮件。</small></span><input class="toggle" type="checkbox" v-model="form.enabled"/></label>
    <div class="form-grid"><label>SMTP 主机<input v-model.trim="form.host" placeholder="smtp.example.com" maxlength="254" :required="form.enabled"/></label><label>SMTP 端口<input v-model.number="form.port" type="number" min="1" max="65535" required/></label><label>连接加密<select v-model="form.encryption"><option value="ssl">SSL/TLS（常用端口 465）</option><option value="starttls">STARTTLS（常用端口 587）</option></select></label><label>SMTP 用户名<input v-model.trim="form.username" autocomplete="off" maxlength="254" placeholder="通常为完整邮箱地址"/></label><label class="span2">SMTP 授权码 / 密码<input v-model="form.password" type="password" autocomplete="new-password" maxlength="1000" :placeholder="form.password_configured?'已保存，留空保留原授权码':'输入邮件服务提供的授权码或密码'"/><small>加密保存，不回显原密码。</small></label><label>发件邮箱<input v-model.trim="form.from_email" type="email" maxlength="254" :required="form.enabled" placeholder="notice@example.com"/></label><label>发件人名称<input v-model.trim="form.from_name" maxlength="100" placeholder="留空使用站点名称"/></label></div>
    <label v-if="form.password_configured" class="checkbox-label"><input type="checkbox" v-model="form.clear_password"/>清除已保存的 SMTP 授权码</label>
   </template>
   <div class="form-footer"><button class="btn primary" :disabled="busy||sending"><LoaderCircle v-if="busy" class="spinning" :size="16"/><Save v-else :size="16"/>{{busy?'保存中…':'保存配置'}}</button></div>
  </form>
  <template v-if="group==='mail'">
   <section class="mail-test"><h3>测试发送</h3><p class="muted small">使用已保存的配置发送测试邮件，修改配置后请先保存。</p><form class="test-row" @submit.prevent="test"><label>测试收件邮箱<input v-model.trim="testEmail" type="email" required maxlength="254" placeholder="输入你的收件邮箱"/></label><button class="btn secondary" :disabled="sending||busy||!value.ready"><LoaderCircle v-if="sending" class="spinning" :size="16"/><Send v-else :size="16"/>{{sending?'发送中…':'发送测试邮件'}}</button></form><p v-if="testResult" class="callout" role="status">{{testResult}}</p></section>
   <section class="mail-log"><div class="section-heading"><div><h3>发送记录</h3><p class="muted small">保留最近 90 天记录，不记录验证码或邮件正文。“已发送”表示邮件服务器已接受发送。</p></div><button class="icon-btn bordered" aria-label="刷新发送记录" @click="loadLogs" :disabled="logLoading"><RefreshCw :size="16" :class="{spinning:logLoading}"/></button></div><p v-if="logError" class="alert danger">{{logError}}</p><div class="mail-log-scroll"><table><thead><tr><th>发送时间</th><th>收件邮箱</th><th>用途</th><th>结果</th><th>说明</th></tr></thead><tbody><tr v-for="row in logs" :key="row.id"><td>{{date(row.created_at)}}</td><td>{{row.recipient}}</td><td>{{({register:'注册验证',reset:'密码重置',test:'测试邮件'} as Row)[row.purpose]}}</td><td><span class="badge" :class="row.status==='sent'?'succeeded':row.status==='failed'?'failed':'processing'">{{({sent:'已发送',failed:'发送失败',pending:'发送中'} as Row)[row.status]}}</span></td><td>{{row.detail||'正在发送'}}</td></tr><tr v-if="!logs.length"><td colspan="5" class="muted">{{logLoading?'加载中…':'暂无发送记录'}}</td></tr></tbody></table></div><div class="log-pager"><span class="muted small">共 {{total}} 条 · 第 {{page}} 页</span><button class="btn secondary" :disabled="page<=1||logLoading" @click="page--;loadLogs()">上一页</button><button class="btn secondary" :disabled="page*20>=total||logLoading" @click="page++;loadLogs()">下一页</button></div></section>
  </template>
 </div>
</template>
<style scoped>
.control-heading{display:flex;gap:14px;align-items:center;margin-bottom:24px}.control-heading h2{margin:0 0 5px}.control-heading p{margin:0;color:var(--muted);font-size:13px}.feature-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.control-row{display:flex;flex-direction:row;align-items:center;justify-content:space-between;gap:24px;border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px}.feature-grid .control-row{margin:0}.control-row b{display:block;margin-bottom:6px}.control-row small{display:block;color:var(--muted);font-size:12px;line-height:1.7;font-weight:400}.control-row .toggle{flex-shrink:0;margin:0}.domain-field{margin-top:24px}.site-controls small{line-height:1.7}.mail-test,.mail-log{border-top:1px solid var(--line);padding-top:24px;margin-top:24px}.test-row{display:flex;align-items:end;gap:14px}.test-row label{flex:1;margin:0}.test-row button{margin-bottom:1px;white-space:nowrap}.mail-log-scroll{overflow-x:auto}.mail-log table{width:100%;min-width:650px;border-collapse:collapse;font-size:12px}.mail-log th,.mail-log td{padding:13px 10px;text-align:left;border-bottom:1px solid var(--line)}.mail-log td{overflow-wrap:anywhere;max-width:250px}.mail-log .section-heading{margin-bottom:12px}.log-pager{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-top:16px}.log-pager span{margin-right:auto}.site-controls .form-footer{margin-top:24px}@media(max-width:760px){.feature-grid{grid-template-columns:1fr}.test-row{align-items:stretch;flex-direction:column}.control-row{padding:15px;gap:12px}}
</style>
