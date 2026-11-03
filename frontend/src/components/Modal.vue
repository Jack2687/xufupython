<script setup lang="ts">
import {onMounted,onUnmounted,ref} from 'vue'
import {X} from 'lucide-vue-next'
defineProps<{title:string;wide?:boolean}>();const emit=defineEmits<{close:[]}>();const el=ref<HTMLElement>();let previous:Element|null
function onKey(e:KeyboardEvent){if(e.key==='Escape')emit('close');if(e.key==='Tab'){const nodes=el.value?.querySelectorAll<HTMLElement>('button,input,select,textarea,a[href]');if(!nodes?.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}}
onMounted(()=>{previous=document.activeElement;document.body.style.overflow='hidden';el.value?.focus();document.addEventListener('keydown',onKey)})
onUnmounted(()=>{document.body.style.overflow='';document.removeEventListener('keydown',onKey);(previous as HTMLElement)?.focus()})
</script>
<template><Teleport to="body"><div class="modal-overlay" @click.self="emit('close')"><section ref="el" tabindex="-1" class="modal" :class="{wide}" role="dialog" aria-modal="true" :aria-label="title"><header><div><span class="eyebrow">XUPAY WORKSPACE</span><h2>{{ title }}</h2></div><button class="icon-btn" aria-label="关闭窗口" @click="emit('close')"><X :size="22"/></button></header><div class="modal-body"><slot/></div></section></div></Teleport></template>
