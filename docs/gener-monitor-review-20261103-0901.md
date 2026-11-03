# 订阅流程与支付全监控复核（更新至 2026-11-03 09:04）

> 历史快照：用户随后再次修改 records.py，管理员详情和列表已补基础付款状态与 ready。当前问题清单见 [09:06 最新复核](D:/aixiangmu/反代/xufu-python/docs/gener-current-review.md)，下文部分字段缺失结论不再成立。

仅检查 gener_direct_pay 从订阅办理到最终结果。本次依据 subscription.py 于 08:59:11、records.py 于 09:01:47 修改后的版本重新检查；用户在检查途中再次修复，已按新文件重读并更正结论。只更新报告，没有修改业务实现、运行付款器或重启服务。

## 已有变化与确认

- 当前源码已经增加初始出口 IP 检测、保存 expected_ip 及两处后续比对，不能再说“从未保存或比对出口”。见 [subscription.py:381](D:/aixiangmu/反代/xufu-python/backend/subscription.py:381)。这些新增普通事件尚未接到全监控技术日志，也不足以证明全程同 IP。
- 付款地址、姓名、邮箱改为从账单快照取值，缺必需内容报错，已删除原 HAR 地址/随机姓名/上游默认邮箱的相应兜底。见 [subscription.py:454](D:/aixiangmu/反代/xufu-python/backend/subscription.py:454)。国家仍使用 `billing.get('country') or 'US'`；账单编号和完整国家快照尚缺。
- 前次已经修正的冻结/解冻流水重复计算、余额行锁、accepted 检查、暂停领取、payable=false 拦截仍在，不再列为完全未做。
- 管理员订单详情已有每 15 秒刷新，不能说管理员详情完全不刷新。见 [Admin.vue:36](D:/aixiangmu/反代/xufu-python/frontend/src/Admin.vue:36)。客户详情的终态刷新条件则仍有问题。
- records.py 在本次检查途中更新。客户订单详情已经返回 checkout.ready、基础付款状态、fee_state 和提示；不能再说客户详情也完全缺这些字段。管理员详情仍使用另一段旧实现。paymentJourney.ts、Admin.vue、Customer.vue 没有相应改动。

用新 records.py 中实际函数配合离线假数据检查三种订单状态，未调用网络、数据库或付款代码：

| 订单状态 | 客户详情 | 管理员详情 |
| --- | --- | --- |
| queued | preparing、held；存在模拟链接时 ready=true | payment 为空，未提供 ready/fee_state/trace |
| processing | preparing、held；存在模拟链接时 ready=true | payment 为空，未提供 ready/fee_state/trace |
| succeeded | succeeded、charged；存在模拟链接时 ready=true | 有 succeeded 和卡尾号/金额，仍未提供 ready/fee_state/trace |

此验证说明客户和管理员返回结构不同，不证明实际订单已执行。客户 amount/currency 目前取自仅在成功后写入的 payment_amount/payment_currency，因此仍不能当作已补齐付款前报价。

## 支付全监控仍缺少的内容

