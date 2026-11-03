import {ref} from 'vue'
export type Row=Record<string,any>
export const subscriptionRegions=[
 {code:'PH',name:'菲律宾',currency:'PHP'}, {code:'US',name:'美国',currency:'USD'},
 {code:'JP',name:'日本',currency:'JPY'}, {code:'SG',name:'新加坡',currency:'SGD'},
 {code:'EG',name:'埃及',currency:'EGP'}, {code:'TR',name:'土耳其',currency:'TRY'},
 {code:'GB',name:'英国',currency:'GBP'}
]
export const regionLabel=(code:string)=>{const r=subscriptionRegions.find(r=>r.code===code);return r?`${r.name} · ${r.code}`:code}
export const toast=ref('')
let timer:ReturnType<typeof setTimeout>
export function notify(message:string){toast.value=message;clearTimeout(timer);timer=setTimeout(()=>toast.value='',5000)}
export async function api(path:string,method='GET',body?:unknown,headers:Record<string,string>={},signal?:AbortSignal){const r=await fetch('/api'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json',...headers},body:body===undefined?undefined:JSON.stringify(body),signal});const v=await r.json();if(!r.ok)throw new Error(v.error||'请求未完成');return v}
export async function action(fn:()=>Promise<void>){try{await fn()}catch(e){notify((e as Error).message)}}
export const money=(v:any)=>Number(v||0).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
export const cash=(v:any)=>money(Number(v||0)/100)
export const date=(v:any)=>v?new Date(v).toLocaleString('zh-CN',{hour12:false}):'—'
export const renewalAttention=(o:Row)=>o?.workflow==='subscription'&&o.status==='succeeded'&&['review','retrying'].includes(o.renewal)
export const orderLabel=(o:Row)=>o?.workflow==='subscription'&&o.activation==='review'?'已付款，订阅生效待核实':o?.workflow==='subscription'&&o.activation==='pending'&&o.status==='processing'&&['pending','kept'].includes(o.renewal)?'正在办理订阅':renewalAttention(o)?(o.renewal==='review'?'订阅成功，取消自动续费失败':'订阅成功，正在重新处理续费'):o?.workflow==='subscription'&&o.status==='succeeded'?'订阅成功':labels[o?.status]||o?.status
export const orderTone=(o:Row)=>o?.activation==='review'?'review':renewalAttention(o)?(o.renewal==='review'?'review':'processing'):o?.status
export const labels:Row={draft:'草稿',queued:'排队中',processing:'办理中',awaiting_payment:'链接已生成',succeeded:'已完成',failed:'未完成',cancelled:'已撤回',review:'待核实',pending:'待审核',approved:'已入账',rejected:'未通过',active:'可用',disabled:'停用',used:'已兑换',healthy:'连接正常',untested:'未测试',topup:'充值入账',redeem:'兑换入账',adjustment:'人工调整',freeze:'服务费冻结',unfreeze:'服务费解冻',charge:'服务扣费',refund:'服务退费',high:'高风险',medium:'需关注',low:'提示',member:'客户',admin:'管理员',not_started:'未开始'}
export function download(name:string,content:string,type='text/plain;charset=utf-8'){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([content],{type}));a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
export async function copy(v:string){await navigator.clipboard.writeText(v);notify('已复制')}
export function key(){return crypto.randomUUID()}
