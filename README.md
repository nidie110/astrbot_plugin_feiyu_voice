# astrbot_plugin_feiyu_voice

用 IndexTTS 让 AstrBot 里的 bot「真的开口说话」：克隆音色、长文分段、自动把 TTS 服务拉起来。

## 人格设定里要写什么（装了之后必看）

这个插件**不直接提供给模型的工具**，它把自己的动作注册进
[`astrbot_plugin_persona_mind`](../astrbot_plugin_persona_mind) 的 `feiyu` 入口。所以：

1. **必须同时装 `persona_mind`。** 只装本插件时，模型那边一个动作都看不到 —— 不是坏了，是没有入口。
2. **人格设定里只写「什么时候该动手」，不要写 `feiyu(...)`、`action=`、`target=` 或任何参数名。**
   参数格式由插件在真要动手的那一轮临时告诉她（`engine/specs.py` + `engine/selector.py`），
   写进人格只会变成「同一件事两处说法」，而且插件启停时人格不会跟着变。
3. 人格里有这么几行，本插件才会被自然调用：

- 「用语音说」「念一下」「听你声音」「发条语音」→ **说**

### 可以直接粘贴的一段

只装本插件时，把下面这几行加进人格设定的末尾就够了：

```
- 「用语音说」「念一下」「用你的声音」「发条语音」→ 用我自己的声音念
```

**不要**改成 `feiyu(action="...")` 那种写法——参数格式由插件在真要动手的那一轮临时给她。

**反例**（这些不该触发）：裸的「说说」、「评论区」单独出现、她说「我去看看」但没给具体目标。

完整的原理、可逐字复制的人格段落、写入脚本，见 [persona_mind/README.md](../astrbot_plugin_persona_mind/README.md#让她自然地用这些本事人格设定里要写什么)。


## 怎么用

| 你这么说 | 她做什么 |
| --- | --- |
| 「用语音说一遍」 | 合成语音发出来 |
| 「念一下这段」「读给我听」 | 同上 |
| 「用你的声音说爱我」 | 用她当前音色念 |

**指令形式**：`/说话 <内容>`、`/音色 <wav路径>`、`/语音状态`

**注意**：语音服务（7860）模型加载要 60-90 秒，刚重启别立刻发语音；插件会自己把服务拉起来，但第一次会等。

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
