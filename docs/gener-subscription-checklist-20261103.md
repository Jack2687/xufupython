# gener_direct_pay 订阅办理全程检查

> 历史快照：本文截至 02:22。02:35 源码已有新修改，冻结/解冻流水等旧问题已修；最新结论与行号见 [02:47 复核报告](D:/aixiangmu/反代/xufu-python/docs/gener-subscription-review-20261103-0247.md)。不要将下文历史问题直接当作当前状态。

本次范围按用户最新要求收窄为：用户进入订阅办理、填写资料并点击办理，直到付款结果、订阅生效、续费设置、服务费结算和页面结果均明确。仅检查 `gener_direct_pay`；另外两个流程、独立结账链接菜单及开发者 API 对等实现不在本清单范围内。

这是源码检查和验收清单，不是已完成修复的声明。本次只更新检查文档，没有修改业务代码、重启服务、提交订单或发起真实付款。继续遵守用户之前的选择：“先只修配置，付款保持未启用”。现有付款器仍生成设备及行为信号，不能以打开前端开关的方式启用该付款路径；接入官方收银台或 SDK 需要另行确定。

## 检查快照

2026-11-03 02:22:06（Asia/Shanghai）再次读取结果：

- 本地 `/health/live`、`/health/ready` 均为 HTTP 200。
- `/api/public` 的 `payment_connected`、`checkout_connected` 均为 false；数据库 payment.enabled 为 true，保存的流程仍为 `cslive`，因此页面选中 gener 不能视为配置已保存。
- orders 和 subscription_runs 均为 0；没有可据此证明付款或全程成功的实际订单。
- 源码包含 `order_proxy_sessions`，当前数据库尚无此表。
- 本地服务进程启动于 01:22:47；subscription.py 修改于 02:10:55。启动入口没有自动重载，后续源码修改尚未加载。
- 检查期间曾见到收尾引用未定义的 lease；02:10:55 版本已移除该引用，不再列为当前错误。租约释放和有效期覆盖仍需单独验收。
- 本次 subscription.py 的 SHA-256 与上次相同；records.py 仍为 01:57:43 版本。此前列出的主要缺口尚未因新提交而消除。scheduler.paused 当前为 false。

源码快照 SHA-256：

```text
backend/subscription.py 6533d2bcfe165360c35674d4cc192710ae0337eb6c4489bc8b054405823efc11
backend/records.py 922e544a31c6171d49b95e8c885976dbd15e1bd79b49398ab25b2b51eb6534b9
```

## 已有基础

| 项目 | 当前状态与代码 |
| --- | --- |
| 服务费报价 | 已有独立只读报价；此前针对性测试和页面验证通过。[membership.py](D:/aixiangmu/反代/xufu-python/backend/membership.py:51) |
| gener 全套餐长链 | 前端锁定、后端保存约束已经完成；执行调用也指定 long。[ConfigEditor.vue](D:/aixiangmu/反代/xufu-python/frontend/src/components/ConfigEditor.vue:13)、[settings.py](D:/aixiangmu/反代/xufu-python/backend/settings.py:91)、[subscription.py](D:/aixiangmu/反代/xufu-python/backend/subscription.py:343) |
| 下单风控配置 | 已改为读取 risk 配置组；不能再列为“仍读取错误配置组”。[subscription.py](D:/aixiangmu/反代/xufu-python/backend/subscription.py:161) |
| 同单代理端点 | 已有按国家选择、读取 vendor/ip_mode、加密保存端点及有效期、同订单复用端点的代码。尚未证明实际出口始终一致，也没有完成与原项目的行为等价测试。[subscription.py](D:/aixiangmu/反代/xufu-python/backend/subscription.py:93) |
| 随机资料与快照 | 已从启用且非测试资料中随机选择，并保存姓名和邮箱等字段；完整性和使用一致性仍有缺口。[subscription.py](D:/aixiangmu/反代/xufu-python/backend/subscription.py:141)、[快照](D:/aixiangmu/反代/xufu-python/backend/subscription.py:210) |
| 成功扣费流水 | 已记录扣费后的实际 balance/frozen；冻结和解冻流水仍需修正。[subscription.py](D:/aixiangmu/反代/xufu-python/backend/subscription.py:294) |
| 生效和续费核查 | 已有局部轮询和状态写入；不等于具备重启恢复或最终完成保障。[subscription.py](D:/aixiangmu/反代/xufu-python/backend/subscription.py:525) |

## 本条链路尚未闭合的环节

