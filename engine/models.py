"""查一个接口上有哪些模型可用。

为什么要做这个
    用户填模型名全靠猜：Kimi 以前叫 moonshot-v1-8k，现在叫 kimi-k2.6。
    填错了报 404，报错还是英文的 "Not found the model"，
    用户根本不知道该填什么。

    改成：Key 填好就自动去问接口"你有哪些模型"，拉回来做成下拉框。
    这是最不会出错的做法——列表就是服务商自己给的。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .llm import _normalize_base


def list_models(base_url: str, api_key: str) -> tuple[bool, list[str], str]:
    """列出这个接口可用的模型。

    返回 (成功, 模型名列表, 错误信息)。

    走的是 OpenAI 的 /v1/models 接口，几乎所有兼容服务都实现了。
    有些服务不实现或者需要额外权限，那种情况返回失败，
    界面退回手动输入。
    """
    if not base_url or not api_key:
        return False, [], "地址和 Key 都要填"

    # 从 chat/completions 地址反推 /models 地址
    chat_url = _normalize_base(base_url)
    models_url = chat_url.replace("/chat/completions", "/models")

    req = urllib.request.Request(
        models_url,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return False, [], f"查不到模型列表（HTTP {e.code}）{detail}"
    except Exception as e:  # noqa: BLE001
        return False, [], f"查不到模型列表：{type(e).__name__}"

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return False, [], "接口返回的格式不是预期的样子"

    names = []
    for it in items:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or ""
            if mid:
                names.append(str(mid))

    names = sorted(set(names))
    if not names:
        return False, [], "接口没返回任何模型"
    return True, names, ""


def pick_default(models: list[str]) -> str:
    """从模型列表里挑一个合适的默认值。

    优先级：
        1. 名字里带 chat 的（对话模型，不是 embedding / tts）
        2. 排除明显的非对话模型（embedding、whisper、tts、rerank、vision-only）
        3. 剩下的按名字排第一个
    """
    if not models:
        return ""

    skip_words = ("embed", "whisper", "tts", "rerank", "moderation", "audio")

    def usable(name: str) -> bool:
        low = name.lower()
        return not any(w in low for w in skip_words)

    candidates = [m for m in models if usable(m)]
    if not candidates:
        candidates = models[:]

    # 优先带 chat 的
    for m in candidates:
        if "chat" in m.lower():
            return m

    return candidates[0]
