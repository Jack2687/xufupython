<script setup lang="ts">
import {ref,computed,onMounted,onUnmounted,watch} from 'vue'
import {Search,ChevronDown,Check} from 'lucide-vue-next'
import type {Row} from '../lib'
const props=defineProps<{modelValue:string;countries:Row[]}>()
const emit=defineEmits<{'update:modelValue':[value:string]}>()
const open=ref(false),query=ref(''),root=ref<HTMLElement|null>(null),index=ref(0)
const selected=computed(()=>props.countries.find(c=>c.code===props.modelValue))
const options=computed(()=>props.countries.filter(c=>[c.name,c.code,c.currency].some(v=>String(v).toLowerCase().includes(query.value.trim().toLowerCase()))))
const label=(r:Row)=>`${r.name}（${r.code}）· ${r.currency}`
function choose(row:Row){emit('update:modelValue',row.code);open.value=false;query.value=''}
function outside(event:MouseEvent){if(!root.value?.contains(event.target as Node))open.value=false}
function keydown(event:KeyboardEvent){if(event.key==='Escape'){open.value=false;return}if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();open.value=true;index.value=Math.max(0,Math.min(options.value.length-1,index.value+(event.key==='ArrowDown'?1:-1)))}else if(event.key==='Enter'&&open.value&&options.value[index.value]){event.preventDefault();choose(options.value[index.value])}}
watch(query,()=>index.value=0)
onMounted(()=>document.addEventListener('click',outside));onUnmounted(()=>document.removeEventListener('click',outside))
</script>
<template><div ref="root" class="country-select" @keydown="keydown"><span class="country-label">国家 / 地区</span><button type="button" class="country-trigger" role="combobox" aria-label="国家 / 地区" aria-controls="subscription-country-options" :aria-expanded="open" aria-haspopup="listbox" :disabled="!countries.length" @click="open=!open;query=''">{{selected?label(selected):'暂无开放地区'}}<ChevronDown :size="16"/></button><div v-if="open" class="country-dropdown"><div class="country-search"><Search :size="16"/><input v-model="query" placeholder="搜索国家、代码或币种" aria-label="搜索国家、代码或币种" autocomplete="off"/></div><ul id="subscription-country-options" role="listbox" aria-label="可选国家"><li v-for="(row,i) in options" :key="row.code"><button type="button" role="option" :aria-selected="modelValue===row.code" :class="{focused:i===index,chosen:modelValue===row.code}" @click="choose(row)">{{label(row)}}<Check v-if="modelValue===row.code" :size="16"/></button></li><li v-if="!options.length" class="empty">没有符合条件的地区</li></ul></div></div></template>
<style scoped>
.country-select{position:relative;width:100%;min-width:0}.country-label{display:block;font-size:13px;font-weight:600;margin-bottom:8px}.country-trigger{display:flex;align-items:center;justify-content:space-between;gap:12px;background:var(--surface,#fff);color:var(--text,#243a30);border:1px solid var(--line,#dce6e0);border-radius:10px;padding:12px 14px;width:100%;text-align:left;min-height:46px}.country-trigger:disabled{opacity:.6}.country-dropdown{position:absolute;left:0;right:0;top:100%;z-index:30;margin-top:6px;background:var(--surface,#fff);border:1px solid var(--line,#dce6e0);border-radius:12px;box-shadow:0 12px 35px #183c2a20;overflow:hidden}.country-search{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line)}.country-search input{border:0!important;box-shadow:none!important;padding:6px!important;min-width:0;width:100%}.country-dropdown ul{list-style:none;margin:0;padding:6px;max-height:280px;overflow:auto}.country-dropdown li button{width:100%;display:flex;align-items:center;justify-content:space-between;text-align:left;border:0;background:transparent;border-radius:6px;padding:11px 10px;color:inherit}.country-dropdown li button:hover,.country-dropdown li button.focused{background:#f0f6f3}.country-dropdown li button.chosen{color:#19795a;font-weight:600}
</style>