| 阶段 | 当前缺口及影响 | 对应代码 | 完成判据 |
| --- | --- | --- | --- |
| 1. 点击办理与服务端开关 | 页面入口关闭，后台已注册执行路由及线程。领取任务不检查 payment.enabled 或 scheduler.paused；当前保存流程、运行源码和数据库结构也未统一。 | [records.py](D:/aixiangmu/反代/xufu-python/backend/records.py:16)、[任务领取](D:/aixiangmu/反代/xufu-python/backend/subscription.py:242)、[后台调度](D:/aixiangmu/反代/xufu-python/backend/main.py:52)、[新表](D:/aixiangmu/反代/xufu-python/backend/schema.sql:134) | 停用时服务端拒绝受理和执行；符合约定的付款实现确定后，再验证配置、数据库与运行版本一致。 |
| 2. 参数、幂等和并发受理 | 后端未检查 accepted；seats/premium_seats 直接 int 转换，keep_renewal 直接 bool 转换；卡字段只查非空。同一幂等键不比较请求内容，余额和未完成订单数量未在同账号锁内重查。 | [前端提交](D:/aixiangmu/反代/xufu-python/frontend/src/Customer.vue:73)、[参数](D:/aixiangmu/反代/xufu-python/backend/subscription.py:169)、[余额和幂等](D:/aixiangmu/反代/xufu-python/backend/subscription.py:195) | 无效输入稳定返回业务错误；重试返回原订单；不同请求复用键时拒绝；并发提交不能超额冻结或绕过订单上限。 |
| 3. 订单快照 | 已保存价格和部分账单，但没有保存本单 subscription_flow；请求摘要也不包含所有影响订阅的输入。 | [请求摘要](D:/aixiangmu/反代/xufu-python/backend/subscription.py:198)、[订单写入](D:/aixiangmu/反代/xufu-python/backend/subscription.py:213)、[运行表](D:/aixiangmu/反代/xufu-python/backend/schema.sql:143) | 后续修改全局配置不改变已受理订单的套餐、席位、续费意愿和流程；页面可准确解释本单使用的设置。 |
| 4. 代理与账单 | 保存相同代理 URL 不等于已验证相同出口。expected_ip 虽在表中存在，但订阅代码没有写入或读取，也没有调用出口检查。容量租约固定 600 秒，复用会话时不重获租约。账单无可用项时返回空对象；国家实际在 billing 配置组，却从资料行取值；无 profile_id；付款邮箱不使用快照邮箱，缺项会混用旧默认地址或随机姓名。 | [代理会话](D:/aixiangmu/反代/xufu-python/backend/subscription.py:93)、[600 秒租约调用](D:/aixiangmu/反代/xufu-python/backend/subscription.py:324)、[expected_ip](D:/aixiangmu/反代/xufu-python/backend/schema.sql:138)、[随机资料](D:/aixiangmu/反代/xufu-python/backend/subscription.py:141)、[资料格式](D:/aixiangmu/反代/xufu-python/backend/settings.py:50)、[实际消费](D:/aixiangmu/反代/xufu-python/backend/subscription.py:384) | 能证明实际请求采用约定的同单代理；会话过期和容量有明确处理。账单为用户获授权的完整资料快照，不因缺项静默混入其它来源。 |
| 5. 长链生成结果 | 调用固定 long 已符合要求；但调用方只检查 ok，而生成器允许 ok=true、payable=false。 | [调用方](D:/aixiangmu/反代/xufu-python/backend/subscription.py:352)、[生成器返回](D:/aixiangmu/反代/xufu-python/generator.py:893) | 提链成功、链接可用与付款成功分别记录；不可用或过期结果不能进入后续扣款环节。 |
| 6. 付款结果与原交易核查 | 固定轮询仍未成功即作为 payment_declined，外层统一失败并解冻；网络超时、额外验证、结果未知和明确失败没有可靠区分。缺少持久化提交检查点及原交易恢复；终态更新不检查旧状态，后续异常可能覆盖已成功状态。 | [结果判断](D:/aixiangmu/反代/xufu-python/backend/subscription.py:456)、[统一失败](D:/aixiangmu/反代/xufu-python/backend/subscription.py:516)、[异常处理](D:/aixiangmu/反代/xufu-python/backend/subscription.py:596)、[失败落库](D:/aixiangmu/反代/xufu-python/backend/subscription.py:271) | 通过官方付款方式处理验证；未知结果只核查原交易，不再次提交扣款；明确失败才按规则释放服务费；付款成功不会被后续核查错误改成付款失败。 |
| 7. 订阅生效与续费结果 | status 只返回 active/will_renew，未核对目标套餐及席位；生效轮询耗尽后没有再次调度。取消请求存在重发但没有持久化提交标记；续费核查遇 ProviderError 的 break 跳过 review 更新，可留下 pending。保留续费分支未依据实际 renew 值确认。 | [状态读取](D:/aixiangmu/反代/xufu-python/backend/renewals.py:159)、[生效轮询](D:/aixiangmu/反代/xufu-python/backend/subscription.py:538)、[取消及核查](D:/aixiangmu/反代/xufu-python/backend/subscription.py:555)、[保留及 pending](D:/aixiangmu/反代/xufu-python/backend/subscription.py:585) | “已付款”“目标订阅已生效”“续费设置已核实”分别有证据；迟延生效可继续核查；核查耗尽有明确待人工处理结果，不能无限 pending。 |
| 8. 服务费结算 | 冻结后写流水又加一次 fee；失败解冻后写流水又减一次 fee，流水 frozen_after 与真实余额不一致。成功和失败结算缺少按原状态约束，虽有流水唯一键，仍不能当作完整幂等状态机。 | [冻结](D:/aixiangmu/反代/xufu-python/backend/subscription.py:219)、[解冻](D:/aixiangmu/反代/xufu-python/backend/subscription.py:271)、[成功](D:/aixiangmu/反代/xufu-python/backend/subscription.py:284)、[流水唯一键](D:/aixiangmu/反代/xufu-python/backend/schema.sql:29) | 同一订单仅结算或解冻一次；用户冻结额与每条流水一致；核查异常不错误退款或再次扣费。 |
| 9. 重启、超时与最终处置 | 领取后直接改 processing，没有任务租约、心跳或中断恢复；调度暂停、超时等设置未接入本任务。原交易人工核查、关闭等页面操作也缺少对应服务端路由，不能作为异常兜底。 | [领取](D:/aixiangmu/反代/xufu-python/backend/subscription.py:242)、[调度](D:/aixiangmu/反代/xufu-python/backend/main.py:35)、[前端核查与关闭](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:9) | 重启后按原订单阶段继续查询，不重复付款；自动核查或明确人工处置最终结束任务，并保留原因和证据。 |
| 10. 用户看到的最终结果 | 后端付款详情只在 last4 存在时返回，列表 payment 固定空对象；缺 fee_state、运行中状态和本单流程。前端在 status=succeeded、activation=pending 时仍可显示“订阅成功”；详情轮询只认 queued/processing/review 或续费 review/retrying，会在付款成功但收尾未完成时停止。管理员账单字段、trace 与组件也未对齐。 | [客户详情](D:/aixiangmu/反代/xufu-python/backend/records.py:38)、[管理员详情](D:/aixiangmu/反代/xufu-python/backend/records.py:75)、[标签](D:/aixiangmu/反代/xufu-python/frontend/src/lib.ts:18)、[刷新条件](D:/aixiangmu/反代/xufu-python/frontend/src/Customer.vue:79)、[流程名称](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:18) | 页面能区分付款中、已付款待生效、续费待核实、完成、明确失败和需人工处理；没有假成功、永久转圈或结束前停止刷新。 |
| 11. 敏感资料收尾 | subscription_runs 的 card_enc 和 credentials_enc 没有完成/过期清理；maintenance 只清理 renewal_tasks 的过期授权。部分上游原始响应及链接进入日志或事件。 | [保存资料](D:/aixiangmu/反代/xufu-python/backend/subscription.py:224)、[日志及异常](D:/aixiangmu/反代/xufu-python/backend/subscription.py:349)、[异常事件](D:/aixiangmu/反代/xufu-python/backend/subscription.py:604)、[维护任务](D:/aixiangmu/反代/xufu-python/backend/main.py:27) | 卡安全码不在授权后保留；其它敏感资料按用途到期清理；页面和日志只保留脱敏的业务证据。 |

