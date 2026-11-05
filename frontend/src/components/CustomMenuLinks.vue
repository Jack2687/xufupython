<script setup lang="ts">
import {ExternalLink,Globe2} from 'lucide-vue-next'
import type {Row} from '../lib'
withDefaults(defineProps<{items:Row[];route:string;prefix?:string}>(),{prefix:'/custom/'})
const icon=(svg:string)=>'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg)
</script>
<template><section v-if="items.length" class="custom-menu-links"><p>更多服务</p><a v-for="item in items" :key="item.id" :href="item.open_new_tab?item.url:'#'+prefix+item.id" :target="item.open_new_tab?'_blank':undefined" :rel="item.open_new_tab?'noopener noreferrer':undefined" :class="{active:route===prefix+item.id}"><img v-if="item.icon_svg" :src="icon(item.icon_svg)" alt=""/><Globe2 v-else :size="18"/><span>{{item.label}}</span><ExternalLink v-if="item.open_new_tab" :size="13" class="external-mark"/><span v-else-if="route===prefix+item.id" class="nav-dot"></span></a></section></template>
<style scoped>.custom-menu-links img{width:18px;height:18px;object-fit:contain;flex-shrink:0}.custom-menu-links a>span:not(.nav-dot){overflow-wrap:anywhere;min-width:0}.external-mark{margin-left:auto;opacity:.6;flex-shrink:0}</style>
