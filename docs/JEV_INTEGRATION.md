# dispatch-guard × Jev 接入方案（v4：Han 定 judge=lux 单 tier）

基线：dispatch-guard **v0.1.10**（origin/main，评审修复 D1–D17/F17 + 二轮 B1–B10 已合回）× jev-kernel **v0.2.1**（origin/main，hardening PR #1 已 ff 合入本地）。
状态：**P0 + P1 已实施**（分支 `feat/jev-arbitration`，未合并 main）。P0：`backend/jev.py`（stdlib systemone 客户端 + `inherit:jev-kernel` 解析）+ `_draft_route_table` 草稿挂钩 + 审计子对象。P1：盲区语义仲裁——`_jev_blindspot` 助手挂在 `_guard_target` 的 `not blockable` 分支（`_no_owner_pass` 调用前），复用 `backend/jev.py`，只把达门控的 judge 选择交回现有 deny/warn 路径（见 §3 P1 落地）。P2（shell mention Noul）未做。本文件即实施方案原文。
模型信息源：jev-kernel 运行时配置 `~/.qwenpaw/plugin-data/jev-kernel/jev.json`（本地 judge 链，非 TypeSafe 云）。

**v3→v4 变更摘要（Han 决策：用 lux）**：① judge 收敛为 **lux 单 tier**（实测 121ms），clef/session 不接入——级联、48s 最坏延迟、session provider 暴露面全部作废；② 配置形态改为 `inherit:jev-kernel` + `tiers:["lux"]`（endpoint 单一事实源保留，tier 白名单锁 lux）；③ lux 失败即 L3 fail-open（P0 回落关键词扫描 / P1 走现行 no_owner），无下一级可级联；④ rubric 第 8 条改为「lux 宕机 fail-open」断言，死 tier 级联测试作废。

## 0. 结论

接。judge 用 jev-kernel 现成的本地 System One 链（lux → clef → session），不接 TypeSafe 云；数据不出本机信任域。
形态：opt-in 的 L2 语义仲裁层；L1 确定性规则永远第一；L3 fail-open 兜底不变（jev 调用位于 F17 wrapper 内，异常即 fail-open）。

## 1. judge 链事实（jev-kernel v0.2.1 运行时配置与代码）

| tier | type | baseUrl | model | timeoutMs |
|---|---|---|---|---|
| lux | systemone | `http://100.100.231.22:8090` | `StartLux-Decision-2B-Q8_0` | 3000 |
| clef | systemone | `http://192.168.1.132:11434`（Ollama ≥0.35 decision 模型） | `clef-flash:latest` | 30000 |
| session | llm（继承 QwenPaw 会话模型，OpenAI-compatible，`enable_thinking:false`） | ProviderManager 解析 | 会话 active_model | 15000 |

wire 协议（jev-kernel `backend/judges.py`，参照 mu-agent 0.1.8 验证）：
- `POST {baseUrl}/v1/systemone`；baseUrl 归一化：path ``/``→追加 `/v1/systemone`，`/v1`→追加 `/systemone`，其余原样。
- body `{"model","state","questions"}`；boolean 发出为 `type:"noul"`；choice criteria 为 `{label: description}` 映射。
- 应答翻译：`{"noul": p}`→`{"type":"boolean","probability":p}`；score per-level `probabilities`→argmax level + confidence。
- 级联：tiers 顺序，后级只看前级 uncertain 的问题；全链无解→fallback verdict（choice: value=None, p=0.0）+ 记原因。
- 阈值默认：`yes=0.85 / no=0.15 / minProbability=0.6 / risk=0.85`；choice uncertain = p < minProbability；noul uncertain = no < p < yes。
- v0.2.1 新增：全局 `enabled`（默认 true）kill-switch；`ledgerTail`（ledger 轮转）；approvalTools 增加 hook 宿主工具名（`Bash`/`shell`，与本方案无关）。
- v0.2.0 起 jev-kernel 亦以 PreToolUse hook 跑在 ZCode/Qoder/Claude Code/Codex —— judge 定义跨 harness 单一事实源的价值更高。
- `scripts/systemone_adapter.py`：gateway 方言→raw ollama 方言的 loopback 适配器（默认上游 127.0.0.1:11434）。本方案客户端直说 raw 方言（judges 类型即 systemone），**不需要**适配器；适配器仅作开发期联调工具登记。

### 1.1 实测（2026-10-13，本机直测）

