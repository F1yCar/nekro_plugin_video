# nekro_plugin_video — 原生视频理解插件

## 定位

供 **Agent 自调用** 的视频分析工具。不注册任何指令、关键词或消息监听器；
工具返回分析结果给主 Agent，由主 Agent 组织并发送用户可见回复，插件自身不发消息。

只参考 `astrbot_zssm_explain` 的**视频链路**，其余功能（文本解释、网页/知乎/微信/PDF、
合并转发整体解释、群文件文本预览等）一律不移植。

- 上游来源: `https://github.com/exynos967/astrbot_zssm_explain`（参考实现，未逐行复制）
- 兼容目标: `KroMiose/nekro-agent`（上游）与 `Akiyo-dayo/NekroAgent_ByAkiyo` Devin 分支
- 旧的兼容层平移版保留在 `../nekro_plugin_zssm/`（历史参考，勿混淆）

## 入口契约

```
@plugin.mount_sandbox_method(SandboxMethodType.AGENT, name="视频分析")
async def analyze_video(_ctx: AgentCtx, source: str = "", message_id: str = "") -> str
```

- `source`：沙盒路径（`/app/uploads/`、`/app/shared/`）、HTTP/HTTPS 视频直链、B站链接或 BV/av 号
- `message_id`：当前会话中含视频的消息 ID（仅 `onebot_v11`），用于引用/转发消息回查
- 二者必须且只能提供一个；错误一律 `raise`，不返回错误字符串（plugin-rules 硬要求）

## 模块

| 文件 | 职责 |
|---|---|
| `plugin.py` | `NekroPlugin` 元数据 + `VideoConfig` 配置（无业务逻辑，避免循环导入） |
| `__init__.py` | 插件入口：挂载唯一 AGENT 方法 + cleanup |
| `analyzer.py` | 编排：获取 → 边界校验 → 抽帧/直传 → 逐帧描述 → 汇总 |
| `sources.py` | 来源解析：沙盒路径、URL、B站、OneBot 消息回查（含会话归属校验） |
| `media.py` | ffprobe 时长、ffmpeg 抽帧/音频提取/压缩，全部子进程带超时 |
| `network.py` | 受限下载：公网地址校验、DNS 解析白名单、手动重定向、大小/速率上限 |
| `llm.py` | 模型组解析、回退调用、ASR 转写、base64/dashscope 直传 |
| `host.py` | 双版本宿主适配：Akiyo 作用域/隔离/用量归属的条件接入 |

## 双版本兼容决策

- **Bot 获取**：一律 `_ctx.get_onebot_v11_bot()`。上游走全局 `get_bot()`，
  Akiyo 走 `get_bot_by_chat_key(chat_key)` 按实例路由——插件不做版本判断。
- **实例作用域（Akiyo 独有）**：`host.settings()` 动态探测
  `nekro_agent.services.plugin.scope`，存在时：
  - `resolve_active_plugins(instance_key, chat_key)` 校验本作用域可见性
  - `resolve_scoped_config(...)` 取实例级配置覆盖（沙盒 RPC 无实例 contextvar，
    不能用 `plugin.get_config()` 的隐式解析，必须显式传 `instance_key`）
  - `DBAdapterInstance.is_instance_active` + `isolation.get_quarantine` 双闸门
  - 上游无这些模块 → 走 `plugin.get_config()` 全局配置
- **用量归属（Akiyo 独有）**：`host.usage_scope()` 在每次模型调用外包裹
  `LLMUsageContext(scene=PLUGIN, chat_key=...)`，上游无此模块时为空上下文。
- **`channel_id` 解析**：仅 `re.search(r"group_(\d+)$")` 提取群号用于
  `get_group_file_url`；提取失败即拒绝，不猜测（Akiyo 文档声明 channel_id
  为不透明字段，两版实测均保持 `group_xxx`/`private_xxx` 形态）。
- **消息回查安全**：`get_msg` 结果校验 `group_id`/`user_id` 与 `ctx.channel_id`
  一致，防止 Agent 用伪造 message_id 回查其他会话的视频。

## 模型选择（对应原插件 video_provider_id / select_vision_provider）