## 02:22 复核补充与具体代码证据

以下是对上表的补充；没有修改相应业务代码。

**重复请求可能被余额检查误拒。** [create_order](D:/aixiangmu/反代/xufu-python/backend/subscription.py:195) 先检查可用余额，随后才查同幂等键订单。首次请求冻结了全部可用余额但客户端未收到响应时，再提交相同请求会先遇到余额不足，不能稳定取得原订单。查到旧单后也没有比对 request_hash。

```python
if workflow == 'subscription':
    require(user['balance'] - user['frozen'] >= fee, '可用余额不足，请充值后再试')

rh = digest(json.dumps([workflow, product_id, country, seats], ensure_ascii=False))
old = q.one('SELECT id,status FROM orders WHERE user_id=$1 AND idempotency_key=$2', user['id'], idem_key)
if old:
    return {'id': old['id'], 'status': old['status'], 'replayed': True}
```

**冻结/解冻流水是重复计算，不只是显示文案。** [冻结代码](D:/aixiangmu/反代/xufu-python/backend/subscription.py:220) 更新后再次加 fee；[解冻代码](D:/aixiangmu/反代/xufu-python/backend/subscription.py:275) 更新后再次减 fee。关键 SQL 如下：

```sql
UPDATE users SET frozen=frozen+$2 WHERE id=$1;
-- 随后的 freeze 流水：
SELECT id,'freeze',0,balance,frozen+$2,... FROM users WHERE id=$1;

UPDATE users SET frozen=GREATEST(0,frozen-$2) WHERE id=$1;
-- 随后的 unfreeze 流水：
SELECT id,'unfreeze',0,balance,GREATEST(0,frozen-$2),... FROM users WHERE id=$1;
```

