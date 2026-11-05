<script setup lang="ts">
import {computed,onUnmounted,ref,watch} from 'vue'
import {ExternalLink,RefreshCw,LoaderCircle,Globe2} from 'lucide-vue-next'
import {api,type Row} from '../lib'
const props=defineProps<{id:string;menus:Row[]}>()
const item=ref<Row|null>(null),error=ref(''),loading=ref(false),frameLoading=ref(false),slow=ref(false),reload=ref(0)
let controller:AbortController|undefined,timer:ReturnType<typeof setTimeout>|undefined
const available=computed(()=>props.menus.find(menu=>menu.id===props.id))
const sandbox=computed(()=>{try{return 'allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox'+(new URL(item.value!.url).origin!==location.origin?' allow-same-origin':'')}catch{return ''}})
function frameStart(){clearTimeout(timer);slow.value=false;frameLoading.value=true;timer=setTimeout(()=>{slow.value=true;frameLoading.value=false},12000)}
function loaded(){clearTimeout(timer);frameLoading.value=false}
async function load(){controller?.abort();clearTimeout(timer);const request=new AbortController();controller=request;item.value=null;error.value='';loading.value=true
 try{const value=await api('/custom-pages/'+encodeURIComponent(props.id),'GET',undefined,{},request.signal);if(request!==controller)return;item.value=value;if(!value.open_new_tab)frameStart()}
 catch(e){if((e as Error).name!=='AbortError')error.value=(e as Error).message}finally{if(request===controller)loading.value=false}
}
watch([()=>props.id,()=>JSON.stringify(available.value)],()=>{void load()},{immediate:true})
onUnmounted(()=>{controller?.abort();clearTimeout(timer)})
</script>
<template>
 <section class="panel custom-page">
  <div v-if="loading" class="empty"><LoaderCircle class="spinning"/>正在打开页面…</div>
  <div v-else-if="error||!item||!available" class="empty"><Globe2/><h2>页面暂不可用</h2><p>{{error||'此菜单已移除或暂未开放。'}}</p></div>
  <template v-else><header><h2>{{item.label}}</h2><div><button v-if="!item.open_new_tab" class="icon-btn bordered" aria-label="重新加载页面" @click="reload++;frameStart()"><RefreshCw :size="17"/></button><a v-if="item.open_new_tab||!item.hide_open_button" :href="item.url" class="btn secondary" target="_blank" rel="noopener noreferrer"><ExternalLink :size="15"/>新窗口打开</a></div></header>
   <div v-if="item.open_new_tab" class="empty"><Globe2/><p>此服务在新窗口中打开。</p><a :href="item.url" class="btn primary" target="_blank" rel="noopener noreferrer">打开 {{item.label}}<ExternalLink :size="16"/></a></div>
   <template v-else><p v-if="frameLoading" class="frame-hint"><LoaderCircle class="spinning" :size="15"/>正在加载页面…</p><p v-else-if="slow" class="frame-hint">页面加载较慢，可以重试<template v-if="!item.hide_open_button">或点击“新窗口打开”</template>。</p><iframe :key="item.id+reload+item.url" :src="item.url" :title="item.label" :sandbox="sandbox" referrerpolicy="no-referrer" @load="loaded" @error="loaded();slow=true"></iframe></template>
  </template>
 </section>
</template>
<style scoped>.custom-page{padding:0;overflow:hidden;min-height:300px}.custom-page header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 22px;border-bottom:1px solid var(--line)}.custom-page h2{margin:0;font-size:18px;overflow-wrap:anywhere}.custom-page header>div{display:flex;align-items:center;gap:10px;flex-shrink:0}.custom-page iframe{display:block;width:100%;height:calc(100dvh - 230px);min-height:480px;border:0;background:white}.frame-hint{display:flex;align-items:center;gap:8px;padding:10px 22px;margin:0;font-size:12px;color:var(--muted);background:var(--soft)}@media(max-width:600px){.custom-page header{padding:14px;flex-wrap:wrap}.custom-page iframe{min-height:420px;height:calc(100dvh - 210px)}}</style>
