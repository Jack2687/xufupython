# 订阅流程代码检查（2026-11-03）

> 本文保留 02:01 的历史检查快照；后续源码已有修改。用户最新范围仅为 gener_direct_pay 从订阅办理到最终结果，当前清单见 [gener 订阅全程检查](D:/aixiangmu/反代/xufu-python/docs/gener-subscription-checklist-20261103.md)。不要继续将本文旧代理实现结论当作现状。

检查对象：`D:/aixiangmu/反代/xufu-python`，截至北京时间 02:01 读取到的源码。检查方式为源码阅读、AST 路由清点、数据库计数与本地只读健康接口；没有运行订阅执行器、提交订单或实际付款。本文描述当前缺口，不是付款路径的启用方案。

本地进程启动时间为 2026-11-03 01:22:47；`subscription.py` 修改于 01:55:15，`records.py` 修改于 01:57:43。[启动入口](D:/aixiangmu/反代/xufu-python/scripts/serve.py:12) 未启用自动重载，因此这些后续修改尚未加载到当前服务。健康接口正常，当前保存流程为 `cslive`，提链/付款可用状态均为 false，订单和 subscription_runs 均为 0。付款配置 enabled=true，不能把前端 false 等同于服务端完整停用。

已确认的新进展，不能继续列为“完全没做”：

| 项目 | 当前源码证据 | 验证程度 |
| --- | --- | --- |
| gener 选项对应值 | [subscription.py:27](D:/aixiangmu/反代/xufu-python/backend/subscription.py:27) 已是 `FLOW = 'gener_direct_pay'` | 源码已改，当前进程未加载 |
| 下单风控配置组 | [subscription.py:70](D:/aixiangmu/反代/xufu-python/backend/subscription.py:70) 从 risk 读取限流和订单上限 | 源码已改，未完成订单受理测试 |
| 账单姓名/邮箱保存 | [subscription.py:119](D:/aixiangmu/反代/xufu-python/backend/subscription.py:119) 已写入快照 | 部分完成，实际消费字段仍有问题 |
| 成功扣费流水余额 | [subscription.py:210](D:/aixiangmu/反代/xufu-python/backend/subscription.py:210) 已直接记录更新后的 balance/frozen | 源码已改；冻结和解冻流水仍有错误 |
| 管理员详情 | [records.py:75](D:/aixiangmu/反代/xufu-python/backend/records.py:75) 开始返回付款、链接、账单 | 部分完成，字段与前端契约尚未完全对齐 |
| 生效与续费轮询 | [subscription.py:459](D:/aixiangmu/反代/xufu-python/backend/subscription.py:459) 已增加轮询 | 部分完成，持久化恢复和防重复未完成 |
| 服务费报价 | [membership.py:51](D:/aixiangmu/反代/xufu-python/backend/membership.py:51) | 之前的针对性测试与本地页面验证通过 |
| gener 全套餐长链配置 | [ConfigEditor.vue:13](D:/aixiangmu/反代/xufu-python/frontend/src/components/ConfigEditor.vue:13)、[settings.py:91](D:/aixiangmu/反代/xufu-python/backend/settings.py:91) | 前后端约束、构建、针对性测试与页面验证通过 |

尚未完成或仍有问题的代码位置：

