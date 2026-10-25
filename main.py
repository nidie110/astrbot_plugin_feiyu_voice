import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from gradio_client import Client, handle_file

PLUGIN_NAME = "astrbot_plugin_feiyu_voice"

# 所有路径都不写死：默认留空 = 不自动拉起服务，使用者自己在插件配置里填环境。
DEFAULT_CONFIG = {
    # IndexTTS WebUI 地址（gradio_client 去连它）
    "index_tts_url": "http://127.0.0.1:7860/",
    # 默认参考音色 wav；留空则只能靠 /音色 让每个用户各自上传
    "reference_audio": "",
    "max_chars": 200,
    "segment_max_chars": 40,
    "segment_pause_ms": 140,
    "replace_text_with_voice": True,
    "keyword_trigger": True,
    # ---- 服务自启（可选，全留空就不自启）----
    "service_venv_py": "",
    "service_webui": "",
    "service_webui_cwd": "",
    "service_webui_log": "",
    "service_shim": "",
    "service_shim_cwd": "",
    "service_shim_log": "",
    "service_shim_port": 9880,
    # 自启最长等待秒数（模型加载慢的机器可以调大）
    "service_wait_seconds": 180,
}

# Windows: 子进程不弹黑窗
CREATE_NO_WINDOW = 0x08000000


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def _spawn(cmd, cwd, log_path) -> None:
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log = open(log_path, "ab")
        subprocess.Popen(
            cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, close_fds=True,
        )
        logger.info(f"[feiyu_voice] 已拉起服务(pid 待定): {' '.join(cmd)}")
    except Exception as e:
        logger.error(f"[feiyu_voice] 拉起服务失败 {cmd[0]}: {e}")


# 句末切分：中英文句号、问号、感叹号、省略号、分号、换行
SENT_RE = re.compile(r"[^。！？!?…；;\n]+[。！？!?…；;\n]?")


