# gener_direct_pay 订阅全程复核（2026-11-03 02:47）

> 历史版本。08:59 的源码已修改代理和账单处理；最新监控及全程状态检查见 [09:01 复核报告](D:/aixiangmu/反代/xufu-python/docs/gener-monitor-review-20261103-0901.md)。以下旧行号和部分未完成结论不再代表当前版本。

范围：订阅办理页面点击提交，到付款结果、订阅生效、续费设置、服务费结算和页面最终结果。依据 02:35:50 修改的 subscription.py 及本次读取到的其它文件；本报告替代 02:22 清单的现状结论。仅做源码、AST 路由、只读数据库及健康接口检查，没有修改业务代码、重启服务或发起真实付款。

## 本次已补上的代码

以下表示源码已有改动，不表示完整流程已通过测试或当前服务已加载。

| 已有进展 | 代码位置 | 尚需注意 |
| --- | --- | --- |
| 订阅下单要求 accepted=true，补充卡字段基础校验；席位转换错误转业务错误 | [subscription.py:169](D:/aixiangmu/反代/xufu-python/backend/subscription.py:169)、[subscription.py:195](D:/aixiangmu/反代/xufu-python/backend/subscription.py:195) | 严格类型、有效期等仍不完整，见 R06。 |
| 幂等查询移到余额检查之前，开始比较 request_hash | [subscription.py:210](D:/aixiangmu/反代/xufu-python/backend/subscription.py:210) | 摘要覆盖范围及并发重放仍不完整，见 R05。 |
| 余额检查增加用户行锁，订阅订单上限在该锁持有期间查询 | [subscription.py:220](D:/aixiangmu/反代/xufu-python/backend/subscription.py:220) | 不能再说订阅余额检查完全没有锁；同键并发查询在此锁之前。 |
| 没有可用账单时拒绝下单 | [subscription.py:228](D:/aixiangmu/反代/xufu-python/backend/subscription.py:228) | 已修复空账单直接继续，但完整快照及消费字段未齐，见 R08。 |
| 冻结和解冻流水直接使用更新后的 frozen | [subscription.py:243](D:/aixiangmu/反代/xufu-python/backend/subscription.py:243)、[subscription.py:302](D:/aixiangmu/反代/xufu-python/backend/subscription.py:302) | 上次所述“流水又加/减一次 fee”已修；终态保护仍未完成。 |
| 领取任务检查 scheduler.paused | [subscription.py:266](D:/aixiangmu/反代/xufu-python/backend/subscription.py:266) | 尚未检查 payment.enabled；暂停不能取消已领取任务。 |
| 拦截 payable=false，记录生成器调整后的国家及币种事件 | [subscription.py:379](D:/aixiangmu/反代/xufu-python/backend/subscription.py:379) | 固定长链要求仍满足；不能再说完全不读 payable 或实际国家。 |
| 新增付款结果分类，并尝试读取 amount_received | [subscription.py:491](D:/aixiangmu/反代/xufu-python/backend/subscription.py:491) | 未知结果的最终落库和无实收证据时的回填仍有问题，见 R01/R03。 |
| 续费核查异常后会落 review | [subscription.py:668](D:/aixiangmu/反代/xufu-python/backend/subscription.py:668) | 上次 break 跳过 for-else、留下 pending 的特定问题已修；持续核查及最终状态仍未完整实现。 |

## 尚未完成或仍有问题