| 编号 | 具体问题与可见影响 | 对应代码 |
| --- | --- | --- |
| M01 | **技术日志尚未接入。** 页面向 PaymentTrace 传 detail.trace，但后端只有 events，格式只有 stage/message/created_at。组件要求 phase/state/description/http_status/elapsed_ms 等，不能简单把字段名换成 trace 就算完整接通。 | [页面绑定](D:/aixiangmu/反代/xufu-python/frontend/src/Admin.vue:65)、[接口返回](D:/aixiangmu/反代/xufu-python/backend/records.py:116)、[事件写入](D:/aixiangmu/反代/xufu-python/backend/subscription.py:322)、[技术日志组件](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentTraceEvents.vue:24)。 |
| M02 | **全程/分阶段耗时、请求计数没有数据。** 前端依赖 complete 事件、elapsed_ms、request_id、diagnostics.request_kind；当前订阅事件没有这些字段，也没有最终 complete 事件。页面只能显示未记录，不能从日志条数推算实际请求次数。 | [耗时与请求计数](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:14)、[全程耗时](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:106)、[事件定义](D:/aixiangmu/反代/xufu-python/backend/schema.sql:27)、[事件写入](D:/aixiangmu/反代/xufu-python/backend/subscription.py:322)。 |
| M03 | **管理员链接成功与监控阶段不一致。** 客户接口已补 ready；管理员接口仍只返回 checkout.url/session_id，阶段完成条件却只看 checkout.ready 或 checkout_links。管理员阶段可能仍为尚未开始，链接类型和轮次也没有可靠来源。 | [管理员接口](D:/aixiangmu/反代/xufu-python/backend/records.py:108)、[完成条件](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:56)、[链接历史绑定](D:/aixiangmu/反代/xufu-python/frontend/src/Admin.vue:65)。 |
| M04 | **IP 记录与监控字段没有对上。** 新事件是 network_check/network_verified，且仅作为普通 stage/message 输出；全监控查找 phase=network、code=exit_ip_matched/proxy_session_changed。已有初始记录也不能让监控证明全程出口一致。 | [新事件](D:/aixiangmu/反代/xufu-python/backend/subscription.py:382)、[监控判定](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:119)。 |
| M05 | **客户修复未同步管理员，报价及过程粒度仍缺。** 客户已经有基础状态/fee_state，但 processing 全部显示 preparing，未区分提交与核查；amount 仍来自成功后才写入的字段。管理员详情仍只有保存卡尾号后才返回 payment，且缺 amount/currency/fee_state；列表 payment 仍固定为空对象。 | [客户新返回](D:/aixiangmu/反代/xufu-python/backend/records.py:54)、[列表](D:/aixiangmu/反代/xufu-python/backend/records.py:95)、[管理员详情](D:/aixiangmu/反代/xufu-python/backend/records.py:102)、[报价/结算显示](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:65)。 |
| M06 | **全监控可能提前显示办理完成。** status=succeeded 且没有 failed/review 阶段时直接显示 complete；activation=pending、renewal=pending 不属于 problems，会出现核查还在等待但总结果已完成。客户标签也有同类问题；客户详情可能提前停止刷新。 | [总结果判定](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:97)、[付款后写入 pending](D:/aixiangmu/反代/xufu-python/backend/subscription.py:342)、[客户标签](D:/aixiangmu/反代/xufu-python/frontend/src/lib.ts:18)、[客户刷新](D:/aixiangmu/反代/xufu-python/frontend/src/Customer.vue:79)。 |
| M07 | **本单流程、账单资料映射不全。** 订单未保存/返回 subscription_flow，名称字典缺 gener_direct_pay；后台账单返回 state_code，前端读 region。profile_id 没有保存或返回，country 从单条资料取但国家在 billing 配置组。 | [订单写入](D:/aixiangmu/反代/xufu-python/backend/subscription.py:263)、[快照](D:/aixiangmu/反代/xufu-python/backend/subscription.py:259)、[后台账单](D:/aixiangmu/反代/xufu-python/backend/records.py:115)、[名称字典](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:18)、[账单组件](D:/aixiangmu/反代/xufu-python/frontend/src/components/OrderBilling.vue:14)。 |
| M08 | **异常处置按钮没有完整后端。** 付款核查和管理员人工关闭的页面调用存在，但本次 AST 路由清点没有对应处理器。运行状态、可核查标记、关闭历史也没有完整投影。 | [关闭调用](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:10)、[核查调用](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:20)、[后台当前返回](D:/aixiangmu/反代/xufu-python/backend/records.py:116)。 |
| M09 | **新可用标志与执行能力及先前停用约定不一致。** public 现在将三个流程都按配置标为 connected，但 create_order 仅接受 gener_direct_pay；catalog 和 quote 仍固定 payment_connected=false。对 gener 而言，该变更会使入口依配置变为可用，不能把它误报为“付款仍硬关闭”。当前没有上线验收，也未启动服务。 | [新 public](D:/aixiangmu/反代/xufu-python/backend/records.py:17)、[执行限制](D:/aixiangmu/反代/xufu-python/backend/subscription.py:191)、[报价可用标志](D:/aixiangmu/反代/xufu-python/backend/membership.py:59)。 |

## 影响监控真实性及最终结果的业务问题

