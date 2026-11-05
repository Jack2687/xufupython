<script setup lang="ts">
import {computed,onUnmounted,ref,watch} from 'vue'
import {ArrowRight,ArrowLeft,ShieldCheck,LoaderCircle} from 'lucide-vue-next'
import {api,type Row} from '../lib'
const props=defineProps<{pub:Row}>()
const emit=defineEmits<{authenticated:[]}>()
const mode=ref<'login'|'register'|'reset'>('login'),email=ref(''),password=ref(''),confirm=ref(''),name=ref(''),code=ref(''),invite=ref(new URLSearchParams(location.search).get('invite')||'')
const busy=ref(false),sending=ref(false),error=ref(''),message=ref(''),seconds=ref(0)
const policy=computed(()=>props.pub.security||{registration_open:props.pub.site.registration_open})
const needsCode=computed(()=>mode.value==='reset'||mode.value==='register'&&policy.value.email_verify_enabled)
let timer:ReturnType<typeof setInterval>|undefined
function countdown(value:number){clearInterval(timer);seconds.value=value;timer=setInterval(()=>{seconds.value=Math.max(0,seconds.value-1);if(!seconds.value)clearInterval(timer)},1000)}
onUnmounted(()=>clearInterval(timer))
watch(mode,()=>{password.value='';confirm.value='';code.value='';error.value='';message.value=''})
watch(email,()=>{code.value='';message.value=''})
watch(policy,p=>{if(mode.value==='register'&&!p.registration_open)mode.value='login'})
async function send(){
 if(busy.value||sending.value||seconds.value)return
 sending.value=true;error.value='';message.value=''
 try{const result=await api('/auth/email-code','POST',{email:email.value,purpose:mode.value==='reset'?'reset':'register'});countdown(result.retry_after);message.value=result.message}
 catch(e){error.value=(e as Error).message}finally{sending.value=false}
}
async function submit(){
 if(busy.value||sending.value)return
 error.value='';message.value=''
 const bytes=(value:string)=>new TextEncoder().encode(value).length
 if(mode.value!=='login'&&(bytes(password.value)<10||bytes(password.value)>72)){error.value='密码须为 10～72 字节，中文和表情占多个字节';return}
 if(mode.value==='register'&&(bytes(name.value.trim())<1||bytes(name.value.trim())>80)){error.value='用户名须为 1～80 字节';return}
 if(mode.value==='reset'&&password.value!==confirm.value){error.value='两次输入的密码不一致';return}
 busy.value=true
 try{
  const path=mode.value==='reset'?'reset-password':mode.value
  const result=await api('/auth/'+path,'POST',{email:email.value,password:password.value,...(mode.value==='register'?{name:name.value,invite:invite.value,code:code.value}:mode.value==='reset'?{code:code.value}:{})})
  password.value='';code.value=''
  if(mode.value==='reset'){mode.value='login';queueMicrotask(()=>{message.value=result.message})}else emit('authenticated')
 }catch(e){error.value=(e as Error).message}finally{busy.value=false}
}
</script>
<template>
 <form class="login-form" @submit.prevent="submit">
  <div class="eyebrow">WELCOME TO {{pub.site.english_name}}</div>
  <h2>{{mode==='register'?'建立你的专属空间':mode==='reset'?'找回你的账户':'欢迎回来'}}</h2>
  <p class="muted">{{mode==='register'?'用一个账户，管理每一次订阅。':mode==='reset'?'验证注册邮箱，设置新的登录密码。':'登录后继续管理你的服务与资金。'}}</p>
  <div v-if="mode!=='reset'" class="segmented"><button type="button" :class="{active:mode==='login'}" :disabled="busy||sending" @click="mode='login'">登录账户</button><button v-if="policy.registration_open" type="button" :class="{active:mode==='register'}" :disabled="busy||sending" @click="mode='register'">注册账户</button></div>
  <button v-else type="button" class="text-btn auth-back" :disabled="busy||sending" @click="mode='login'"><ArrowLeft :size="15"/>返回登录</button>
  <div v-if="error" class="alert danger" role="alert">{{error}}</div><div v-if="message" class="callout" role="status">{{message}}</div>
  <label v-if="mode==='register'">用户名<input v-model="name" required maxlength="80" autocomplete="username" placeholder="请输入用户名"/></label>
  <label>{{mode==='login'?'登录账号':'邮箱地址'}}<input v-model="email" :type="mode==='login'?'text':'email'" required maxlength="254" :placeholder="mode==='login'?'用户名或邮箱':'name@example.com'" :autocomplete="mode==='login'?'username':'email'" :disabled="busy||sending"/></label>
  <p v-if="mode==='register'&&policy.email_domain_whitelist?.length" class="muted small">支持的邮箱域名：{{policy.email_domain_whitelist.join('、')}}</p>
  <label v-if="needsCode">邮箱验证码<div class="auth-code"><input v-model="code" required inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" autocomplete="one-time-code" placeholder="6 位验证码"/><button class="btn secondary" type="button" @click="send" :disabled="sending||seconds>0||!email||busy"><LoaderCircle v-if="sending" :size="15" class="spinning"/>{{seconds>0?seconds+' 秒后重发':sending?'发送中…':'获取验证码'}}</button></div><small>验证码 15 分钟有效，请勿分享给他人。</small></label>
  <label>{{mode==='reset'?'新密码':'账户密码'}}<input v-model="password" type="password" required :minlength="1" maxlength="72" :placeholder="mode==='login'?'输入你的密码':'10～72 字节密码'" :autocomplete="mode==='login'?'current-password':'new-password'"/></label>
  <label v-if="mode==='reset'">确认新密码<input v-model="confirm" type="password" required minlength="1" maxlength="72" autocomplete="new-password" placeholder="再输入一次新密码"/></label>
  <label v-if="mode==='register'&&pub.features?.invites!==false">邀请标识（选填）<input v-model="invite" maxlength="80" placeholder="来自伙伴的邀请标识"/></label>
  <div v-if="mode==='login'&&policy.password_reset_available" class="auth-forgot"><button type="button" class="text-btn" :disabled="busy||sending" @click="mode='reset'">忘记密码？</button></div>
  <button class="btn primary full" :disabled="busy||sending"><LoaderCircle v-if="busy" class="spinning" :size="18"/>{{busy?'请稍候…':mode==='register'?(needsCode?'验证并注册账户':'注册账户'):mode==='reset'?'重置密码':'进入工作台'}}<ArrowRight v-if="!busy" :size="18"/></button>
  <div class="login-hint"><ShieldCheck :size="15"/>{{mode==='reset'?'重置后原登录会话和 API 密钥将失效':'仅在你自己的账户空间查看业务资料'}}</div>
 </form>
</template>
<style scoped>
.auth-code{display:flex;gap:10px}.auth-code input{min-width:0;flex:1}.auth-code button{white-space:nowrap;padding:10px 14px}.auth-forgot{display:flex;justify-content:flex-end;margin:-3px 0 18px}.auth-back{margin:0 0 22px}.alert,.callout{margin-bottom:20px}.small{overflow-wrap:anywhere}.login-form{padding-top:25px;padding-bottom:25px}
</style>