| 编号 | 问题及具体影响 | 当前代码证据 |
| --- | --- | --- |
| R01 | **付款结果未知仍按失败解冻。** 新名称 payment_result_unconfirmed 只是异常文本，随后被外层通用异常捕获，调用 _fail_order 将订单改 failed 并释放服务费；没有进入待核实状态。提交后断网也走同类路径。 | [未知结果:591](D:/aixiangmu/反代/xufu-python/backend/subscription.py:591) → [通用异常:705](D:/aixiangmu/反代/xufu-python/backend/subscription.py:705) → [失败落库:298](D:/aixiangmu/反代/xufu-python/backend/subscription.py:298)。 |
| R02 | **终态与资金收尾缺状态保护。** _succeed_order 和 _fail_order 均仅按 id 更新，不限制原状态。成功事务结束后的事件写入若异常，仍可能进入外层失败处理。charge/unfreeze 的唯一 reference 分别不同，不能阻止成功后又走失败解冻；仍可能影响同用户其它订单的冻结额。 | [失败更新:300](D:/aixiangmu/反代/xufu-python/backend/subscription.py:300)、[成功更新:314](D:/aixiangmu/反代/xufu-python/backend/subscription.py:314)、[成功后的事件:599](D:/aixiangmu/反代/xufu-python/backend/subscription.py:599)、[流水唯一键](D:/aixiangmu/反代/xufu-python/backend/schema.sql:29)。 |
| R03 | **缺少实收证据仍填预计金额。** 已优先读取 amount_received，但取不到或为 0 时又使用 expected_amount，接口和页面继续叫 charged_amount/“已核实实收”。币种也仍取付款前的响应，没有独立保存最终支付金额及币种凭据。 | [金额提取:508](D:/aixiangmu/反代/xufu-python/backend/subscription.py:508)、[回填及保存:594](D:/aixiangmu/反代/xufu-python/backend/subscription.py:594)、[对外返回](D:/aixiangmu/反代/xufu-python/backend/records.py:43)、[实收展示](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:29)。 |
| R04 | **生效核查可能误报成功。** 套餐不匹配只记录警告，仍标 active；没有核实席位，也没有区分 Pro 5x 和其它 Pro。新分支直接 request 而非 status，未保留 status 内的账号一致性及布尔类型校验，字符串 false 也可能被 bool 当成真。 | [直接读取及类型转换:628](D:/aixiangmu/反代/xufu-python/backend/subscription.py:628)、[不匹配仍继续:633](D:/aixiangmu/反代/xufu-python/backend/subscription.py:633)、[标生效:650](D:/aixiangmu/反代/xufu-python/backend/subscription.py:650)；对照 [renewals.py:165](D:/aixiangmu/反代/xufu-python/backend/renewals.py:165)。 |
| R05 | **幂等仍只覆盖部分情况。** request_hash 仅含 workflow/product_id/country/seats，不包含账号、premium_seats、workspace_name 和续费意愿；相同键变更这些输入仍会返回旧单。同键并发时，两次旧单查询可都发生在行锁之前，锁后不重查、也不捕获 UniqueViolation 为重放响应。在多实例或并发执行场景，可能返回余额错误或唯一键 500。 | [摘要及旧单查询:210](D:/aixiangmu/反代/xufu-python/backend/subscription.py:210)、[后取得用户锁:222](D:/aixiangmu/反代/xufu-python/backend/subscription.py:222)、[插入:235](D:/aixiangmu/反代/xufu-python/backend/subscription.py:235)。当前单进程不代表这些数据库边界已获得保证。 |
| R06 | **部分输入仍不是严格校验。** keep_renewal=bool(v) 会把字符串 false 当成开启；int 可截断小数并接受布尔值。卡年份只检查数字，没有长度或到期校验。 | [席位:177](D:/aixiangmu/反代/xufu-python/backend/subscription.py:177)、[续费意愿:185](D:/aixiangmu/反代/xufu-python/backend/subscription.py:185)、[卡年份:201](D:/aixiangmu/反代/xufu-python/backend/subscription.py:201)。 |
| R07 | **同单实际出口一致性未验证。** 代理会话 URL 已保存、生成器有初始检测；但 expected_ip 没有读写，没有把初始出口作为后续基准。租约固定 600 秒，复用会话直接返回 URL，没有重获容量租约，也没有完成即释放的关联记录。 | [复用:100](D:/aixiangmu/反代/xufu-python/backend/subscription.py:100)、[租约:127](D:/aixiangmu/反代/xufu-python/backend/subscription.py:127)、[600 秒:351](D:/aixiangmu/反代/xufu-python/backend/subscription.py:351)、[expected_ip 列](D:/aixiangmu/反代/xufu-python/backend/schema.sql:138)、[初始检测](D:/aixiangmu/反代/xufu-python/generator.py:729)。 |
| R08 | **随机账单快照和实际消费不一致。** 无资料拒绝已经补上，但国家实际在 billing 配置组，快照却从单条资料读取 country；未保存 profile_id。实际邮箱仍取上游/旧默认邮箱；地址和姓名仍有旧默认值兜底。 | [快照:231](D:/aixiangmu/反代/xufu-python/backend/subscription.py:231)、[资料定义](D:/aixiangmu/反代/xufu-python/backend/settings.py:50)、[消费邮箱和地址:419](D:/aixiangmu/反代/xufu-python/backend/subscription.py:419)、[默认邮箱](D:/aixiangmu/反代/xufu-python/backend/prolong/stripe_hosted.py:205)。 |
| R09 | **生效/续费尚无持久化收尾。** 轮询次数和间隔固定；未生效写 pending 后不再调度。取消续费可重发，却没有持久化“已提交”标记。核查关闭时忽略 active；保留续费分支不验证 renew 就写 kept。已修复一次核查异常落 review，但没有自动恢复或完整人工处置。 | [固定生效轮询:624](D:/aixiangmu/反代/xufu-python/backend/subscription.py:624)、[取消提交:656](D:/aixiangmu/反代/xufu-python/backend/subscription.py:656)、[关闭核查:673](D:/aixiangmu/反代/xufu-python/backend/subscription.py:673)、[保留/等待:688](D:/aixiangmu/反代/xufu-python/backend/subscription.py:688)；对照独立续费模块已有 [cancel_started](D:/aixiangmu/反代/xufu-python/backend/renewals.py:201)，订阅模块未采用。 |
| R10 | **重启恢复和停用保护仍未完成。** 已检查 scheduler.paused，但领取任务未检查 payment.enabled；执行器未做阶段停用检查。queued 直接变 processing，缺任务租约、持久化阶段、付款提交检查点及原交易恢复。进程中断可留下 processing 并占满并发数。 | [领取:266](D:/aixiangmu/反代/xufu-python/backend/subscription.py:266)、[改 processing:288](D:/aixiangmu/反代/xufu-python/backend/subscription.py:288)、[执行器:334](D:/aixiangmu/反代/xufu-python/backend/subscription.py:334)、[调度](D:/aixiangmu/反代/xufu-python/backend/main.py:35)、[运行表](D:/aixiangmu/反代/xufu-python/backend/schema.sql:143)。 |
| R11 | **付款核查和人工关闭接口缺失。** 页面调用订单付款核查/管理员关闭，但 AST 路由清点未找到处理器；不能依靠现有页面解决异常单。 | [关闭调用](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:10)、[核查调用](D:/aixiangmu/反代/xufu-python/frontend/src/components/PaymentPanel.vue:20)。后台相关入口目前只有创建、读取和管理员备注。 |
| R12 | **用户可能提前看到成功，随后停止刷新。** 成功付款落库 status=succeeded，activation/renewal=pending。标签只在 status=processing 时特殊处理 activation=pending；刷新条件未包含 succeeded+pending。 | [成功写入:314](D:/aixiangmu/反代/xufu-python/backend/subscription.py:314)、[标签](D:/aixiangmu/反代/xufu-python/frontend/src/lib.ts:18)、[刷新](D:/aixiangmu/反代/xufu-python/frontend/src/Customer.vue:79)。 |
| R13 | **订单和页面字段仍未对齐。** 没保存本单 subscription_flow；名称映射缺 gener。列表 payment 固定空对象，详情缺 fee_state、进行中/待核实状态。管理员账单返回 state_code，组件读 region，缺 profile_id；PaymentTrace 读取 trace，而接口没有输出该结构。 | [订单写入:235](D:/aixiangmu/反代/xufu-python/backend/subscription.py:235)、[列表](D:/aixiangmu/反代/xufu-python/backend/records.py:30)、[详情](D:/aixiangmu/反代/xufu-python/backend/records.py:38)、[管理员字段](D:/aixiangmu/反代/xufu-python/backend/records.py:88)、[名称映射](D:/aixiangmu/反代/xufu-python/frontend/src/paymentJourney.ts:18)、[管理员组件](D:/aixiangmu/反代/xufu-python/frontend/src/Admin.vue:65)。 |
| R14 | **敏感数据清理和日志脱敏未完成。** card_enc/credentials_enc 没有完成或到期清理；maintenance 仅清独立续费任务授权。generator 会记录令牌前缀和原始 HTTP 代理 URL，后者可能含完整代理凭据。 | [保存:250](D:/aixiangmu/反代/xufu-python/backend/subscription.py:250)、[维护](D:/aixiangmu/反代/xufu-python/backend/main.py:27)、[日志转发:376](D:/aixiangmu/反代/xufu-python/backend/subscription.py:376)、[令牌日志](D:/aixiangmu/反代/xufu-python/generator.py:687)、[代理日志](D:/aixiangmu/反代/xufu-python/generator.py:707)。 |
| R15 | **全程测试及运行环境未跟上。** tests 没有 run_subscription 的全流程测试，test_13 仍断言创建路由和 subscription_runs 不存在。当前进程未加载最新代码，新代理会话表尚未存在。 | [旧测试](D:/aixiangmu/反代/xufu-python/tests/test_backend.py:267)、[无热重载启动](D:/aixiangmu/反代/xufu-python/scripts/serve.py:12)、[新表定义](D:/aixiangmu/反代/xufu-python/backend/schema.sql:134)。 |

