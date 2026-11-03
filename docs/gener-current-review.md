# gener_direct_pay 当前剩余问题（2026-11-03 09:06）

> 2026-11-03 后续修复：本报告以下内容为 09:06 历史快照。排队调度、完整账单快照和邮箱兜底、付款地址消费、独立技术日志及真实请求状态/耗时、本机代理链已调整，最新行为和验证见 [运行与订单记录修复](runtime-and-order-records.md)。付款恢复、完整浏览器资源诊断仍未实现；自动付款仍未启用。

以用户最新两次修改后读取到的源码为准：subscription.py 修改于 08:59:11，records.py 修改于 09:04:41。本报告取代 09:01/09:04 及更早报告的现状清单。只检查本流程和支付全监控，不扩展另外两种流程、独立提链菜单或开发者 API 的完整实现。

## 已确认补齐，不再列为缺失

- 客户/管理员订单列表都通过 `_order_list_payment` 返回基础付款状态、服务费状态，已不是固定空 payment。[records.py:82](D:/aixiangmu/反代/xufu-python/backend/records.py:82)
- 客户/管理员详情都补了 `checkout.ready`、基础付款状态、`fee_state` 和提示。[客户详情](D:/aixiangmu/反代/xufu-python/backend/records.py:40)、[管理员详情](D:/aixiangmu/反代/xufu-python/backend/records.py:118)
- 基础金额字段已经返回，但来源仍是成功后保存的 payment_amount，不能据此认定已有付款前报价或独立实收凭据。
- 管理员详情原本已有每 15 秒刷新，不属于未实现项目。[Admin.vue:36](D:/aixiangmu/反代/xufu-python/frontend/src/Admin.vue:36)
- 08:59 版已经增加初始出口记录和部分后续比对；账单姓名、邮箱、地址已改为从系统快照读取并拒绝缺项。不能继续用更早的“完全没有比对”“仍使用 HAR 地址/随机姓名”的结论。
- 前次已修的服务费流水重复加减、余额行锁、用户确认、基础卡字段检查、暂停领取、不可付款链接拦截仍在。

## 支付全监控尚未完成