class FeiyuVoice(Star):
    """大肥鱼·说话：把回复念成语音（IndexTTS 克隆音色，分段合成）。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.plugin_cfg = dict(config or {})
        self.data_dir = Path("data/plugin_data") / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.data_dir / "config.json"
        self.cfg = self._load_cfg()
        self.client = None
        self._pending: dict[str, bool] = {}

    # ---------- 配置 ----------

    def _load_cfg(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        if self.cfg_path.exists():
            try:
                cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning(f"[feiyu_voice] 配置读取失败: {e}")
        else:
            self.cfg_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.plugin_cfg:
            # 插件配置（_conf_schema.json）优先于本地 config.json，空值不覆盖
            cfg.update({k: v for k, v in self.plugin_cfg.items() if v not in (None, "")})
        return cfg

    # ---------- 文本切分 ----------

    def _split(self, text: str) -> list[str]:
        """按句末标点切句，过长的句子再按逗号切。"""
        limit = int(self.cfg.get("segment_max_chars", 40))
        text = re.sub(r"\s+", " ", (text or "").strip())
        if not text:
            return []
        out: list[str] = []
        for sent in SENT_RE.findall(text):
            sent = sent.strip()
            if not sent:
                continue
            if len(sent) <= limit:
                out.append(sent)
                continue
            # 长句按逗号/顿号再切
            buf = ""
            for piece in re.split(r"(?<=[，,、：:])", sent):
                if len(buf) + len(piece) <= limit:
                    buf += piece
                else:
                    if buf:
                        out.append(buf)
                    buf = piece
            if buf:
                out.append(buf)
        return out

    # ---------- TTS ----------

    def _get_client(self):
        url = self.cfg.get("index_tts_url", DEFAULT_CONFIG["index_tts_url"])
        if not url.endswith("/"):
            url += "/"
        if self.client is None:
            self.client = Client(url, verbose=False)
        return self.client

    # ---------- 服务自启（7860 IndexTTS / 9880 shim）----------

    _spawn_lock = threading.Lock()

    def _service_port(self) -> int:
        url = str(self.cfg.get("index_tts_url", DEFAULT_CONFIG["index_tts_url"]))
        m = re.search(r":(\d+)", url)
        return int(m.group(1)) if m else 7860

    def _ensure_service(self, wait: float = 0.0) -> bool:
        """7860 没起就后台拉起 IndexTTS；没配置自动拉起时只做探测。"""
        port = self._service_port()
        if _port_open(port):
            self._touch_shim()
            return True
        py = self.cfg.get("service_venv_py", "") or ""
        webui = self.cfg.get("service_webui", "") or ""
        if not py or not webui:
            logger.debug("[feiyu_voice] 未配置 service_venv_py / service_webui，不自动拉起")
            return self._wait_port(port, wait) if wait > 0 else False
        with self._spawn_lock:
            if not _port_open(port):
                cwd = self.cfg.get("service_webui_cwd", "") or os.path.dirname(webui)
                log = self.cfg.get("service_webui_log", "") or os.path.join(cwd, "webui.autostart.log")
                _spawn([py, webui, "--port", str(port)], cwd, log)
                logger.info("[feiyu_voice] IndexTTS 未运行，已后台拉起，模型加载约 60-90 秒")
        if wait <= 0:
            return False
        if self._wait_port(port, wait):
            self._touch_shim()
            return True
        logger.error(f"[feiyu_voice] 等了 {int(wait)} 秒，{port} 仍没就绪")
        return False

    def _wait_port(self, port: int, wait: float) -> bool:
        deadline = time.time() + wait
        while time.time() < deadline:
            if _port_open(port):
                return True
            time.sleep(2)
        return False

    def _touch_shim(self) -> None:
        """7860 起来后顺带保证 9880 shim 也在（AstrBot 原生 TTS provider 用它）。"""
        shim = self.cfg.get("service_shim", "") or ""
        if not shim:
            return
        try:
            shim_port = int(self.cfg.get("service_shim_port", 9880))
        except (TypeError, ValueError):
            shim_port = 9880
        if not _port_open(shim_port):
            py = self.cfg.get("service_venv_py", "") or "python"
            cwd = self.cfg.get("service_shim_cwd", "") or os.path.dirname(shim)
            log = self.cfg.get("service_shim_log", "") or os.path.join(cwd, "tts_shim.autostart.log")
            _spawn([py, shim], cwd, log)

    async def initialize(self):
        """插件加载完成后，如果语音服务没在跑，就后台预热，不阻塞启动。"""
        try:
            if not _port_open(self._service_port()):
                logger.info("[feiyu_voice] 检测到 IndexTTS 未运行，开始后台自启")
            asyncio.create_task(asyncio.to_thread(self._ensure_service, self._wait_seconds()))
        except Exception as e:
            logger.warning(f"[feiyu_voice] 自启检查失败: {e}")

    def _wait_seconds(self) -> float:
        try:
            return float(self.cfg.get("service_wait_seconds", 180))
        except (TypeError, ValueError):
            return 180.0

    def _ref_audio(self, event: AstrMessageEvent) -> str | None:
        user_ref = self.data_dir / "voices" / f"{event.get_sender_id()}.wav"
        if user_ref.exists():
            return str(user_ref)
        ref = self.cfg.get("reference_audio", "")
        if ref and os.path.exists(ref):
            return ref
        return None

    def _tts_one(self, text: str, ref: str, out: Path) -> None:
        if not self._ensure_service(wait=self._wait_seconds()):
            raise RuntimeError(
                f"IndexTTS 服务没起来（{self.cfg.get('index_tts_url')}）。"
                "请先启动 IndexTTS WebUI，或在插件配置里填 service_* 让它自启。"
            )
        client = self._get_client()
        res = client.predict(
            prompt=handle_file(ref),
            text=text,
            infer_mode="批次推理",
            max_text_tokens_per_sentence=120,
            sentences_bucket_max_size=4,
            param_5=True,
            param_6=0.8,
            param_7=30,
            param_8=1.0,
            param_9=0.0,
            param_10=3,
            param_11=10.0,
            param_12=600,
            api_name="/gen_single",
        )
        src = res.get("value") if isinstance(res, dict) else res
        if not src or not os.path.exists(src):
            raise RuntimeError("IndexTTS 没有返回有效的音频文件")
        shutil.copy2(src, out)

    def _synthesize(self, text: str, ref: str) -> str:
        """分段合成，再拼成一个 wav，段落之间留一点停顿。"""
        import numpy as np
        import soundfile as sf

        segments = self._split(text[: int(self.cfg.get("max_chars", 200))])
        if not segments:
            raise ValueError("没有可念的内容")

        tmp_dir = self.data_dir / "generated" / "parts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        pause_ms = int(self.cfg.get("segment_pause_ms", 140))

        waves = []
        sr = None
        for i, seg in enumerate(segments):
            part = tmp_dir / f"p_{uuid.uuid4().hex[:6]}_{i}.wav"
            self._tts_one(seg, ref, part)
            data, sr_i = sf.read(str(part), dtype="float32", always_2d=True)
            sr = sr or sr_i
            waves.append(data)
            try:
                part.unlink()
            except Exception:
                pass

        if not waves:
            raise RuntimeError("分段合成失败")

        silence = np.zeros((int(sr * pause_ms / 1000), waves[0].shape[1]), dtype="float32")
        merged = []
        for i, w in enumerate(waves):
            merged.append(w)
            if i != len(waves) - 1:
                merged.append(silence)
        audio = np.concatenate(merged, axis=0)

        out = self.data_dir / "generated" / f"v_{uuid.uuid4().hex[:8]}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), audio, sr, subtype="PCM_16")
        logger.info(f"[feiyu_voice] 分段合成完成: {len(segments)} 段, {len(audio)/sr:.1f}s")
        return str(out)

    # ---------- 想听声音时，把她的回复转成语音 ----------

    # 明确要求「用语音说/发/念 XXX」——直接念 XXX，不经过模型
    EXPLICIT_RE = re.compile(
        r"^(?:用|用你的|给)?\s*(?:语音|声音|嗓子|嘴)\s*(?:说|发|念|读|讲|来)\s*[：:，,。\s]*(.+)$",
        re.S,
    )

    # 想要语音的泛化说法：命中后把她的回复转成语音
    VOICE_INTENT = re.compile(
        r"(语音|声音|说给我听|念一下|念给|读一下|读给|出声|开口|"
        r"发条语音|发个语音|发一句|听听你|想听你|我想听你)"
    )

    # 一次「想听声音」的意图，允许延续几轮（她可能第一轮没反应过来）
    STICKY_TURNS = 2

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def mark_voice_intent(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if text.startswith("/"):
            return

        # 1) 明确指令式：用语音说XXX / 语音发XXX -> 直接念 XXX
        m = self.EXPLICIT_RE.match(text)
        if m:
            payload = m.group(1).strip(" ：:，,。\n")
            ref = self._ref_audio(event)
            if payload and ref:
                event.stop_event()
                logger.info(f"[feiyu_voice] 明确指令，直接念: {payload[:40]}")
                try:
                    path = await asyncio.to_thread(self._synthesize, payload, ref)
                except Exception as e:
                    logger.error(f"[feiyu_voice] 明确指令合成失败: {e}")
                    yield event.plain_result(f"语音没生成出来：{type(e).__name__}")
                    return
                yield event.chain_result([Comp.Record(file=path, url=path)])
                return

        # 2) 泛化说法：标记本轮，等她回复出来后转语音
        if not self.cfg.get("keyword_trigger", True):
            return
        broad = event.is_private_chat() or event.is_admin()
        hit = bool(self.VOICE_INTENT.search(text))
        if not broad and not re.search(r"(语音|声音|念|读)", text):
            hit = False
        if hit:
            self._pending[event.unified_msg_origin] = self.STICKY_TURNS
            logger.info(f"[feiyu_voice] 本轮想要语音: {text[:40]}")
        elif self._pending.get(event.unified_msg_origin, 0) > 0:
            self._pending[event.unified_msg_origin] -= 1

    NO_MOUTH_RE = re.compile(
    r"(没嘴|没有嘴|开不了口|发不出|发不了|说不出|不会说话|只能打字|只会打字|说不了话|"
    r"语音.{0,6}(不行|不了|发不出|出不来)|我.{0,4}(不能|不会).{0,4}(说话|发声|语音))"
)

    @filter.on_decorating_result()
    async def attach_voice(self, event: AstrMessageEvent):
        pending = self._pending.get(event.unified_msg_origin, 0) > 0
        result = event.get_result()
        if result is None:
            return

        pieces = [c.text for c in (result.chain or []) if isinstance(c, Comp.Plain)]
        spoken = "".join(pieces).strip()
        if not spoken:
            return

        # 兜底：她要是又否认自己能发声，就把这句拗断，照样发声
        denied = bool(self.NO_MOUTH_RE.search(spoken))
        if not pending and not denied:
            return

        # 去掉"没嘴/只会打字"这类自我否认的句子
        spoken = re.sub(r"[^。！？\n]*(没嘴|没有嘴|开不了口|发不出|发不了|说不出|不会说话|只能打字|只会打字|说不了话|语音[^。！？\n]{0,8}(不行|不了|发不出|出不来))[^。！？\n]*(。|！|？|\n|$)", "", spoken)
        spoken = spoken.strip(" ，,")
        if len(spoken) < 2:
            spoken = "嗯，听着呢。"
        spoken = spoken[: int(self.cfg.get("max_chars", 200))]

        ref = self._ref_audio(event)
        if not ref:
            return
        try:
            path = await asyncio.to_thread(self._synthesize, spoken, ref)
        except Exception as e:
            logger.error(f"[feiyu_voice] 回复转语音失败: {e}")
            return

        self._pending[event.unified_msg_origin] = 0
        if self.cfg.get("replace_text_with_voice", True):
            result.chain = [c for c in result.chain if not isinstance(c, Comp.Plain)]
        result.chain.append(Comp.Record(file=path, url=path))

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="speak_text_aloud")
    async def speak_text_aloud(self, event: AstrMessageEvent, text: str):
        """用你自己的声音把一段话念出来，发成语音消息。

        什么时候调用：
        - 对方明确想听声音："念一下""用语音说""发条语音""说给我听""读出来""我要听你说话"。
        - 对方让你唱但你唱不了时，可以念出来代替，并说明自己只会念不会唱。

        平时聊天不要调用，一条回复最多用一次。不要重复输出和语音一样的文字。

