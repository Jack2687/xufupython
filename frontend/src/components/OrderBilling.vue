<script setup lang="ts">
import {MapPin} from 'lucide-vue-next'
import type {Row} from '../lib'
defineProps<{billing?:Row}>()
</script>

<template>
  <section class="order-billing" aria-label="本单账单资料">
    <div class="billing-title"><h3><MapPin :size="16"/>本单账单资料</h3><small>仅管理员可见</small></div>
    <template v-if="billing?.state==='selected'">
      <p>本单随机选中的资料，保留办理时的原始记录。</p>
      <p v-if="billing.country_source==='legacy_us_billing'">此历史记录未保存国家，按当时系统固定 US 账单规则补显；未改写原始资料。</p>
      <dl>
        <dt>姓名</dt><dd>{{billing.first_name}} {{billing.last_name}}</dd>
        <dt>账单邮箱</dt><dd>{{billing.email||'未记录'}}<small v-if="billing.email_source"> · {{billing.email_source==='system_billing'?'系统账单资料':'本次订阅账号'}}</small></dd>
        <dt>国家 / 州</dt><dd>{{billing.country||'未记录'}} / {{billing.region||billing.state_code||'未记录'}}</dd>
        <dt>城市</dt><dd>{{billing.city}}</dd>
        <dt>街道地址</dt><dd>{{billing.line1}}</dd>
        <dt>邮政编码</dt><dd>{{billing.postal_code}}</dd>
        <template v-if="billing.profile_id"><dt>资料编号</dt><dd class="mono">{{billing.profile_id}}</dd></template>
      </dl>
    </template>
    <p v-else>{{billing?.state==='unavailable'?'本单账单资料暂时无法读取，请稍后刷新。':'本单尚无已保存的账单资料。'}}</p>
  </section>
</template>

<style scoped>
.order-billing{margin:20px 0;padding:20px 22px;border:1px solid #dbe4ee;border-radius:12px;background:#fff}.billing-title{display:flex;justify-content:space-between;align-items:center;gap:12px}.billing-title h3{display:flex;align-items:center;gap:8px;margin:0;font-size:14px}.billing-title small,.order-billing p{font-size:12px;color:#77849a}.order-billing p{margin:10px 0;line-height:1.7}.order-billing dl{display:grid;grid-template-columns:auto minmax(0,1fr);gap:9px 22px;margin:16px 0 0;font-size:12px;line-height:1.7}.order-billing dt{color:#77849a}.order-billing dd{margin:0;overflow-wrap:anywhere}@media(max-width:650px){.order-billing{padding:16px}.order-billing dl{gap:8px 14px}}
</style>