例如用户最初冻结 0、服务费 1000，更新后实际冻结为 1000，freeze 流水却记录 2000。SQL 示例中的省略号仅省略流水其它字段，不是可直接执行的修复语句。

**“已核实实收”尚无最终金额凭据。** [subscription.py:395](D:/aixiangmu/反代/xufu-python/backend/subscription.py:395) 从初始化/税费响应得到 expected_amount；[成功写入](D:/aixiangmu/反代/xufu-python/backend/subscription.py:522) 使用同一金额，没有从最终支付结果重新取实收金额：

```python
_succeed_order(order_id, user_id, fee, last4, expected_amount, currency_from_stripe)
```

而 [records.py:43](D:/aixiangmu/反代/xufu-python/backend/records.py:43) 将其输出为 charged_amount，[PaymentPanel.vue:29](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:29) 显示为“已核实实收”。金额预计值与实际支付凭据必须明确区分；目前不能将该展示当成已核实扣款金额。

**初始出口检测已经存在，整单一致性仍未成立。** [generator.py:729](D:/aixiangmu/反代/xufu-python/generator.py:729) 会做初始代理出口检测；因此不能表述为“整个流程从不检测 IP”。但订阅执行器没有保存返回 ipinfo 的 IP 基准，order_proxy_sessions.expected_ip 仍无读写，后续阶段没有据此比对。

此外 [generator.py:756](D:/aixiangmu/反代/xufu-python/generator.py:756) 会按实际出口重算国家/币种，订阅调用方 [subscription.py:352](D:/aixiangmu/反代/xufu-python/backend/subscription.py:352) 只取 URL 和会话编号，没有核对或保存返回 billingCountry/billingCurrency。生成器内部调整时，订单显示的请求地区可能与实际提链地区不一致，需纳入输入/结果一致性验收。

**日志脱敏还包括代理凭据。** [generator.py:127](D:/aixiangmu/反代/xufu-python/generator.py:127) 保留原始代理 URL，[generator.py:707](D:/aixiangmu/反代/xufu-python/generator.py:707) 在 HTTP 代理模式将该 URL 拼入日志。如果 URL 含用户名和密码，日志就可能包含完整凭据；[subscription.py:349](D:/aixiangmu/反代/xufu-python/backend/subscription.py:349) 将这些消息直接送入应用日志。本次仅检查代码，没有读取或输出真实凭据。

**“付款成功但尚未生效”会漏掉持续刷新。** [成功落库](D:/aixiangmu/反代/xufu-python/backend/subscription.py:287) 立即写 status=succeeded、activation=pending、renewal=pending；[renewalAttention](D:/aixiangmu/反代/xufu-python/frontend/src/lib.ts:18) 只接受续费 review/retrying；[Customer.vue:79](D:/aixiangmu/反代/xufu-python/frontend/src/Customer.vue:79) 因而不会继续刷新这一组合。这里需要按付款、生效、续费三个独立阶段判断是否结束。

**人工核查/关闭不是已实现的收尾。** AST 路由清点只找到 POST /api/orders、GET 订单/订阅详情和管理员 PATCH 备注；没有页面 [PaymentPanel.vue:10](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:10)、[PaymentPanel.vue:20](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:20) 调用的订单关闭及付款核查处理器。界面有调用不代表异常订单已经有可用处置通道。

## 验收边界

当前不能确认“用户点击办理到最终完成已经打通”。本次验证仅覆盖语法/AST 静态检查、实际源码、只读数据库状态和本地健康接口；没有执行订阅付款器。

后续离线验收需要覆盖：正常受理、重复点击、并发余额不足、参数错误、资源不可用、付款结果未知、验证待完成、明确付款失败、迟延生效、续费核查异常、各阶段重启恢复、结算只执行一次及前后端最终状态一致。以模拟上游和隔离测试库验证，不使用真实卡、真实订单或真实取消续费操作。

现有 [test_13](D:/aixiangmu/反代/xufu-python/tests/test_backend.py:267) 仍断言创建路由和 subscription_runs 不存在，与当前源码不符；tests 目录没有 run_subscription 的全程测试。之前配置和报价测试通过，不可代替上述验收。

本文替代旧广泛审计作为本轮范围清单。文件若再次修改，须按新版本复核对应行号和结论；不同时间的源码进展不能直接合并为“当前运行环境已完成”。