| 编号 | 现存缺口及影响 | 当前代码位置 |
| --- | --- | --- |
| M1 | **详细技术日志未接入。** 管理员页面读 `detail.trace`，接口仍只返回普通 `events`。普通事件只有 stage/message/created_at，缺 phase/state/description/http_status/elapsed_ms/request_id/diagnostics，不能仅改字段名就当作真实技术日志。 | [Admin.vue:65](D:/aixiangmu/反代/xufu-python/frontend/src/Admin.vue:65)、[records.py:138](D:/aixiangmu/反代/xufu-python/backend/records.py:138)、[subscription.py:322](D:/aixiangmu/反代/xufu-python/backend/subscription.py:322) |
| M2 | **耗时、请求计数和链接历史未记录完整。** 没有 trace、complete 事件或 checkout_links，阶段耗时、全程时间、实际提交次数、链接类型和重建轮次不能可靠显示。ready 已补，故“提链阶段完全不认成功”的旧问题应撤下；链接历史仍缺。 | [paymentJourney.ts:14](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:14)、[链接类型:56](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:56)、[完成时间:106](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:106)、[接口:138](D:/aixiangmu/反代/xufu-python/backend/records.py:138) |
| M3 | **IP 摘要仍无法显示真实比对结果。** 执行器写 network_check/network_verified 普通事件；组件需要 phase=network、code=exit_ip_matched/proxy_session_changed。目前不匹配，已有事件不能证明整单出口不变。 | [新事件:382](D:/aixiangmu/反代/xufu-python/backend/subscription.py:382)、[摘要条件:119](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:119) |
| M4 | **办理完成可能提前显示。** 前端只把 failed/review 阶段视为 problems，订单 status=succeeded 就能显示完成；activation/renewal 仍 pending/running 时不阻止。客户标签和停止刷新条件也未改。 | [paymentJourney.ts:97](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:97)、[lib.ts:18](D:/aixiangmu/反代/xufu-python/frontend/src/lib.ts:18)、[Customer.vue:79](D:/aixiangmu/反代/xufu-python/frontend/src/Customer.vue:79)、[付款后状态:342](D:/aixiangmu/反代/xufu-python/backend/subscription.py:342) |
| M5 | **付款阶段粒度和报价不完整。** 新辅助函数将所有 processing 映射为 preparing，无法区分提交中、确认中、结果核查。amount/currency 使用成功后字段，付款前仍无独立报价/税费记录；实收真实性另见 B2。 | [状态映射:61](D:/aixiangmu/反代/xufu-python/backend/records.py:61)、[详情金额:134](D:/aixiangmu/反代/xufu-python/backend/records.py:134)、[成功写入:346](D:/aixiangmu/反代/xufu-python/backend/subscription.py:346) |
| M6 | **流程名称和账单映射未对齐。** 订单未保存本单 subscription_flow；名称表没有 gener_direct_pay。账单接口仍返回 state_code、组件读 region；无 profile_id，快照国家未从配置组取得。 | [订单写入:263](D:/aixiangmu/反代/xufu-python/backend/subscription.py:263)、[名称表:18](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:18)、[账单输出:137](D:/aixiangmu/反代/xufu-python/backend/records.py:137)、[账单组件:14](D:/aixiangmu/反代/xufu-python/frontend/src/components/OrderBilling.vue:14)、[快照:259](D:/aixiangmu/反代/xufu-python/backend/subscription.py:259) |
| M7 | **异常单处置没有对应接口。** 页面仍调用付款核查/管理员关闭；AST 路由检查未找到处理器，详情也没有 closures、can_check 等完整结构。 | [PaymentPanel.vue:10](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:10)、[核查:20](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:20)、[详情:138](D:/aixiangmu/反代/xufu-python/backend/records.py:138) |
| M8 | **入口可用标志不等于能力接通。** public 把三个流程都按配置视为 connected，create_order 却仅允许 gener_direct_pay；catalog/overview 只看 enabled，quote 仍返回 false。新代码已不再把入口硬关闭，不能继续声称其始终未开放。 | [records.py:17](D:/aixiangmu/反代/xufu-python/backend/records.py:17)、[catalog:100](D:/aixiangmu/反代/xufu-python/backend/records.py:100)、[执行限制:191](D:/aixiangmu/反代/xufu-python/backend/subscription.py:191)、[报价:59](D:/aixiangmu/反代/xufu-python/backend/membership.py:59) |

## 仍影响全程正确性的业务问题