候选顺序：`MODEL_GROUP`（留空 → 频道主模型组 `USE_MODEL_GROUP`）→ `FALLBACK_MODEL_GROUPS`
→ 自动兜底扫描所有 `MODEL_TYPE=chat` 且 `ENABLE_VISION` 的组（对齐原版
"回落到首个支持图片的 provider"）。`MODEL_OVERRIDE` 非空时覆盖所选组的
`CHAT_MODEL`（仅适用同端点换型号）。ASR 固定走 `ASR_MODEL_GROUP` 的
`audio/transcriptions`，不参与自动扫描。

## 分析流程（对齐原插件）

1. 落盘：沙盒路径复制 / URL 下载（≤ `MAX_SIZE_MB`，默认 50MB，限速 8MB/s）/
   B站 `playurl` 解析 / OneBot `get_file` 系列接口
2. `ffprobe` 测时长，> `MAX_DURATION`（默认 120s）拒绝；探测不到则按上限截断抽帧
3. `DIRECT_MODE`：`off`（默认抽帧）/ `base64`（≤10MB，超限先压缩）/ `dashscope`（≤100MB，file://）
4. 抽帧：时长已知 → 等距 `-ss` 逐点抽帧；未知 → `fps=1/N` 滤镜；GIF 只抽 1 帧；
   帧数 ≤ `MAX_FRAMES`（默认 20），长边 ≤ `MAX_FRAME_EDGE`
5. 可选 ASR：ffmpeg 抽 16k 单声道 WAV → 模型组的 OpenAI `audio/transcriptions`
6. 逐帧 caption（前 n-1 帧，单帧失败记"未识别"不中断）→ 元信息 + captions +
   ASR + 最后一帧 → 汇总 ≤100 字中文总结（提示词口径与原版一致）

## 安全边界

- 下载强制公网地址：拒绝内嵌凭据、localhost、非公网 IP；自建 resolver
  校验 DNS 结果，手动跟随重定向并逐跳复检（防 DNS rebinding / 跳板 SSRF）
- 拒绝 `text/html`、m3u8 等播放列表内容类型；响应禁压缩编码
- ffmpeg/ffprobe 限定 `-protocol_whitelist file,pipe`，所有子进程有超时
- OneBot file_id 校验格式，拒绝含路径分隔符的标识，防止以文件名伪装 file_id
- 临时目录 `na_video_*` 全程 `TemporaryDirectory` 托管，异常也清理

## 验证

```
.venv/bin/python -m pytest -q tests   # 77 passed
.venv/bin/python -m ruff check .      # All checks passed
```

覆盖：入口契约（AGENT 类型、无命令注册、docstring 不含 _ctx）、输入校验、
沙盒路径映射、大小/时长边界、真实 ffmpeg 抽帧与音频提取（imageio-ffmpeg）、
消息构造、模型组回退、Akiyo 作用域/隔离/用量分支、双版本源码契约扫描。

## 部署验证状态

已部署到本机 OrbStack 上游 NA 容器（`plugins/workdir/nekro_plugin_video`，
`PLUGIN_ENABLED` 已含 `F1yCar.nekro_plugin_video`），启动日志确认加载成功。
容器内以真实 `AgentCtx` 端到端调通并拿到真实模型返回（`api` 模型组 +
`gemini-3-flash-preview`）：沙盒路径解析 → 文件获取 → imageio-ffmpeg
动态安装与抽帧 → 消息构造 → 视觉模型调用成功返回中文总结；ASR 端点
404 时按设计降级为"仅依据画面总结"。

## 未验收项（部署前必读）

- Akiyo Devin 分支尚未实机加载验证（作用域/隔离/用量分支已按动态探测编写）
- 未接入真实 NapCat/QQ 收发；真实模型返回待配置 API_KEY 后回归
- B站 playurl 接口、`get_msg`/`get_file`/`get_group_file_url` 返回结构按公开
  OneBot v11/NapCat 约定编写，字段差异需真机回归
- dashscope 直传为可选依赖（`pip install nekro-plugin-video[dashscope]`），未实测
- ASR 复用所选模型组的 `audio/transcriptions` 端点，仅适用于 OpenAI 兼容转写服务
- Akiyo 用量归属以 `PLUGIN` scene + `chat_key` 记账，归属到频道/实例由写侧解析
- 合并转发内视频的递归展开限制在 3 层 / 64 节点预算内