## 关键代码摘录

R01：结果未知仍进入通用失败处理：

```python
# subscription.py:591
if not paid:
    raise RuntimeError('payment_result_unconfirmed')

# subscription.py:705
except Exception as e:
    log.error('[%s] subscription error: %s', order_id, e, exc_info=True)
    _fail_order(order_id, user_id, fee, f'办理异常：{str(e)[:120]}')
```

R03：没有实际金额时填入预计金额：

```python
# subscription.py:595
actual_charged = charged_minor if charged_minor > 0 else expected_amount
```

R04：套餐不匹配仍继续标生效：

```python
# subscription.py:633
if active and plan_type and expected_plan not in plan_type and plan_type not in expected_plan:
    log.warning('[%s] activation plan mismatch: expected %s got %s', order_id, expected_plan, plan_type)
    # Still mark active — plan type strings vary, don't block on mismatch
```

R05：请求摘要仅包含四项，且查询原单发生在用户行锁之前：

```python
# subscription.py:211
rh = digest(json.dumps([workflow, product_id, country, seats], ensure_ascii=False))
```

这些片段用于解释现有问题，不是可直接应用的修复补丁。

## 当前运行与验证边界

02:47:04（Asia/Shanghai）只读查询：保存流程仍为 cslive，payment.enabled=true、scheduler.paused=false；公开接口 payment_connected=false、checkout_connected=false。orders/subscription_runs 都是 0；数据库没有 order_proxy_sessions。服务进程于 01:22:47 启动，未加载 02:35:50 的新源码。live/ready 均 HTTP 200，健康通过不等于上述功能已实现。

继续遵守用户先前确认“付款保持未启用”。当前 [付款调用](D:/aixiangmu/反代/xufu-python/backend/subscription.py:413) 仍进入生成设备/行为信号的实现，不能通过改可用标志将其视为已经完成安全接入。合规的官方付款及验证接入尚未确定；本轮没有启用、调试或执行该付款路径。

本次未新增或运行付款测试。已做静态代码及路由检查、只读数据库核对和健康接口检查；下一步功能验收仍需隔离数据及模拟上游覆盖重复提交、并发受理、结果未知、迟延生效、续费异常、各阶段中断、单次结算和最终展示。

源码 SHA-256：

```text
backend/subscription.py 42dc7c510dea4bfce2724b2412a14e4b357d8b3594ab64e809783191347c040e
backend/records.py 922e544a31c6171d49b95e8c885976dbd15e1bd79b49398ab25b2b51eb6534b9
backend/main.py d7d4c81dba7ea846eea2321a1c8d96f5041d65fdf5bba2b629ca16eeb96c3d0e
backend/schema.sql ead947a7e7fa437f3470352abbc99e6f32b145401ecac9546c3194834d9d2881
```
