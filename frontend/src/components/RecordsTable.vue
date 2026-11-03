<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import {ChevronLeft,ChevronRight,SearchX} from 'lucide-vue-next'
import type {Row} from '../lib'
const props=defineProps<{rows:Row[];columns:{key:string;label:string;wide?:boolean}[];loading?:boolean;scope:string;filtered?:boolean}>()
defineEmits<{reset:[]}>()
const page=ref(1),size=ref(12)
const pages=computed(()=>Math.max(1,Math.ceil(props.rows.length/size.value)))
const visible=computed(()=>props.rows.slice((page.value-1)*size.value,page.value*size.value))
// Status refreshes preserve the current page; changing the result set resets it.
watch(()=>props.rows.map(r=>r.id).join(','),()=>page.value=1)
watch(size,()=>page.value=1)
</script>
<template>
 <div class="records-count" role="status"><span><b>{{rows.length}}</b> 条{{filtered?'匹配':''}}记录 <span class="records-scope">· {{scope}}</span></span><span v-if="loading">正在刷新…</span></div>
 <div class="records-scroll" tabindex="0" aria-label="记录列表，可横向滚动">
  <table class="records-table"><thead><tr><th v-for="c in columns" :key="c.key" scope="col">{{c.label}}</th></tr></thead>
   <tbody><tr v-for="r in visible" :key="r.id"><td v-for="c in columns" :key="c.key" :data-label="c.label" :class="{'record-wide':c.wide}"><div class="record-cell"><slot :name="c.key" :row="r">{{r[c.key]??'—'}}</slot></div></td></tr></tbody>
  </table>
  <div v-if="!rows.length" class="records-empty"><SearchX :size="28"/><b>{{loading?'正在读取记录…':filtered?'没有符合条件的记录':'暂时没有记录'}}</b><p>{{filtered?'试试其他关键词，或清除筛选条件。':'新的业务记录会显示在这里。'}}</p><button v-if="filtered" class="text-btn" @click="$emit('reset')">清除筛选</button></div>
 </div>
 <div class="records-pagination"><label>每页<select v-model.number="size" aria-label="每页条数"><option :value="12">12 条</option><option :value="24">24 条</option><option :value="48">48 条</option></select></label><div><span>{{page}} / {{pages}}</span><button class="icon-btn bordered" :disabled="page===1" aria-label="上一页" @click="page--"><ChevronLeft :size="16"/></button><button class="icon-btn bordered" :disabled="page>=pages" aria-label="下一页" @click="page++"><ChevronRight :size="16"/></button></div></div>
</template>
