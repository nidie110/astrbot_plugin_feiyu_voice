# astrbot_plugin_feiyu_voice

用 IndexTTS 让 AstrBot 里的 bot「真的开口说话」：克隆音色、长文分段、自动把 TTS 服务拉起来。

## 特性

- **零音色训练**：上传一段 3~10 秒的 wav 就能克隆，支持每个用户用自己的音色（`/音色`）
- **服务自启**：插件加载即后台探测 `IndexTTS` 端口，没起就自己拉起来；每次合成前还会兜一次底，服务被关掉也能自愈
- **长文分段**：超过 `segment_max_chars` 会自动按句切开、逐段合成、按 `segment_pause_ms` 拼成一条
- **两种触发**：模型调 `speak_text_aloud` 工具，或者直接说「用语音说你好」
- **配置集中管理**：所有路径都由插件配置页填写，代码里没有任何机器专属路径

## 安装

1. 把本目录放到 `astrbot/data/plugins/`（或在 WebUI 上传 zip）
2. 装依赖：`pip install -r requirements.txt`（`gradio_client`、`numpy`、`soundfile`）
3. 准备好 IndexTTS（本项目在 `webui.py` 里提供了 `--port` 启动方式）
4. 到 WebUI → 插件配置里填「IndexTTS WebUI 地址」，需要自启再填服务那几项
5. 重启 AstrBot，看到日志 `feiyu_voice 就绪` 即可

## 配置

| 键 | 说明 |
| --- | --- |
| `index_tts_url` | IndexTTS 的 gradio 地址，默认 `http://127.0.0.1:7860/` |
| `reference_audio` | 默认参考音色 wav，留空则只能靠 `/音色` 逐人上传 |
| `max_chars` / `segment_max_chars` / `segment_pause_ms` | 长度与停顿控制 |
| `replace_text_with_voice` | 文字回复是否直接换成语音 |
| `keyword_trigger` | 是否识别「我想听你说话」这类自然语言意图 |
| `service_venv_py` / `service_webui` | 服务自启用的 python 与 `webui.py` 路径，**都留空则不自启，只做端口探测** |
| `service_shim` 等 | 可选，给 AstrBot 原生 OpenAI-TTS provider 用的 9880 转发脚本 |
| `service_wait_seconds` | 自启等待上限，默认 180 秒 |

## 指令与工具

| 类型 | 名称 | 说明 |
| --- | --- | --- |
| 指令 | `/说话 内容` | 把内容合成语音发送 |
| 指令 | `/音色 <wav路径>` | 设置当前会话/用户的参考音色 |
| 指令 | `/语音状态` | 看服务是否在线、当前音色 |
| 工具 | `speak_text_aloud` | 让模型自己决定「用语音回答」 |

## 常见问题

**合成返回错误、日志说连不上 7860**
服务没起。要么填好 `service_venv_py` + `service_webui` 让插件自启，要么手动启动 IndexTTS 再重试。

**第一次要语音很慢**
模型冷启动要几十秒，属于正常现象；之后一直常驻。机器慢就调大 `service_wait_seconds`。

**没有声音但没报错**
检查 `reference_audio` 是否存在、wav 是否为单声道 16k 以上。音色文件损坏时 IndexTTS 会静默返回空音频。

## 许可

MIT，见 [LICENSE](LICENSE)。
