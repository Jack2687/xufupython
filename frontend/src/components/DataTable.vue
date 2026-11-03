<script setup lang="ts">
import {ref,computed,watch} from 'vue'
import {Search,ChevronLeft,ChevronRight,Inbox} from 'lucide-vue-next'
import type {Row} from '../lib'
const props=defineProps<{rows:Row[];columns:{key:string;label:string}[];selectable?:boolean;searchable?:boolean}>()
const emit=defineEmits<{selection:[ids:string[]]}>()
const q=ref(''),page=ref(1),selected=ref<string[]>([])
const filtered=computed(()=>props.rows.filter(r=>!q.value||JSON.stringify(r).toLowerCase().includes(q.value.toLowerCase())))
const pages=computed(()=>Math.max(1,Math.ceil(filtered.value.length/12)))
const visible=computed(()=>filtered.value.slice((page.value-1)*12,page.value*12))
watch(q,()=>page.value=1);watch(()=>props.rows,()=>{page.value=Math.min(page.value,pages.value);selected.value=selected.value.filter(x=>props.rows.some(r=>r.id===x));emit('selection',selected.value)})
function toggle(id:string){selected.value=selected.value.includes(id)?selected.value.filter(x=>x!==id):[...selected.value,id];emit('selection',selected.value)}
function all(){const ids=visible.value.map(x=>x.id);selected.value=ids.every(id=>selected.value.includes(id))?selected.value.filter(id=>!ids.includes(id)):[...new Set([...selected.value,...ids])];emit('selection',selected.value)}
</script>
<template><div class="data-table"><div class="table-tools"><div class="search-box" v-if="searchable!==false"><Search :size="16"/><input v-model="q" placeholder="搜索当前列表…" aria-label="搜索当前列表"/></div><span class="muted">{{ filtered.length }} 条记录 <template v-if="selected.length">· 已选 {{ selected.length }}</template></span></div><div class="table-scroll"><table><thead><tr><th v-if="selectable"><input type="checkbox" aria-label="选择本页" :checked="visible.length>0&&visible.every(x=>selected.includes(x.id))" @change="all"/></th><th v-for="c in columns" :key="c.key">{{ c.label }}</th></tr></thead><tbody><tr v-for="(r,i) in visible" :key="r.id||i"><td v-if="selectable"><input type="checkbox" :aria-label="`选择 ${r.email||r.prefix||r.id}`" :checked="selected.includes(r.id)" @change="toggle(r.id)"/></td><td v-for="c in columns" :key="c.key"><slot :name="c.key" :row="r">{{ r[c.key]??'—' }}</slot></td></tr></tbody></table><div class="empty" v-if="!filtered.length"><Inbox :size="30"/><strong>这里还没有记录</strong><p>新的业务记录会显示在这里。</p></div></div><div class="pagination"><span>每页 12 条</span><div><button class="icon-btn" :disabled="page===1" @click="page--" aria-label="上一页"><ChevronLeft :size="16"/></button><span>{{ page }} / {{ pages }}</span><button class="icon-btn" :disabled="page===pages" @click="page++" aria-label="下一页"><ChevronRight :size="16"/></button></div></div></div></template>
