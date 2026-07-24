# DeepSeek 独立控制系统

## 1. 定位

DeepSeek Steward 是 `a15280020511/test2` 的独立、最高优先级技术控制层。

它对网页 GPT 提供三个独立模式：

1. `ASSIST`：所有新任务的第一入口；
2. `REVIEW`：所有正式结果的最终发布闸门；
3. `REPAIR`：所有技术故障的诊断和受控维修入口。

网页 GPT 继续负责用户意图、公开证据收集以及与用户沟通。DeepSeek负责仓库使用指导、预算建议、插件建议、运行审计、故障诊断、兼容性管理和最小维修。

DeepSeek只使用官方 API：`https://api.deepseek.com`。官方DeepSeek不可用时必须真实停止，不得切换到OpenRouter或其他模型冒充DeepSeek。

## 2. 最高优先级入口

任何需要专家团、OpenRouter推理或付费模型的新任务，必须先运行 `ASSIST`：

```text
用户
→ 网页 GPT
→ DeepSeek ASSIST
→ READY / STOP
→ 三档预算建议与插件建议
→ 网页 GPT 向用户展示预算
→ 用户明确选择
→ Execution Plan v2
→ 临时插入工具
→ 执行
→ DeepSeek REVIEW
→ APPROVE 后发布
```

网页 GPT 不得跳过 DeepSeek入口直接提交付费专家团。

`ASSIST` 必须返回：

- 当前任务是否 `READY`；
- 需要哪些临时插件；
- 经济、均衡、质量三档预算；
- 每档预计费用区间、最高费用、模型调用次数、单次输出token限制；
- 需要向用户提出的预算选择问题；
- 专家数量、角色、阶段、红队、裁决和模型建议。

DeepSeek不得声称用户已经批准预算。批准只能来自网页 GPT 与用户的真实对话。

## 3. 预算控制

正式 `Execution Plan v2` 必须记录：

- DeepSeek ASSIST 的 `operation_id`；
- DeepSeek 已向网页 GPT 返回预算方案；
- 网页 GPT 已向用户展示预算；
- 用户选择的档位或自定义最高金额；
- 预计费用上下限；
- 用户批准的最高费用；
- 最大模型调用次数；
- 每次调用最大输出token；
- 用户批准的简短审计引用。

缺少任一项时，执行器必须在任何付费模型调用前停止。

预计费用上限不得超过用户批准的最高费用；计划调用次数不得超过批准次数；Agent Framework每次运行必须使用批准的 `max_tokens`。

费用仍是执行前估算，不是OpenRouter最终账单保证。网页 GPT 必须向用户明确说明这一点。

## 4. 临时插件原则

永久控制内核只使用Python标准库和GitHub原生能力。

第三方能力放在 `plugins/`：

```text
plugins/
└── expert-team/
    ├── plugin.json
    └── requirements.txt
```

专家团插件包含：

- Microsoft Agent Framework Core；
- Microsoft Agent Framework OpenAI provider；
- OpenRouter SDK模型情报能力。

插件生命周期：

```text
任务需要插件
→ 在 Runner 临时目录创建 venv
→ 安装插件 requirements
→ 运行允许的模块和操作
→ 写入日志与生命周期证据
→ 无论成功、失败或超时都删除 venv
→ GitHub Hosted Runner 结束后再次整体销毁
```

不用时，插件依赖不存在于DeepSeek控制环境中。

DeepSeek只维修本地插件清单、薄适配器、工作流和兼容边界；不维护、fork或重写上游工具包。

## 5. REVIEW模式

专家团完成后必须自动调用 `REVIEW`。

DeepSeek检查：

- 任务是否完成；
- 事实、假设和推断是否分开；
- 证据是否充分；
- 结果是否自相矛盾；
- 是否完成计划中的全部阶段；
- 红队和裁决是否按要求执行；
- 是否遵守用户预算；
- 是否把程序成功误认为现实结论正确；
- 是否适合向用户发布。

返回值：

```text
APPROVE
REPLAN
COLLECT
STOP
```

只有 `APPROVE` 允许网页 GPT 发布。GitHub Workflow 的 `success` 只代表技术执行完成，不代表报告已经通过质量审核。

## 6. REPAIR模式

任何技术异常先交给DeepSeek：

- Workflow、Run、Job、Step、Log异常；
- checkout、Python或依赖安装失败；
- 插件安装、导入、接口兼容和适配器错误；
- OpenRouter或Agent Framework集成故障；
- DeepSeek官方API集成故障；
- GPT Action/OpenAPI错误；
- 状态、日志、Artifact和结果发布错误；
- 任务重复、取消、强制取消和恢复异常；
- 控制面和工作仓库跨仓库错误。

DeepSeek必须区分：

1. 仓库自身缺陷；
2. 临时外部故障；
3. 上游工具包缺陷；
4. 用户输入或预算未批准；
5. GitHub或DeepSeek平台级不可用。

只有第一类可以自动产生最小代码修改。第二至第五类不得伪造仓库修复。

维修必须：

- 使用完整文件替换；
- 不修改 `tests/`、`.git/`、`artifacts/`、`runtime_results/` 和Secrets；
- 不删除CI闸门；
- 不强推主分支；
- 先验证，再创建修复分支和PR；
- 一个故障最多一次维修和一次恢复，不建立无限循环。

## 7. 独立救援

正常控制：`.github/workflows/deepseek-control.yml`

独立救援：`.github/workflows/deepseek-rescue.yml`

救援Workflow不checkout仓库、不依赖正常控制脚本，可以在普通控制面异常时：

- 查询目标Run；
- 普通取消；
- 强制取消；
- 重跑失败任务；
- 收集故障证据；
- 直接调用官方DeepSeek进行诊断。

如果GitHub Actions整体不可用、控制令牌失效或官方DeepSeek不可达，任何仓库内系统都无法越过这一物理边界，必须真实报告 `STOP`。

## 8. 对内与对外

### 对内管理

- 任务身份和去重；
- 插件生命周期；
- 预算闸门；
- Run全程监控；
- 取消和强制关闭；
- 结果审核；
- 故障诊断；
- 受控修复；
- 日志、Artifact和审计证据。

### 对外辅助网页 GPT

- 判断任务是否适合使用专家团；
- 给出最小必要插件；
- 给出三档预算和取舍；
- 指导网页 GPT 搜证和填写Execution Plan；
- 提醒网页 GPT 向用户确认预算；
- 在结果不合格时明确要求补证、重规划或停止。

## 9. 核心原则

> DeepSeek先接触、预算先批准、工具临时插入、执行全程可审计、结果必须复核、故障统一交给DeepSeek、上游维护归上游。
