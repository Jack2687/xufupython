import {cash,type Row} from './lib'

export const feeStates:Row={held:'服务费已冻结',charged:'服务费已结算',released:'服务费已解冻'}
export const paymentStates:Row={not_started:'尚未付款',preparing:'付款准备中',quoted:'金额已核实',submitting:'正在付款',checking:'核实付款中',action_required:'需要额外验证',review:'付款待核实',succeeded:'付款成功',failed:'付款未完成',cancelled:'付款准备已结束',link:'仅提取链接'}
export const renewalStates:Row={not_started:'尚未开始',pending:'待处理',kept:'已选择保留',disabled:'已关闭自动续费',review:'取消续费待处理',retrying:'正在重新处理',not_applicable:'不涉及续费'}
export const paymentState=(r:Row)=>r.workflow==='link'?'link':r.payment?.state||'not_started'
export const renewalState=(r:Row)=>r.workflow==='subscription'?r.renewal||'not_started':'not_applicable'
export const signed=(v:unknown)=>v==null?'未记录':`${Number(v)>0?'+':Number(v)<0?'−':''}${cash(Math.abs(Number(v)))}`
export const deltaTone=(v:unknown)=>v==null||Number(v)===0?'muted':Number(v)>0?'positive':'negative'
export function upstreamAmount(p:Row={}){
 const actual=p.charged_amount!=null&&!!p.charged_currency
 const amount=actual?p.charged_amount:p.amount,currency=String(actual?p.charged_currency:p.currency||'').toUpperCase()
 if(amount==null||!currency)return {value:'—',label:'金额尚未记录',actual:false}
 const digits=currency==='JPY'?0:2
 return {value:`${(Number(amount)/(digits?100:1)).toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits})} ${currency}`,label:actual?'已核实实收':'含税报价',actual}
}
export function discountText(o:Row){
 const s=o.price_snapshot||{}
 if(s.standard==null)return '历史订单未记录优惠'
 const saving=Math.max(0,Number(s.standard)-Number(o.fee))
 return saving>0?`已减 ${cash(saving)} R${s.source==='override'?' · 专属定价':s.tier_name?` · ${s.tier_name}`:''}`:'标准服务费'
}