- **lux 存活**：`POST /v1/systemone` 200，**121ms**，应答 `{"answers":{"q0":{"type":"noul","noul":0.675}}}` —— raw 方言（noul 键）与协议假设逐字吻合；121ms ≪ 2500ms 硬帽，D3 预算余量充足（硬帽保留 2500 不降，给冷启动尖峰留余量）。
- **clef 双地址皆死**：① 本机 Ollama = **v0.33.3 < 0.35**，无 `/v1/systemone` 端点 → 404（`clef-flash:latest` 模型已 pull 在服务端却serve不出协议）；② `192.168.1.132` 非本机（本机 LAN IP 为 192.168.1.2）且当前不可达（connect 失败）。hook 宿主配置 `~/.jev-kernel/jev.json` 的 clef 指向 127.0.0.1:11434，同样死。
- **当前有效链 = lux → session**（clef 层按级联语义记 error 跳过，设计已覆盖，无需改代码）。运维复活 clef 二选一：本机 Ollam a 升级 ≥0.35（模型已就绪，升级即活），或恢复 192.168.1.132 可达。此为 jev-kernel 运维项，不阻塞 dispatch-guard 接入。
- **.env 澄清**：全盘（磁盘 .env × 10、QwenPaw 宿主进程 env、hook 宿主目录）均无 jev/typesafe/ollama 相关键；「已部署模型」的信息实际住在两处 `jev.json`（plugin-data 与 ~/.jev-kernel），不在 .env。唯一相邻密钥是 wave-mcp 进程 env 的 OpenCode 键（与 jev-kernel 无关）；内置 `jev` tier 的 OpenCode Zen 免费回退本就不需要键。

## 2. 设计决策（D1–D6 沿用 v2，D7 新增）

**D1 · 不接云，judge=lux 单 tier（Han 定）。** `"judges": "inherit:jev-kernel"` + `"tiers": ["lux"]`：endpoint/model 从 jev-kernel jev.json 读（单一事实源，禁止复制第二处），tier 白名单锁 lux——clef（当前 404）与 session（provider 暴露面）不接入。inherit 解析先查 jev-kernel 全局 `enabled`，false 视同无 judge；jev.json 中 lux 规格缺失/改名→jev 层禁用+审计，不报错。

**D2 · 协议级复用，不做进程内 import。** dispatch-guard 内置 ~80 行 stdlib urllib systemone 客户端（raw 方言），镜像 jev-kernel `judges.py` 归一化/翻译 + 契约测试防漂移。理由不变：bare-Python 测试承诺（CI 3.10–3.12）与发布独立性。jev-kernel 缺失/禁用→jev 层禁用 + 审计记原因，不报错。

**D3 · 单 judge 延迟预算。** lux 实测 121ms；在线 P1 与离线 P0 同用硬帽 `online_timeout_ms=2500`（lux spec timeoutMs=3000 取其小），超时/连接失败即 L3，无级联等待。

**D4 · 置信度门控（choice）。** p ≥ 0.85 且 enforce 且 allow_deny → deny 点名；0.6 ≤ p < 0.85 → warn 点名；p < 0.6 / 选中 `basic_op`·`none` / fallback → 现行行为 + 审计。noul（P2）用 0.85/0.15。

**D5 · 默认关闭。** `jev.enabled=false` 时与 v0.1.10 逐字节一致（回归断言基线改为 v0.1.10）。README 隐私节：默认零网络；启用后仅本地 judge（inline 配云 judge 时披露规则生效）。

**D6 · 审计对齐 ledger。** `dispatch_guard.jsonl` 增 `jev` 子对象 `{judge, latencyMs, probability, value, fallback}`，字段名对齐 jev-kernel ledger。

**D7（v3 新增）· 元完整性优先。** D1 元完整性 gate（`_guard_table_meta_hit`）位于 fail-open wrapper 之上、路由逻辑之上；jev 调用必须在其**之下**（即 `_guard_target` 内）。对 routes.json/routes.draft.json 的写改永远先被 D1 拒绝，**零 judge 调用**——jev 不得成为元完整性 gate 的旁路，也不得为其付出延迟。

## 3. 插入点（v0.1.10 行号）与问题设计

**P0 · 草稿生成 `_draft_route_table:1044`（`_dispatch_policy:1015` 不变优先）— 离线，先做。**
每个 `_DRAFT_PRESETS` 类别一个 Choice 问，同请求并行（lux 单请求多问并行，121ms 量级）；state = `{"categories":[...], "agents":[{"id","skills","description"}]}`（ASCII 优先）；criteria = live agent id（description 截断 200 字符）+ `"unrouted"`；lux 失败/不确定→回落现行关键词扫描（现行逻辑是 fallback，不被替换）。

**P1 · 盲区仲裁 — 唯一入口：`_guard_target:1599` 的 `not blockable` 分支（`_no_owner_pass:1564` 调用前）。**
触发条件：`_route_hit:1686` 未命中 且 `_live_owner:1524` 返回 blockable=False（= inventory 非空可读、确无 owner）——即现行静默 no_owner 放行的盲点。
- Choice 问 criteria = live agent ids + `"basic_op"` + `"none"`；state = `{"target","tool","ext","agents":[...]}`；lux 单 judge、硬帽 2500ms。
- 解析出 owner（p 达门控）→ 交回现有 deny/warn 路径（owner 由构造保证 live，`_live_owner` 复核仍过）。
- **不挂** v0.1.10 新增第三分支（`owner is None and blockable` = inventory 读取失败）：无 inventory 即无 criteria 可言，保持「回报用户确认归属」语义。
- jev 调用位于 F17 fail-open wrapper 内：超时/异常/非 JSON → 直接走现行 `_no_owner_pass`，审计记 fallback（lux 单 tier，无级联）。