| 编号 | 问题 | 当前代码位置 |
| --- | --- | --- |
| B1 | **付款未知结果仍变失败并解冻。** payment_result_unconfirmed 经通用异常进入 _fail_order，没有原交易待核查/恢复状态。 | [subscription.py:634](D:/aixiangmu/反代/xufu-python/backend/subscription.py:634)、[捕获:748](D:/aixiangmu/反代/xufu-python/backend/subscription.py:748)、[失败处理:326](D:/aixiangmu/反代/xufu-python/backend/subscription.py:326) |
| B2 | **无实收证据时仍使用预计金额。** `actual_charged = charged_minor if charged_minor > 0 else expected_amount`，但接口/页面仍标为实收。 | [subscription.py:638](D:/aixiangmu/反代/xufu-python/backend/subscription.py:638)、[PaymentPanel.vue:29](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:29) |
| B3 | **生效验证仍可能误报。** 套餐不匹配只警告，仍标 active；无席位核验。直接 bool 转换上游字段，直接 request 分支没有原 status 中的账号一致性检查。 | [subscription.py:671](D:/aixiangmu/反代/xufu-python/backend/subscription.py:671)、[不匹配:676](D:/aixiangmu/反代/xufu-python/backend/subscription.py:676)、[标生效:693](D:/aixiangmu/反代/xufu-python/backend/subscription.py:693) |
| B4 | **恢复和续费收尾不足。** 没有订阅任务租约、付款提交检查点和重启恢复；迟延生效写 pending 后不再调度；取消续费没有持久化防重复标记，保留续费不核实实际 renew。领取虽然检查 paused，未检查 payment.enabled。 | [领取:294](D:/aixiangmu/反代/xufu-python/backend/subscription.py:294)、[续费:699](D:/aixiangmu/反代/xufu-python/backend/subscription.py:699)、[pending:734](D:/aixiangmu/反代/xufu-python/backend/subscription.py:734)、[main.py:35](D:/aixiangmu/反代/xufu-python/backend/main.py:35) |
| B5 | **结算终态和受理幂等仍不完整。** 成功/失败更新不限制旧状态，成功后的异常可覆盖成功并解冻。请求摘要只含四项，未包括账号/续费/工作区/高级席位；同键并发查询在用户锁之前，锁后不重查。 | [失败:326](D:/aixiangmu/反代/xufu-python/backend/subscription.py:326)、[成功:339](D:/aixiangmu/反代/xufu-python/backend/subscription.py:339)、[摘要及查单:238](D:/aixiangmu/反代/xufu-python/backend/subscription.py:238) |
| B6 | **严格参数及完整资料快照仍有缺口。** keep_renewal 使用 bool 转换，席位 int 可接受非预期类型，卡年份缺有效期校验；账单国家在执行阶段兜底 US，资料编号和配置组国家未完整快照。 | [参数:205](D:/aixiangmu/反代/xufu-python/backend/subscription.py:205)、[快照:259](D:/aixiangmu/反代/xufu-python/backend/subscription.py:259)、[国家取值:458](D:/aixiangmu/反代/xufu-python/backend/subscription.py:458) |
| B7 | **新增 IP 检测尚不能算全程一致验收。** 到期查询选择全表最新会话，而非本订单；每次执行重新写基准，监控也没有持续的同单比对证据。这里只审计事实，没有修改或增强该付款路径。 | [subscription.py:161](D:/aixiangmu/反代/xufu-python/backend/subscription.py:161)、[基准:381](D:/aixiangmu/反代/xufu-python/backend/subscription.py:381) |
| B8 | **资料清理、脱敏和完整测试仍缺。** 卡/授权数据没有终态或到期清理；generator 可将代理原始 URL 写入日志。测试目录无完整订阅执行测试，旧 test_13 仍断言路由和表不存在。 | [维护:27](D:/aixiangmu/反代/xufu-python/backend/main.py:27)、[日志:707](D:/aixiangmu/反代/xufu-python/generator.py:707)、[旧测试:267](D:/aixiangmu/反代/xufu-python/tests/test_backend.py:267) |

## 本次验证

提取新 records.py 中实际的订单列表/详情函数，注入固定假数据运行；没有使用数据库、网络、真实 Session 或银行卡。queued、processing、succeeded、failed、review 五种状态下，客户和管理员列表/详情返回的基础付款及 fee_state 一致；假链接存在时客户/管理员 ready 均为 true。剩余缺少 trace、checkout_links、closures，账单输出仍为 state_code。

本次 09:01 检测应用端口 18970 和数据库端口 18972 都不可连接；此后没有启动服务，所以没有在线验收或当前数据库配置结论。未执行支付、未修改业务代码、未启用现有设备/行为信号付款路径。新增 public 可用标志由用户源码修改产生，不是本次操作。

09:10 最后复核：下列四份源码哈希仍一致；报告 55 个代码链接的目标文件与行号均有效。subscription.py 和 records.py 的 Python AST 语法检查通过。再次检测 18970、18972 均不可连接，因此上述结果仍限于源码和离线检查，不代表完整订阅流程已通过在线验收。

源码快照 SHA-256：

```text
backend/subscription.py 598be9e4f69f562b0f09737ff2114b865acb6e454b10e89e3fd7862d06b4d620
backend/records.py 9798ebf68107cf03da1f6f6ad98754068406cf5c4fdfffd85527c3c9b133ccb9
frontend/src/paymentJourney.ts 51d3df549a07fce80b4d85b751c74be350dba1c96fe5e0245353a57ff82c4357
frontend/src/Admin.vue 93a7671c3b1d7c66f89d24df3f68b75d32e3a7e5af774dfad2e2f2371739cb37
```
