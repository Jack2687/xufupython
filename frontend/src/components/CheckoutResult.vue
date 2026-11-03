<script setup lang="ts">
import {Copy,ExternalLink} from 'lucide-vue-next'
import {copy,date,type Row} from '../lib'
defineProps<{checkout?:Row;paymentState?:string}>()
</script>
<template><section class="checkout-result"><div v-if="checkout?.waiting_message" class="callout">{{checkout.waiting_message}}<small v-if="checkout.next_check_at">下次检查：{{date(checkout.next_check_at)}}</small></div><div v-if="checkout?.ready" class="checkout-link-box"><h3>结账链接已生成</h3><p v-if="!paymentState">链接生成任务已完成。本工具不提交付款、不冻结或收取服务费。</p><p v-else>付款进度与结算状态见下方，处理中请勿重复付款。</p><div class="checkout-url mono">{{checkout.url}}</div><div class="actions"><button type="button" class="btn primary" @click="copy(checkout.url)"><Copy :size="16"/>复制结账链接</button><a class="btn secondary" :href="checkout.url" target="_blank" rel="noreferrer noopener"><ExternalLink :size="16"/>打开付款页</a></div><small>链接在本平台保留至 {{date(checkout.expires_at)}}；上游实际有效期以付款页为准。</small></div><div v-else-if="checkout?.expired" class="callout">链接已超过本平台保留时间，请新建结账准备。</div></section></template>