**P1 落地（分支已实施）**：助手 `_jev_blindspot(rel, tool, mode=, allow_deny=)` 在 `_guard_target` 的 `not blockable` 分支、`_no_owner_pass` 之前调用；发一个 Choice 问（criteria = live agent id（description 截断 200）+ `basic_op` + `none`，state = `{target,tool,ext,agents:[…]}`）。达门控的 owner 先经 `_live_owner(value)` 复核仍 blockable，才交回现有 deny/warn 文案（复用点名格式）。off／无 specialist／configured-but-unrunnable／超时／异常／非 JSON 一律退化为逐字一致的现行 no_owner。回归隔离靠**只增不改**：`_deny`/`_warn_and_pass`/`_no_owner_pass` 各加一个可选 `jev=` 关键字，仅在带仲裁时挂审计子对象，其它路径记录字节不变。第三分支（inventory 读取失败）不挂钩，L1 命中与 D1 元写零 judge 调用由结构保证。测试：`tests/test_jev_blindspot.py`（rubric 1–6/8/9，假 systemone 服务器）。

**P2 · shell mention Noul（DESIGN.md mention 层）— 后置可砍。** p ≥ 0.85 才把 mention 级警告升级为 explicit-target 级警告；永不因 noul deny。

## 4. 配置形态（routes.json 新增块；`_normalize_config` 对未知顶层键透传，jev 块按 D4 模式做类型降级：类型错→该字段视同缺省，不 crash）

```jsonc
"jev": {
  "enabled": false,                 // opt-in；false 时与 v0.1.10 行为一致
  "judges": "inherit:jev-kernel",   // endpoint/model 单一事实源；或 inline [{"name","type","baseUrl","model","timeoutMs"}]
  "tiers": ["lux"],                 // Han 决策：只用 lux；inherit 规格 + tier 白名单
  "thresholds": "inherit",          // 或 {"deny":0.85,"warn":0.6}
  "online_timeout_ms": 2500,        // lux 硬帽（与 spec timeoutMs 取小）
  "record": true
}
```

## 5. 验收 rubric（派单随消息带走）

1. 契约测试（stdlib `http.server` 假 systemone）：baseUrl 归一化 3 例、noul 翻译、score argmax、answers 有序 list 的 zip 映射、`_parse_error` 路径。
2. 延迟帽：假 judge sleep 3s > 2500ms → fail-open 走 `_no_owner_pass` + 审计 `fallback=timeout`，agent 不卡。
3. inherit 解析：jev-kernel 配置缺失→禁用+审计 `no_judge_configured`；jev-kernel `enabled:false`→同禁用；jev.json 无 lux 规格→禁用+审计；`tiers:["lux"]` 时 clef/session 规格存在也不得被调用（假服务器断言零请求）。两者零异常。
4. 门控矩阵：p=0.90→deny(enforce)/warn(warn 模式)；p=0.70→warn 点名；p=0.50→现行 no_owner；选中 basic_op/none→现行行为。
5. **D7 断言：jev enabled 时写 routes.json/routes.draft.json → D1 元完整性 deny，且假 judge 服务器收到 0 请求。**
6. 回归：`jev.enabled=false` 时 v0.1.10 全套件绿（含 `tests/test_review_regressions.py` 949 行评审回归）+ `ruff check .` + CI 3.10/3.11/3.12；enabled=false 全程零出站 socket 断言。
7. 隐私：enabled=true 且 inherit 本地链时仅 LAN/CGNAT 段连接（测试断言连接目标集合）。
8. lux 宕机（假服务器 connection refused / 404 / sleep 超帽）→ P0 回落关键词草稿+审计 `fallback`；P1 走现行 `_no_owner_pass`+审计；agent 不卡、零异常。

## 6. 排序与保留风险

P0 → P1 → P2。前置已满足（v0.1.10 在 main），Han 已定 judge=lux，批准后开 `feat/jev-arbitration` 派 Codex_Agent。
保留风险：lux 为 2B Q8 本地模型，CJK/复杂 manifest 校准未经实测——P0 先跑一个 session 的 shadow 三方对比（jev 草稿 vs 关键词草稿 vs 人工终稿）定标，P1 deny 阈值是否高于 0.85 由实测一致率决定，禁止照抄 cookbook 数值。clef 复活（Ollama ≥0.35）纯属 jev-kernel 运维项，与本方案解耦：dispatch-guard 只认 lux。
