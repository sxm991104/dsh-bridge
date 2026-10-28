"""对话模型调用：任何 OpenAI 兼容的接口都能接。

为什么自己写这一层
    原来任务端跑在 DeepSeek Harness 里，用户想用得先装 DSH。
    这对很多人是劝退的——为了用你的东西，先装一个几十兆的
    运行时、再配一堆东西。

    这一层的目标很窄：给一个 base_url + api_key + model 三样东西，
    就能跑起来。OpenAI、DeepSeek、通义、Kimi、本地 Ollama、
    任何照着 OpenAI 格式做的服务，都走同一个代码路径。

    不依赖任何 SDK。用 urllib 直接发 HTTP：
    多装一个包，用户就多一个装不上的可能。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .ratelimit import RateLimiter


class LLMError(Exception):
    """模型调用失败。分清楚是哪一类，后面才好提示用户。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str          # 原始 JSON 字符串，可能被截断，调用方要容错


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # 用量：算出花了多少 token，界面要显示
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 结束原因：stop / tool_calls / length
    finish_reason: str = ""


def _normalize_base(base_url: str) -> str:
    """把用户填的地址整理成能用的形式。

    用户可能填：
        https://api.openai.com
        https://api.openai.com/v1
        https://api.openai.com/v1/chat/completions
    这三种都得能用。统一成 .../v1/chat/completions。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.openai.com"
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


class LLM:
    """一个能发请求的模型客户端。

    config 是普通 dict，来自用户配置：
        base_url   必填
        api_key    必填
        model      必填
        timeout    可选，默认 180 秒
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 180):
        self.url = _normalize_base(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # 这个 Key 这家服务的频率限制。撞过 429 之后它就知道该隔多久。
        self.limiter = RateLimiter()

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int = 8192, temperature: float = 0.3) -> Reply:
        """发一轮对话，返回模型的话（可能带工具调用）。

        temperature 的处理有个坑：
            各家对它的宽容度不一样。实测 kimi-k2.6 只接受 1，
            发 0.3 直接 400：
                "invalid temperature: only 1 is allowed for this model"

            所以这里不做"聪明"的适配——先按传入的值发，
            如果对方因为参数不对拒了，就去掉这个参数重发一次。
            OpenAI 兼容服务全都接受"不传 temperature"。
        """
        def build(include_temp: bool) -> bytes:
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
            }
            if include_temp:
                payload["temperature"] = temperature
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def make_req(body: bytes) -> urllib.request.Request:
            return urllib.request.Request(
                self.url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )

        try:
            return self._parse(self._send(make_req(build(True))))
        except LLMError as e:
            msg = str(e).lower()
            # 只对这几种参数问题重试，别把真正的错误也吞掉
            if any(k in msg for k in ("temperature", "max_tokens", "top_p")):
                return self._parse(self._send(make_req(build(False))))
            raise

    def _send(self, req: urllib.request.Request) -> dict:
        """发请求，遇到限流和临时故障会等一下再试。

        为什么要专门处理 429：
            实测 kimi-k2.6 的免费额度是**每分钟 3 次请求**，
            而跑一个任务要好几轮（每轮一次请求），很容易撞上。
            不重试的话，任务跑到一半就废了，用户看到的只是
            "接口返回 429"——他不知道自己该等多久，也不知道该干什么。

            所以这里等：先 5 秒，再 10 秒、20 秒……
            服务端如果在响应里说了"再等几秒"，就照它说的等。
        """
        last_err: Exception | None = None
        # 最多试 5 次。限额很紧的服务（比如每分钟 3 次）也能熬过去。
        for attempt in range(5):
            # 上一轮撞限流的话，这里会等到安全时刻再发
            self.limiter.wait()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.limiter.mark_ok()
                return data
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:  # noqa: BLE001
                    pass

                # 429：限流。
                #
                # 不能只按它说的 "after 1 second" 等——那个 1 秒是
                # 通用话术，实际限制是"每分钟 3 次"，等 1 秒还在同一窗口。
                # 交给 limiter：它会从错误信息里读出真实 RPM，
                # 算出"下一次安全时刻"。
                if e.code == 429:
                    gap = self.limiter.on_429(detail)
                    rpm = self.limiter.rpm
                    if rpm:
                        desc = f"这台服务每分钟最多 {rpm} 次请求"
                    else:
                        desc = "这台服务有频率限制"
                    last_err = LLMError(
                        f"请求太快了，{desc}。已经等了一会儿，再试一次。"
                    )
                    if attempt < 4:
                        time.sleep(min(60.0, gap))
                        continue
                    raise LLMError(
                        f"{desc}，连续重试 {attempt + 1} 次还是被拒。"
                        "这个任务需要多轮对话，用免费额度可能跑不完——"
                        "换一个限额更宽的 Key，或者等一会儿再派。"
                    ) from None

                # 其它 4xx 是配置问题，重试没用
                if 400 <= e.code < 500 and e.code != 408:
                    raise LLMError(
                        f"接口返回 {e.code}。{_explain_http(e.code)}"
                        f"详情：{detail}"
                    ) from None

                # 5xx 和超时：临时故障，等一下再试
                last_err = LLMError(f"接口返回 {e.code}：{detail}")
            except urllib.error.URLError as e:
                last_err = LLMError(f"连不上接口：{e.reason}")
            except TimeoutError:
                last_err = LLMError("接口超时")
            except json.JSONDecodeError:
                last_err = LLMError("接口返回的不是 JSON")

            if attempt < 4:
                time.sleep(2 * (attempt + 1))

        raise last_err or LLMError("调用失败")

    @staticmethod
    def _retry_after(detail: str, default: float) -> float:
        """从错误信息里读"该等几秒"。

        各家写法不一样：
            kimi:    "please try again after 1 seconds"
            openai:  响应头里有 Retry-After（这里读不到，只能看正文）
        读不到就用默认的指数退避。
        """
        import re

        m = re.search(r"after\s+(\d+)\s*second", detail, re.I)
        if m:
            try:
                return min(60.0, max(1.0, float(m.group(1))))
            except ValueError:
                pass
        return min(60.0, default)

    def _parse(self, data: dict) -> Reply:
        """把 OpenAI 格式的返回拆成 Reply。

        各家服务在细节上有出入，这里按最宽松的方式读：
        有的服务 choices[0].message.content 是 null、只有 tool_calls，
        有的会把 tool_calls 塞在别处。尽量兼容。
        """
        choices = data.get("choices") or []
        if not choices:
            err = (data.get("error") or {}).get("message") if isinstance(
                data.get("error"), dict) else data.get("error")
            raise LLMError(f"模型没返回内容{('：' + str(err)) if err else ''}")

        ch = choices[0]
        msg = ch.get("message") or {}
        reply = Reply()
        reply.content = msg.get("content") or ""

        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            reply.tool_calls.append(ToolCall(
                id=tc.get("id") or f"call_{len(reply.tool_calls)}",
                name=fn.get("name") or "",
                arguments=fn.get("arguments") or "{}",
            ))

        reply.finish_reason = ch.get("finish_reason") or ""
        usage = data.get("usage") or {}
        reply.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        reply.completion_tokens = int(usage.get("completion_tokens") or 0)
        return reply


def _explain_http(code: int) -> str:
    """把常见状态码翻成用户能看懂的一句话。"""
    if code == 401:
        return "API Key 不对，或者已经失效了。"
    if code == 403:
        return "这个 Key 没有权限用这个模型。"
    if code == 404:
        return "接口地址填错了，或者这个模型不存在。"
    if code == 429:
        return "请求太频繁，或者额度用完了。"
    if code >= 500:
        return "对面服务出问题了，等一会儿再试。"
    return ""


def test_connection(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """试一下能不能通。设置页的「测试连接」用它。"""
    try:
        llm = LLM(base_url, api_key, model, timeout=30)
        reply = llm.chat(
            [{"role": "user", "content": "只回复两个字：可以"}],
            max_tokens=20,
        )
        text = (reply.content or "").strip()[:40]
        return True, f"通了。它回了：{text or '(空)'}"
    except LLMError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