重要：你确实能发声。禁止说"我没有嘴""发不了语音""我只会打字"这类话——那是假的。

        Args:
            text(string): 要念的内容，口语化，最好 60 字以内，不要带表情和 Markdown
        """
        text = (text or "").strip()
        if not text:
            yield event.plain_result("要念什么？")
            return

        # 工具已经发声，就不要再让 hook 重复发一遍
        self._pending[event.unified_msg_origin] = 0

        ref = self._ref_audio(event)
        if not ref:
            yield event.plain_result("没有参考音频，先把音色设好")
            return

        try:
            path = await asyncio.to_thread(self._synthesize, text, ref)
        except Exception as e:
            logger.error(f"[feiyu_voice] 生成失败: {e}")
            yield event.plain_result(f"语音没生成出来：{type(e).__name__}")
            return

        yield event.chain_result([Comp.Record(file=path, url=path)])

    # ---------- 指令 ----------

    @filter.command("说话")
    async def cmd_say(self, event: AstrMessageEvent):
        """让她念一句：/说话 你好呀"""
        raw = (event.message_str or "").strip()
        parts = raw.split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""
        if not text:
            yield event.plain_result("用法：/说话 要念的内容")
            return
        ref = self._ref_audio(event)
        if not ref:
            yield event.plain_result("没有参考音频，先把音色设好")
            return
        yield event.plain_result("生成中…")
        try:
            path = await asyncio.to_thread(self._synthesize, text, ref)
        except Exception as e:
            logger.error(f"[feiyu_voice] 生成失败: {e}")
            yield event.plain_result(f"失败：{type(e).__name__}: {e}")
            return
        yield event.chain_result([Comp.Record(file=path, url=path)])

    @filter.command("音色")
    async def cmd_voice(self, event: AstrMessageEvent):
        """设置自己的音色：/音色 <wav 路径>"""
        raw = (event.message_str or "").strip()
        parts = raw.split(maxsplit=1)
        path = parts[1].strip() if len(parts) > 1 else ""
        if not path or not os.path.exists(path):
            yield event.plain_result("用法：/音色 D:\\某个录音.wav（必须是 wav 文件）")
            return
        dest = self.data_dir / "voices" / f"{event.get_sender_id()}.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        yield event.plain_result("音色换好了，试试 /说话 你好")

    @filter.command("语音状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看语音服务状态"""
        try:
            c = self._get_client()
            eps = c.view_api(return_format="dict")
            n = len(eps.get("named_endpoints", {}))
            ok = f"已连接，{n} 个接口"
        except Exception as e:
            ok = f"连不上：{type(e).__name__}"
        yield event.plain_result(
            f"IndexTTS: {self.cfg.get('index_tts_url')} | {ok}\n参考音频: {self._ref_audio(event)}"
        )
