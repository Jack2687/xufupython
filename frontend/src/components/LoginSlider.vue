<script setup lang="ts">
import {ref,onMounted,onBeforeUnmount} from 'vue'
import {api} from '../lib'
const emit=defineEmits<{verified:[proof:string]}>()
const challenge=ref<any>(null),x=ref(0),busy=ref(false),passed=ref(false),error=ref('')
let timer:ReturnType<typeof setTimeout>|undefined
let generation=0
async function reset(){
 const current=++generation;clearTimeout(timer);emit('verified','');passed.value=false;x.value=0;error.value='';busy.value=true
 try{const data=await api('/auth/slider');if(current!==generation)return;challenge.value=data}
 catch(e){if(current===generation)error.value=(e as Error).message}finally{if(current===generation)busy.value=false}
}
async function verify(){
 if(busy.value||passed.value||!challenge.value?.id)return
 busy.value=true;error.value=''
 try{const data=await api('/auth/slider/verify','POST',{id:challenge.value.id,x:x.value});passed.value=true;emit('verified',data.proof);timer=setTimeout(()=>{void reset()},data.expires_in*1000)}
 catch(e){challenge.value=null;emit('verified','');error.value=(e as Error).message}finally{busy.value=false}
}
defineExpose({reset})
onMounted(reset);onBeforeUnmount(()=>{generation++;clearTimeout(timer)})
</script>
<template>
 <section class="login-slider" aria-label="登录滑动验证">
  <div class="slider-title"><span>{{passed?'验证通过':busy?'正在验证…':'拖动滑块，对齐图中的深色位置'}}</span><button type="button" :disabled="busy" @click="reset">换一张</button></div>
  <template v-if="challenge?.enabled&&!passed">
   <div class="slider-canvas"><img :src="challenge.image" alt="将滑块移至图中的深色位置"/><span :style="{left:(x/320*100)+'%'}"/></div>
   <input v-model.number="x" type="range" min="0" max="284" step="1" aria-label="移动验证滑块" :disabled="busy" @change="verify"/>
  </template>
  <p v-if="passed" class="slider-success" role="status">✓ 滑动验证完成</p>
  <p v-if="challenge?.enabled===false" role="status">当前无需滑动验证</p>
  <p v-if="error" role="alert">{{error}}，请点击“换一张”。</p>
 </section>
</template>
<style scoped>
.login-slider{margin:16px 0;padding:12px;border:1px solid #d9e6e0;border-radius:12px;background:#f8fbfa;color:#315f50;font-size:12px}.slider-title{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:10px}.slider-title button{background:none;border:0;color:#297258;cursor:pointer;white-space:nowrap}.slider-canvas{position:relative;width:100%;max-width:320px;margin:auto}.slider-canvas img{display:block;width:100%;height:auto}.slider-canvas span{position:absolute;top:27.7778%;width:11.25%;height:44.4444%;border-radius:7px;background:#e0b04f;border:2px solid #fff;box-sizing:border-box;box-shadow:0 1px 4px #5676}.login-slider input[type=range]{display:block;width:100%;max-width:320px;margin:12px auto 0;padding:0;accent-color:#297258;cursor:grab;height:28px}.slider-success{font-weight:600}.login-slider p{margin:5px 0;line-height:1.6}
</style>
