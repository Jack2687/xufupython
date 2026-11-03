<script setup lang="ts">
import {Copy,Link2} from 'lucide-vue-next'
import {date,notify,type Row} from '../lib'
defineProps<{links:Row[]}>()
async function copyLink(value:string){
 try{
  if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(value)
  else{
   const field=document.createElement('textarea');field.value=value;field.style.cssText='position:fixed;opacity:0;left:0;top:0';field.setAttribute('readonly','');document.body.appendChild(field)
   try{field.select();if(!document.execCommand('copy'))throw new Error('clipboard')}finally{field.remove()}
  }
  notify('结账链接已复制')
 }catch{notify('复制未完成，请选中链接后手动复制')}
}
</script>
<template><section class="trace-links" aria-label="提链地址（结账链接）"><div class="trace-link-heading"><h4><Link2 :size="16"/>提链地址（结账链接）</h4><small>仅管理员可见</small></div><p class="trace-hint">{{links.some(link=>link.source==='current_snapshot')?'显示本单当前保存的实际链接，不代表完整的历次提链记录。':'显示已保留的实际结账链接。'}}仅供排查，已保存的链接不代表仍可付款。</p><p v-if="!links?.length" class="trace-empty">暂无保留的结账链接。尚未生成或此前已清理的链接无法显示。</p><article v-for="(link,i) in links" :key="link.round||link.source||i" class="trace-link-item"><div class="trace-link-heading"><b>{{link.round?'第 '+link.round+' 轮':'本单已保存链接'}}</b><button v-if="link.available" type="button" class="text-btn" @click="copyLink(link.url)"><Copy :size="14"/>复制链接</button></div><time>{{link.generated_at?'生成记录时间：'+date(link.generated_at):'生成时间未记录'}}</time><textarea v-if="link.available" readonly class="trace-link-url mono" :aria-label="link.round?'第 '+link.round+' 轮结账链接':'本单已保存的结账链接'" :value="link.url" rows="3" spellcheck="false"/><p v-else class="trace-empty">已保存的链接暂无法读取。</p></article></section></template>
<style scoped>.trace-links{margin-top:18px;padding:16px;background:white;border:1px solid #dce5ed;border-radius:10px}.trace-link-heading{display:flex;justify-content:space-between;align-items:center;gap:12px}.trace-link-heading h4{display:flex;align-items:center;gap:7px;font-size:13px;margin:0}.trace-link-heading small,.trace-link-item time{font-size:11px;color:#7c899c}.trace-link-item{padding:13px 0 0}.trace-link-item+.trace-link-item{margin-top:13px;border-top:1px solid #e8edf3}.trace-link-item b{font-size:12px}.trace-link-item time{display:block;margin:7px 0}.trace-link-url{display:block;box-sizing:border-box;width:100%;min-width:0;max-width:100%;font-size:11px;line-height:1.7;resize:vertical;overflow-wrap:anywhere;background:#f7f9fc;color:#53647b;border:1px solid #e3e9f0;border-radius:7px;padding:10px}.trace-link-heading button{display:flex;align-items:center;gap:5px;white-space:nowrap}
.trace-hint,.trace-empty{color:#77849a;font-size:12px;line-height:1.7}</style>
