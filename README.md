# nekro_plugin_video · 视频理解

NekroAgent 原生视频分析插件：以 **Agent 自调用沙盒方法**（`SandboxMethodType.AGENT`）暴露视频理解能力，不注册任何指令/关键词触发，由主 Agent 判断时机调用并自行组织回复。

视频获取与抽帧思路参考自 [astrbot_zssm_explain](https://github.com/exynos967/astrbot_zssm_explain)（仅视频部分，其余功能未移植）。

## 功能

- **输入来源**：沙盒路径（`/app/uploads`、`/app/shared`）、视频直链、B 站链接 / BV 号、OneBot v11 消息 ID 回查（含会话归属校验）
- **分析模式**：
  - `frames`（默认）：ffprobe 测时长 → ffmpeg 等距抽帧 → 逐帧 caption → 汇总中文总结；可选音频转写（ASR）
  - `base64` / `dashscope` 直传模式：视频压缩后直接交给支持视频输入的模型
- **模型选择**：`MODEL_GROUP` → 留空回落频道主模型组 → `FALLBACK_MODEL_GROUPS` → 自动兜底扫描所有视觉 chat 组；`MODEL_OVERRIDE` 可同端点覆盖模型名
- **安全边界**：下载强制公网地址（防 SSRF）、限速限大小、时长/帧数上限、子进程超时、临时目录自动清理
- 兼容上游 `KroMiose/nekro-agent` 与 Akiyo Devin 分支（实例作用域/隔离/用量归属按动态探测接入，上游自动降级）

## 安装

将本仓库克隆到 NA 数据目录的 `plugins/workdir/` 下：

```bash
cd <NA数据目录>/plugins/workdir
git clone https://github.com/F1yCar/nekro_plugin_video.git
```

然后在 WebUI 插件管理中启用「视频理解」，或在配置中把 `F1yCar.nekro_plugin_video` 加入 `PLUGIN_ENABLED`。

容器内无 ffmpeg 时会通过 `dynamic_import_pkg` 自动安装 `imageio-ffmpeg`，无需手动处理。

## 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `MODEL_GROUP` | 空 | 视频分析模型组，留空用频道主模型组 |
| `MODEL_OVERRIDE` | `gemini-3-flash-preview` | 覆盖所选组的模型名（仅同端点换型号） |
| `FALLBACK_MODEL_GROUPS` | `[]` | 后备模型组，按序尝试 |
| `ASR_MODEL_GROUP` | 空 | 音频转写模型组（需 `audio/transcriptions` 端点） |
| `DIRECT_MODE` | `off` | `off`/`base64`/`dashscope` |
| `FRAME_INTERVAL` / `MAX_FRAMES` | 6 / 20 | 抽帧间隔与上限 |
| `MAX_SIZE_MB` / `MAX_DURATION` | 50 / 120 | 视频大小与时长上限 |

## Agent 调用契约

```python
async def analyze_video(_ctx, source: str = "", message_id: str = "") -> str
```

- `source`：沙盒路径、视频直链、B 站链接/BV 号
- `message_id`：当前会话中含视频的消息 ID（OneBot v11 回查）
- 二者必填其一；失败抛异常，返回纯文本分析结果给主 Agent

## 开发

```bash
.venv/bin/python -m pytest -q tests
.venv/bin/python -m ruff check .
```
