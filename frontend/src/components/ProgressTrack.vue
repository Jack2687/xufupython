<script setup lang="ts">
import {Check,Clock3,AlertTriangle} from 'lucide-vue-next'
import {computed} from 'vue'
const props=defineProps<{status:string;paymentState?:string;workflow?:string;renewal?:string;activation?:string}>()
const steps=computed(()=>props.workflow==='subscription'?['提交申请','排队准备','办理准备','订阅付款','订阅生效','取消自动续费','办理完成']:props.paymentState?['提交方案','排队准备','创建结账','付款处理','结果确认']:['提交方案','排队准备','创建结账','链接已生成'])
const attention=computed(()=>props.workflow==='subscription'&&(props.activation==='review'||props.renewal==='review'))
const current=()=>{
 if(props.workflow==='subscription'){
  if(props.paymentState==='succeeded'&&['pending','review'].includes(props.activation||''))return 4
  if(props.status==='succeeded')return ['disabled','kept'].includes(props.renewal||'')?7:5
  if(props.status==='cancelled')return -1
  if(props.status==='queued')return 1
  if(props.paymentState==='succeeded')return props.activation==='active'?5:4
  return props.paymentState&&props.paymentState!=='preparing'?3:2
 }
 return props.paymentState?({preparing:3,quoted:3,submitting:3,checking:4,action_required:4,review:4,succeeded:5,failed:3,cancelled:3}[props.paymentState]??3):({draft:0,queued:1,processing:2,awaiting_payment:4,succeeded:4,failed:2,review:2,cancelled:-1}[props.status]??0)
}
</script>
<template><div class="progress-track" :style="{gridTemplateColumns:`repeat(${steps.length}, minmax(0, 1fr))`}"><div v-for="(s,i) in steps" :key="s" class="progress-step" :class="{done:i<current(),active:i===current()&&!attention,attention:attention&&i===current()}"><span><AlertTriangle v-if="attention&&i===current()" :size="15"/><Check v-else-if="i<current()" :size="15"/><Clock3 v-else-if="i===current()" :size="15"/><template v-else>{{i+1}}</template></span><b>{{s}}<small v-if="workflow==='subscription'&&i===5&&renewal==='kept'">已选择保留</small><small v-else-if="attention&&i===current()">{{activation==='review'?'生效待核实':'取消待核实'}}</small></b></div></div></template>
<style scoped>.progress-step small{display:block;margin-top:4px;font-size:10px;font-weight:normal}.progress-step.attention{color:#b45309}.progress-step.attention>span{background:#fff4df;border-color:#eab75a;color:#b45309}@media(max-width:760px){.progress-step b{font-size:10px;line-height:1.6}}</style>