| 编号 | 问题及影响 | 代码位置与证据 |
| --- | --- | --- |
| R01 | 最新代码尚未被本地运行版本加载；源码修复不能直接算作本地已生效。 | [serve.py:12](D:/aixiangmu/反代/xufu-python/scripts/serve.py:12)，进程时间与上述源码修改时间对比。 |
| R02 | 停用状态未统一。页面仍关闭入口，但后端路由和线程已注册；任务领取不检查 payment.enabled、scheduler.paused。切换配置或已有排队任务时，不能依赖前端 false 阻止执行。 | [records.py:16](D:/aixiangmu/反代/xufu-python/backend/records.py:16)、[main.py:52](D:/aixiangmu/反代/xufu-python/backend/main.py:52)、[subscription.py:151](D:/aixiangmu/反代/xufu-python/backend/subscription.py:151)。 |
| R03 | 两个 cslive 选项没有独立执行流程。gener 常量虽已正确，订单仍没有保存本单流程标识；执行函数读取当前配置但没有使用该变量分派流程。 | [subscription.py:27](D:/aixiangmu/反代/xufu-python/backend/subscription.py:27)、[subscription.py:117](D:/aixiangmu/反代/xufu-python/backend/subscription.py:117)、[subscription.py:224](D:/aixiangmu/反代/xufu-python/backend/subscription.py:224)、[schema.sql:134](D:/aixiangmu/反代/xufu-python/backend/schema.sql:134)。 |
| R04 | 代理厂商及动态/静态信息没有传到执行代码：acquire 只取 id、endpoint_enc、capacity，下游只能用默认值。两厂商的会话参数处理也未与原项目对齐。 | [proxies.py:170](D:/aixiangmu/反代/xufu-python/backend/proxies.py:170)、[subscription.py:243](D:/aixiangmu/反代/xufu-python/backend/subscription.py:243)、[subscription.py:34](D:/aixiangmu/反代/xufu-python/backend/subscription.py:34)。 |
| R05 | 未实现整单出口一致性。优先取续费 global_id，没有按订单国家匹配；每次执行重新生成 session_id，没有冻结原代理端点、首次出口和有效期；也没有付款前/批准前及后续步骤的出口比对。代理容量租约只有 600 秒，后续没有检查或延续。 | [subscription.py:234](D:/aixiangmu/反代/xufu-python/backend/subscription.py:234)、[subscription.py:242](D:/aixiangmu/反代/xufu-python/backend/subscription.py:242)、[schema.sql:143](D:/aixiangmu/反代/xufu-python/backend/schema.sql:143)。现有 [probe_exit](D:/aixiangmu/反代/xufu-python/backend/proxies.py:128) 未用于订阅步骤。 |
| R06 | 账单快照仍不完整且未被一致使用。无可用资料时返回空对象；快照没有资料编号，country 从资料行读取但资料国家实际在配置组；付款邮箱仍取上游或旧默认邮箱，缺字段仍使用默认地址、随机姓名。 | [subscription.py:50](D:/aixiangmu/反代/xufu-python/backend/subscription.py:50)、[subscription.py:119](D:/aixiangmu/反代/xufu-python/backend/subscription.py:119)、[subscription.py:308](D:/aixiangmu/反代/xufu-python/backend/subscription.py:308)、[stripe_hosted.py:205](D:/aixiangmu/反代/xufu-python/backend/prolong/stripe_hosted.py:205)。 |
| R07 | 保存草稿与实际执行未区分。前端 createDraft(false) 不传 prepare，后端不读取 prepare，直接创建 queued 链接任务。后端也未检查 accepted；席位直接 int 转换、续费直接 bool 转换，卡字段只检查非空，缺少服务端类型及完整性校验。 | [Customer.vue:67](D:/aixiangmu/反代/xufu-python/frontend/src/Customer.vue:67)、[subscription.py:65](D:/aixiangmu/反代/xufu-python/backend/subscription.py:65)、[subscription.py:80](D:/aixiangmu/反代/xufu-python/backend/subscription.py:80)、[subscription.py:123](D:/aixiangmu/反代/xufu-python/backend/subscription.py:123)。 |
| R08 | 幂等与并发受理未完整实现。相同幂等键直接返回旧单，不比较 request_hash；余额判断来自事务外读取的用户对象，没有锁定用户后重查，订单上限计数也没有同账号并发互斥。唯一键可以挡重复插入，但异常未转换成稳定的幂等响应。 | [subscription.py:104](D:/aixiangmu/反代/xufu-python/backend/subscription.py:104)、[subscription.py:107](D:/aixiangmu/反代/xufu-python/backend/subscription.py:107)、[subscription.py:112](D:/aixiangmu/反代/xufu-python/backend/subscription.py:112)。 |
| R09 | 冻结、解冻流水余额仍错误。先更新 users.frozen，再写流水时又加/减一次 fee。例如原冻结 0、服务费 1000，用户冻结变成 1000，但 freeze 流水记录为 2000；失败解冻还会使其他订单冻结额在流水中少记。成功扣费流水已修，不属于此残留问题。 | [subscription.py:129](D:/aixiangmu/反代/xufu-python/backend/subscription.py:129)、[subscription.py:184](D:/aixiangmu/反代/xufu-python/backend/subscription.py:184)。 |
| R10 | 长链生成结果没有完整验收。生成器可以返回 ok=true、payable=false，但调用方只看 ok。独立提链完成后保留 awaiting_payment，而下单上限把它继续计作未完成订单；缺少明确完成/过期处理。 | [generator.py:893](D:/aixiangmu/反代/xufu-python/generator.py:893)、[subscription.py:276](D:/aixiangmu/反代/xufu-python/backend/subscription.py:276)、[subscription.py:287](D:/aixiangmu/反代/xufu-python/backend/subscription.py:287)、[subscription.py:112](D:/aixiangmu/反代/xufu-python/backend/subscription.py:112)。 |
| R11 | 付款结果未知、官方验证和明确失败没有可靠区分。固定轮询后未成功就抛失败；外层统一标记 failed 并解冻。没有持久化付款提交状态及只核查原交易的恢复流程；终态更新也缺少原状态条件，后续错误可能覆盖已成功状态。 | [subscription.py:418](D:/aixiangmu/反代/xufu-python/backend/subscription.py:418)、[subscription.py:440](D:/aixiangmu/反代/xufu-python/backend/subscription.py:440)、[subscription.py:526](D:/aixiangmu/反代/xufu-python/backend/subscription.py:526)、[subscription.py:182](D:/aixiangmu/反代/xufu-python/backend/subscription.py:182)。 |
| R12 | 后台任务没有完整的恢复及调度契约。领取后直接 processing；没有任务租约/超时恢复，进程中断后会停在那里。scheduler 的暂停、队列上限、任务超时没有在订阅调度中生效。 | [subscription.py:151](D:/aixiangmu/反代/xufu-python/backend/subscription.py:151)、[main.py:35](D:/aixiangmu/反代/xufu-python/backend/main.py:35)、[schema.sql:134](D:/aixiangmu/反代/xufu-python/backend/schema.sql:134)。 |
| R13 | 生效/续费只有局部轮询。只判 is_active/will_renew，没有核对购买的目标套餐和席位；次数与间隔写死，没有采用后台配置。取消请求最多重发两次，缺少持久化防重复标记；核查遇 ProviderError 时 break 会跳过 for-else 的 review 更新，可能留下 pending。轮询完未生效也没有后续领取处理。 | [subscription.py:459](D:/aixiangmu/反代/xufu-python/backend/subscription.py:459)、[subscription.py:479](D:/aixiangmu/反代/xufu-python/backend/subscription.py:479)、[subscription.py:492](D:/aixiangmu/反代/xufu-python/backend/subscription.py:492)、[renewals.py:159](D:/aixiangmu/反代/xufu-python/backend/renewals.py:159)。 |
| R14 | 前后端详情仍不匹配。客户/管理员 checkout 只给 url 和会话编号，没有 ready、expires_at；CheckoutResult 只有 ready 时才展示。管理员账单返回 state_code，组件读 region；没有 profile_id。列表 payment 仍固定空对象；PaymentTrace 读 detail.trace，后端只提供普通 events。流程名称映射也未加入 gener_direct_pay。 | [records.py:38](D:/aixiangmu/反代/xufu-python/backend/records.py:38)、[records.py:88](D:/aixiangmu/反代/xufu-python/backend/records.py:88)、[CheckoutResult.vue:6](D:/aixiangmu/反代/xufu-python/frontend/src/components/CheckoutResult.vue:6)、[OrderBilling.vue:14](D:/aixiangmu/反代/xufu-python/frontend/src/components/OrderBilling.vue:14)、[Admin.vue:65](D:/aixiangmu/反代/xufu-python/frontend/src/Admin.vue:65)、[paymentJourney.ts:18](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:18)。 |
| R15 | 部分前端操作/API 仍无处理器。缺少 POST /api/v1/orders、网页/API 撤回、管理员人工关闭和付款核查；PaymentPanel 的准备、确认、查询入口也未在当前后端注册。是否启用这些操作须与最终流程范围一致，不能把 UI 存在算为接口已实现。 | 现有创建入口只有 [subscription.py:64](D:/aixiangmu/反代/xufu-python/backend/subscription.py:64)；调用方 [Customer.vue:66](D:/aixiangmu/反代/xufu-python/frontend/src/Customer.vue:66)、[PaymentPanel.vue:10](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:10)、[PaymentPanel.vue:18](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:18)。AST 清点确认上述路由缺失。 |
| R16 | 敏感数据生命周期与脱敏未完成。subscription_runs 的卡片和授权数据在结束/过期后没有清理；maintenance 只清 renewal_tasks 的过期授权。generator 日志包含令牌片段和完整链接，付款错误包含原始响应片段，再被写入日志或订单事件。 | [main.py:27](D:/aixiangmu/反代/xufu-python/backend/main.py:27)、[subscription.py:273](D:/aixiangmu/反代/xufu-python/backend/subscription.py:273)、[subscription.py:526](D:/aixiangmu/反代/xufu-python/backend/subscription.py:526)、[generator.py:687](D:/aixiangmu/反代/xufu-python/generator.py:687)、[stripe_hosted.py:160](D:/aixiangmu/反代/xufu-python/backend/prolong/stripe_hosted.py:160)。 |
| R17 | 测试与文档尚未跟上。test_13 仍断言 /api/orders 和 subscription_runs 不存在，已与当前源码冲突；tests 中没有对 run_subscription 的流程测试。最近 3 组通过记录覆盖配置、报价和读取接口，不能证明下单到完成正确。AGENTS/旧说明中仍保留过时的未修复清单。 | [test_backend.py:267](D:/aixiangmu/反代/xufu-python/tests/test_backend.py:267)、[test_backend.py:325](D:/aixiangmu/反代/xufu-python/tests/test_backend.py:325)、[AGENTS.md:15](D:/aixiangmu/反代/xufu-python/AGENTS.md:15)。 |
| R18 | 支付实现仍使用伪造浏览器设备和行为信号，尚未替换为官方收银台或 SDK 的真实验证流程。该项是继续启用此执行路径的边界，不应通过改前端状态掩盖。 | [fingerprint.py:58](D:/aixiangmu/反代/xufu-python/backend/prolong/fingerprint.py:58)、[fingerprint.py:114](D:/aixiangmu/反代/xufu-python/backend/prolong/fingerprint.py:114)，调用处 [subscription.py:302](D:/aixiangmu/反代/xufu-python/backend/subscription.py:302)。 |

本次检查对应源码 SHA-256：

```text
backend/subscription.py  db00ef9692263abde6a29d6ab7c687b177911fd731c6ebaf631256e32c7543b8
backend/records.py       922e544a31c6171d49b95e8c885976dbd15e1bd79b49398ab25b2b51eb6534b9
```

后续若这些文件再次修改，应按新内容重新判断；本次没有修改执行代码、重启服务或验证真实付款。