| 编号 | 仍存在的问题 | 对应代码 |
| --- | --- | --- |
| B01 | **未知付款结果仍作为失败。** payment_result_unconfirmed 抛出后，通用捕获调用 _fail_order，把订单变 failed 并解冻；没有可靠进入原交易待核查状态。 | [未知结果](D:/aixiangmu/反代/xufu-python/backend/subscription.py:634)、[通用捕获](D:/aixiangmu/反代/xufu-python/backend/subscription.py:748)、[失败收尾](D:/aixiangmu/反代/xufu-python/backend/subscription.py:326)。 |
| B02 | **缺实收凭据时仍填预计金额。** amount_received 缺失或为 0 时回填 expected_amount，但接口继续称 charged_amount、页面称已核实实收，不能区分预计与独立核实。 | [回填](D:/aixiangmu/反代/xufu-python/backend/subscription.py:638)、[返回](D:/aixiangmu/反代/xufu-python/backend/records.py:107)、[显示](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:29)。 |
| B03 | **生效状态并非严格目标验证。** 套餐不匹配只记警告仍标 active，席位未验证；直接 bool 转换上游字段，新分支未保留 status 方法里的账号一致性和类型检查。 | [生效判断](D:/aixiangmu/反代/xufu-python/backend/subscription.py:671)、[不匹配警告](D:/aixiangmu/反代/xufu-python/backend/subscription.py:676)、[标 active](D:/aixiangmu/反代/xufu-python/backend/subscription.py:693)、[原 status 校验](D:/aixiangmu/反代/xufu-python/backend/renewals.py:165)。 |
| B04 | **未完成订单没有持久恢复。** queued 变 processing 后没有任务租约、心跳或重启恢复；迟延生效写 pending 后不再调度。取消续费缺持久化提交标记，保留续费不核实实际 renew。领取虽支持 paused，仍未检查 payment.enabled。 | [领取](D:/aixiangmu/反代/xufu-python/backend/subscription.py:294)、[续费](D:/aixiangmu/反代/xufu-python/backend/subscription.py:699)、[pending](D:/aixiangmu/反代/xufu-python/backend/subscription.py:734)、[调度](D:/aixiangmu/反代/xufu-python/backend/main.py:35)。 |
| B05 | **最终资金处理没有原状态保护。** 流水重复计算已修，但成功/失败都直接按 id 更新，后续异常可覆盖成功状态。不同的 charge/unfreeze 唯一键不阻止成功后又解冻。 | [失败](D:/aixiangmu/反代/xufu-python/backend/subscription.py:326)、[成功](D:/aixiangmu/反代/xufu-python/backend/subscription.py:339)、[成功后事件](D:/aixiangmu/反代/xufu-python/backend/subscription.py:642)。 |
| B06 | **幂等和严格参数仍不完整。** 摘要仅含 workflow/product_id/country/seats，未覆盖账号和续费等输入；同键并发查询发生在用户行锁前，锁后不重查。keep_renewal=bool(v) 及 int 席位转换仍会接受不符合预期的输入；卡年份只有数字检查。 | [请求摘要](D:/aixiangmu/反代/xufu-python/backend/subscription.py:238)、[锁](D:/aixiangmu/反代/xufu-python/backend/subscription.py:250)、[参数](D:/aixiangmu/反代/xufu-python/backend/subscription.py:205)。 |
| B07 | **新增出口检测不能证明整单身份一致。** 新函数的到期查询选全表最新代理会话而非本订单；本次执行重新写初始 IP，监控又没有后续比对证据。因此不能将“已有比对函数”当作本单全程同 IP 的验收结果。 | [到期查询](D:/aixiangmu/反代/xufu-python/backend/subscription.py:161)、[基准记录](D:/aixiangmu/反代/xufu-python/backend/subscription.py:381)。本轮只陈述审计事实，没有增强该付款路径的代理逻辑。 |
| B08 | **敏感数据清理、日志脱敏和测试未补齐。** subscription_runs 的卡/授权数据没有终态或过期清理；generator 仍可将完整 HTTP 代理 URL 写日志。没有 run_subscription 全程测试，旧 test_13 仍断言路由及表不存在。 | [保存](D:/aixiangmu/反代/xufu-python/backend/subscription.py:278)、[维护](D:/aixiangmu/反代/xufu-python/backend/main.py:27)、[代理日志](D:/aixiangmu/反代/xufu-python/generator.py:707)、[旧测试](D:/aixiangmu/反代/xufu-python/tests/test_backend.py:267)。 |

## 两处关键现有代码

监控总结果的完成条件：

```typescript
// frontend/src/paymentJourney.ts:97
const problems=activeStages.filter(s=>['failed','review'].includes(s.state)&&s.id!=='complete')
if(o.status==='succeeded')set('complete',problems.length?['review','付款完成，后续仍需处理']:['completed',subscription?'订阅办理完成':'办理完成'])
```

这里没有将“生效或续费仍在 pending/running”排除出完成条件，因此不是单纯缺少几条日志的问题。

后端只有普通事件结构：

```python
# backend/subscription.py:322
def _add_event(order_id: str, stage: str, message: str):
    db.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,$2,$3)", order_id, stage, message)
```

阶段耗时、HTTP 状态、原请求关联、重试次数和完成依据都不能从这三个参数中直接得到；不应将空缺历史数据伪造为已执行。

## 本地验证范围

本次端口检查：127.0.0.1:18970 与 18972 均不可连接。因此本轮没有数据库或在线页面验收，也不能沿用早前“服务正常、订单为零”的状态作为当前结果。未启动或重启服务。

验证方式：源代码阅读、AST 路由清点、哈希记录、端口连通性检查，以及新 records.py 的客户/管理员响应离线假数据检查（排队、处理中、成功）。未运行真实付款，未修改、启用或完善生成设备/行为信号的付款路径。

源码 SHA-256：

```text
backend/subscription.py 598be9e4f69f562b0f09737ff2114b865acb6e454b10e89e3fd7862d06b4d620
backend/records.py b70d55fda4e50324aa422da84061e2eba076d1d1943a1076983f18d4cdccd558
frontend/src/paymentJourney.ts 51d3df549a07fce80b4d85b751c74be350dba1c96fe5e0245353a57ff82c4357
frontend/src/Admin.vue 93a7671c3b1d7c66f89d24df3f68b75d32e3a7e5af774dfad2e2f2371739cb37
```
