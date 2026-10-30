"""危险操作的验证码门。

为什么要这个
    前台丫头能派活。派出去的活能删库、能回滚代码、能读密钥文件。
    而她和用户之间的通道（QQ / 飞书）是**可以被盗用的**——
    拿到手机的人能翻聊天记录，能学用户的语气，能说「确认」。

    所以「问用户确认」这个方案挡不住恶意，只能挡手滑。

    真正的独立通道得是**不在对话里的东西**——邮箱。

三级递进
    第 1 次   提醒      「这会删掉整个数据库，确定吗？」
    第 2 次   警告      「再说一次：不可逆。真的要做？」
    第 3 次   验证码    坚持 → 发到邮箱，拿到码才执行

    前两次是给用户机会自己停下的（说错话、用词夸张、手滑）。
    第三次才是真正的身份验证：贼能说三次「确认」，
    但拿不到邮箱里的那串数字。

几个必须守住的点
    一、验证码只发到邮箱，**绝不出现 IM 通道里**。
        一旦出现在对话里，它就跟聊天记录一起被偷走了。

    二、一次性。验过就作废，不能复用。

    三、有次数上限。猜错了锁一段时间，不让暴力枚举。

    四、绑定到具体的操作。
        给「删 A 库」发的码，不能拿去「删 B 库」。
"""

from __future__ import annotations

import hashlib
import json
import os
import smtplib
import secrets
import time
from dataclasses import dataclass, field
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

# 验证码有效期（秒）。
#
# 用户定的：120 秒。短信验证码通常是 5 分钟，但这里是本机操作，
# 用户就在电脑前面，两分钟足够翻一下邮箱。
# 给太长的窗口反而让"贼在旁边等着"有可乘之机。
CODE_TTL = 120

# 验证码过期之后，安全锁还要关多久（秒）。
#
# 为什么要有这一段：
#    验证码过期只说明"这次没验证成"，不代表这个操作就不危险了。
#    如果过期就直接放行，那攻击者只要拖到过期就能派活。
#
#    所以要再锁 10 分钟。锁满自动解（用户可能真的只是没看到邮件，
#    不能让他被自己锁死）。
LOCK_AFTER_EXPIRE = 600

# 最多猜几次
MAX_TRIES = 3

# 猜错了锁多久（秒）
LOCK_SECONDS = 600

# 危险操作清单。
#
# ⚠️ 这只是"初筛"。真正的判断还是在前台那个模型那里——
#    她看到用户说「把数据库清一下」，即使一个关键词都没匹配上，
#    也该自己意识到这是危险操作。
#    这里的关键词是给**代码层**做最后一道检查用的。
DANGEROUS_PATTERNS = (
    # 删除
    "rm -rf", "rm -r", "del /f", "del /s", "rd /s", "rmdir /s",
    "drop table", "drop database", "truncate", "delete from",
    "清空", "删库", "删掉整个", "全部删",
    # 版本控制
    "git reset --hard", "git push -f", "git push --force",
    "git clean -fd", "git branch -D", "回滚到", "重置到",
    # 磁盘
    "format ", "diskpart", "mkfs",
    # 系统
    "reg delete", "setx ", "环境变量",
    # 密钥
    ".env", "credentials", "id_rsa", "api_key", "secret_key",
)


@dataclass
class PendingCode:
    """一次待验证的申请。"""
    code: str
    task: str              # 要执行的任务
    purpose: str           # 用途（用于邮件正文）
    expires: float = 0.0
    tries: int = 0


@dataclass
class State:
    pending: dict[str, PendingCode] = field(default_factory=dict)
    # 按"谁"的失败次数记录，防止暴力试
    fails: dict[str, int] = field(default_factory=dict)
    locked_until: dict[str, float] = field(default_factory=dict)


_state = State()

# 已经通过验证码的任务指纹。
#
# 为什么不复用 _state.pending：
#    pending 是"等验证的"，验过就删了。
#    这个记的是"验过之后允许执行"，两者生命周期不一样。
#
# 为什么要记：
#    验过之后，用户可能隔几秒才真正派活（中间还要说句话）。
#    不记的话，派活时再查就查不到"验过"的痕迹，会被重新拦下。
#
# 有效期 10 分钟——比验证码本身长，因为中间可能聊几句。
_verified: dict[str, float] = {}


def mark_verified(task: str) -> None:
    """记下这个任务已经验过了。"""
    _verified[_fingerprint(task)] = time.time() + 600


def is_verified(task: str) -> bool:
    """这个任务验过吗（且没过期）。"""
    fp = _fingerprint(task)
    exp = _verified.get(fp)
    if exp is None:
        return False
    if exp < time.time():
        _verified.pop(fp, None)
        return False
    return True


# 安全锁：验证码过期后还要关多久。
#
# 结构：{指纹: 解锁时刻}
# 有这个记录、且没到时刻 → 锁着
_locked: dict[str, float] = {}

# 强制锁：验证码错满三次之后。
#
# 跟普通锁的区别：
#     普通锁（验证码过期）  → 可以重新发码，重置倒计时
#     强制锁（错满三次）    → **不许重新发码**，只能等它自己满
#
# 为什么分开：
#     两个人可能遇到的情况完全不一样。
#
#     验证码过期   → 用户可能就是没看到邮件。让他重发是合理的。
#     验证码错三次  → 可能是有人在猜。这时候再给他无限次重发，
#                    等于把 10 万种可能（6 位数字）摊在十分钟里试。
#
#     用户的原话：
#         「验证码错误（满三次）→ 告知用户还剩几次机会
#           → 三次全错 → 强制锁十分钟（无法重新获取验证码）」
_force_locked: dict[str, float] = {}


def _expire_pending(fp: str) -> None:
    """一个验证码过期了：删掉它，开始锁。"""
    p = _state.pending.pop(fp, None)
    if p is None:
        return
    # 锁满自动解，不需要谁来删
    _locked[fp] = time.time() + LOCK_AFTER_EXPIRE


def lock_remaining(task: str) -> int:
    """这个任务还要锁多少秒。0 表示没锁。

    ⚠️ 要同时看两种锁。红队报告 F-09 指出的就是这个。

        原来只查 _locked（"验证码过期后还要锁 10 分钟"那种）。
        但还有 _force_locked —— "被拦满三次、任务端锁死"的那种。

        后果：is_fenced() 调的是这个函数，于是"被拦满三次锁死之后，
        is_fenced 却说没被关着"。

        派活那条路本身安全（cmd_dispatch 里单独查了 _force_locked），
        所以不是直接可利用的漏洞。但一个叫"还剩多少秒"的函数
        不知道最重的那种锁，迟早有人在别处用它做判断。

        现在两种锁取大的那个。
    """
    fp = _fingerprint(task)

    # 先看看有没有过期的码要处理
    p = _state.pending.get(fp)
    if p is not None and p.expires < time.time():
        _expire_pending(fp)

    # 任务端级强制锁（被拦满三次那种）
    force_until = _force_locked.get(fp)
    if force_until is None:
        force_until = _force_locked.get("_all")
    force_left = int((force_until or 0) - time.time())
    if force_left > 0:
        return force_left

    until = _locked.get(fp)
    if until is None:
        return 0
    left = int(until - time.time())
    if left <= 0:
        # 锁满了，自动开
        _locked.pop(fp, None)
        return 0
    return left


def is_fenced(task: str) -> bool:
    """这个任务现在被安全锁关着吗。

    什么时候会这样：
        · 验证码还活着（等用户填）
        · 验证码过期了，但锁还没满 10 分钟
    """
    fp = _fingerprint(task)

    # 码还没过期 → 等验证，算被关着
    p = _state.pending.get(fp)
    if p is not None and p.expires >= time.time():
        return True

    return lock_remaining(task) > 0


def check(task: str) -> dict:
    """派活前的检查。

    返回：
        {
            "fenced": bool,      被安全锁关着吗（关着就什么都派不出去）
            "verified": bool,    这个任务验过了吗
            "dangerous": bool,   看着像危险操作吗（仅供参考）
            "lock_left": int,    还要锁多少秒
            "waiting": bool,     有没有一个还没过期的码在等用户填
            "reason": str,
        }

    ⚠️ 关键设计：**围栏是全局的，不是按任务分的。**

        用户的原话：
            「只要出了验证码，整个任务端都锁死，什么活都排不出去，
              这已经触发了安全围栏」

        所以这里不判断"这个任务危不危险"再决定拦不拦——
        一旦有任务触发过围栏（发了码），在这个任务解锁之前，
        **所有派活请求都被拒绝**。

        那个"别的任务"怎么判断它属于这个围栏？
            简单做法：只要有任何任务处于围栏状态，就全站锁。
            因为围栏的语义是"系统可能被入侵了，先停下来"。
    """
    # 先扫一遍，把过期的码转成锁
    for fp in list(_state.pending.keys()):
        p = _state.pending.get(fp)
        if p is not None and p.expires < time.time():
            _expire_pending(fp)

    """任意一个任务在围栏里，全站锁。"""
    now = time.time()
    any_pending = any(p.expires >= now for p in _state.pending.values())

    # 清理到期自动解开的锁
    for fp in list(_locked.keys()):
        if _locked[fp] <= now:
            _locked.pop(fp, None)

    # 清理到期的强制锁
    for fp in list(_force_locked.keys()):
        if _force_locked[fp] <= now:
            _force_locked.pop(fp, None)

    # 强制锁期间绝对不能派活，也不能重发
    forced = bool(_force_locked)

    global_lock = any_pending or bool(_locked) or forced

    # 这个任务自己的状态
    mine_verified = is_verified(task)
    mine_pending = has_pending(task)

    # ⚠️ lock_left 是**全站**的解锁剩余时间，不是这个任务自己的。
    #
    #    原来写的是 lock_remaining(task)——查的是"这个任务有没有锁"。
    #    但围栏是全局的：A 任务触发的锁，B 任务也要等。
    #    查 B 自己的锁当然查到 0，于是"还要锁多久"显示成 0，
    #    用户以为马上就好，实际上要等十分钟。
    #
    #    有多个锁时取**最晚**的那个——最后一个解开才算真的开。
    if _locked:
        lefts = [int(v - now) for v in _locked.values() if v > now]
        global_left = max(lefts) if lefts else 0
    else:
        global_left = 0

    # 强制锁的剩余时间也算进"还要锁多久"
    if _force_locked:
        fl = max(int(v - now) for v in _force_locked.values())
        global_left = max(global_left, fl)

    # 有码在等的时候，显示的是"等用户填"，不算倒计时
    mine_left = global_left if not any_pending else 0

    # 能不能重新发码：强制锁期间不行
    can_resend = not forced

    reason = ""
    if global_lock and not mine_verified:
        if forced:
            reason = (
                f"验证码连错三次，强制锁 "
                f"{global_left // 60} 分 {global_left % 60} 秒。"
                f"这段时间里不能重新获取验证码。"
            )
        elif any_pending:
            reason = "有验证码等着填，任务端现在锁着"
        elif global_left > 0:
            reason = (
                f"验证码超时了，任务端还要锁 "
                f"{global_left // 60} 分 {global_left % 60} 秒"
            )
        else:
            reason = "任务端被别的验证请求锁着"

    fenced = bool(global_lock and not mine_verified)

    return {
        # fenced：派不出去（这是插件真正要看的）
        "fenced": fenced,
        # verified：能不能派。跟 fenced 相反，但语义更直白——
        # 没被围栏关着就是"可以派"，不管这个任务本身危不危险
        "verified": not fenced,
        "dangerous": looks_dangerous(task),
        "lock_left": mine_left,
        "waiting": mine_pending,
        "can_resend": can_resend,
        "forced": forced,
        "reason": reason,
    }


def _cfg_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "dsh-bridge")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "guard.json")


def load_config() -> dict:
    """读安全配置（邮箱、SMTP）。

    ⚠️ 这里存的是 SMTP 授权码。不进代码、不进仓库、
        不写日志、不回传给界面。
    """
    p = _cfg_path()
    if not os.path.exists(p):
        return {
            "email": "",
            "smtp_host": "",
            "smtp_port": 465,
            "smtp_user": "",
            "smtp_pass": "",
            "enabled": False,
        }
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("enabled", bool(d.get("email") and d.get("smtp_host")))
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {"email": "", "smtp_host": "", "smtp_port": 465,
            "smtp_user": "", "smtp_pass": "", "enabled": False}


def save_config(cfg: dict) -> None:
    p = _cfg_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    # 这个文件里有授权码，收紧权限
    try:
        import subprocess

        user = os.environ.get("USERNAME") or ""
        if user:
            subprocess.run(
                ["icacls", p, "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, timeout=10,
            )
    except Exception:  # noqa: BLE001
        pass


def is_guard_ready() -> bool:
    """验证码通道配好了没有。"""
    c = load_config()
    return bool(c.get("enabled") and c.get("email") and c.get("smtp_host"))


# 常见邮箱服务商的 SMTP 参数。
#
# 填个邮箱地址就该认出服务器——不该让用户去查文档。
# 查不到的一律让用户自己填，不瞎猜。
_SMTP_PRESETS = {
    "qq.com": ("smtp.qq.com", 465),
    "foxmail.com": ("smtp.qq.com", 465),
    "163.com": ("smtp.163.com", 465),
    "126.com": ("smtp.126.com", 465),
    "yeah.net": ("smtp.yeah.net", 465),
    "sina.com": ("smtp.sina.com", 465),
    "sina.cn": ("smtp.sina.com", 465),
    "sohu.com": ("smtp.sohu.com", 465),
    "aliyun.com": ("smtp.aliyun.com", 465),
    "139.com": ("smtp.139.com", 465),
    "189.cn": ("smtp.189.cn", 465),
    "gmail.com": ("smtp.gmail.com", 465),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
    "live.com": ("smtp-mail.outlook.com", 587),
    "office365.com": ("smtp.office365.com", 587),
    "yahoo.com": ("smtp.mail.yahoo.com", 465),
    "icloud.com": ("smtp.mail.me.com", 587),
}


def detect_smtp(email: str) -> tuple[str, int] | tuple[None, None]:
    """从邮箱地址猜出 SMTP 服务器。

    猜得到返回 (host, port)，猜不到返回 (None, None)——
    猜不到就让用户填，不瞎猜（猜错了会发不出邮件，
    而用户会以为是自己的授权码有问题，白折腾半天）。
    """
    if not email or "@" not in email:
        return None, None
    domain = email.rsplit("@", 1)[1].strip().lower()
    return _SMTP_PRESETS.get(domain, (None, None))


def looks_dangerous(task: str) -> bool:
    """这段任务描述看着危险吗。

    只做初筛——说了「删」「回滚」这类词的算危险。
    判断很保守：宁可多要一次验证码，也别漏放过删库的。
    """
    low = (task or "").lower()
    return any(p in low for p in DANGEROUS_PATTERNS)


def _fingerprint(task: str) -> str:
    """给任务算个指纹，让验证码跟具体任务绑定。

    为什么要绑定：
        给「删 A 库」发的码，不能拿去「删 B 库」。
        不绑的话，贼只要让你为一件无害的事过个码，
        那个码就能拿去干别的。
    """
    return hashlib.sha256((task or "").encode("utf-8")).hexdigest()[:16]


def _new_code() -> str:
    """6 位数字。用 secrets 生成，不用 random。"""
    return f"{secrets.randbelow(1_000_000):06d}"


def send_code_email(task: str, purpose: str = "") -> tuple[bool, str]:
    """发一个验证码到用户的邮箱。返回 (成功, 提示)。

    ⚠️ 这个函数**不把验证码返回给调用方**。
        返回给调用方就等于写进了对话上下文，
        那这个码就废了。

        验证码只走邮件这一条路。
    """
    c = load_config()

    # 服务器没填的话，从邮箱地址自动认一个
    if not c.get("smtp_host") and c.get("email"):
        h, p_ = detect_smtp(c["email"])
        if h:
            c["smtp_host"] = h
            c["smtp_port"] = p_
            save_config(c)

    if not is_guard_ready():
        return False, "还没配安全邮箱"

    fp = _fingerprint(task)

    # 强制锁期间不许重新发码
    fl = _force_locked.get(fp)
    if fl is not None:
        left = int(fl - time.time())
        if left > 0:
            return False, (
                f"验证码连错三次，锁着 {left // 60} 分 {left % 60} 秒。\n"
                f"这段时间里不能重新获取验证码——只能等它自己满。"
            )
        # 锁满了，清掉
        _force_locked.pop(fp, None)

    code = _new_code()
    _state.pending[fp] = PendingCode(
        code=code,
        task=task,
        purpose=purpose,
        expires=time.time() + CODE_TTL,
    )

    subject = "【危险操作验证码】"
    body = (
        f"有人要求执行一个危险操作，需要你的确认。\n\n"
        f"操作内容：\n{task[:500]}\n\n"
        + (f"原因：{purpose}\n\n" if purpose else "")
        + f"验证码：{code}\n\n"
        f"这个码 {CODE_TTL // 60} 分钟内有效，只能用一次。\n\n"
        f"⚠️ 如果这不是你要求的，**什么都不要做**，\n"
        f"  并且立刻去检查你的账号有没有被别人登录。\n"
    )

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("电脑端", c.get("smtp_user") or c.get("email")))
        msg["To"] = c["email"]

        host = c.get("smtp_host")
        port = int(c.get("smtp_port") or 465)
        use_ssl = port == 465 or bool(c.get("smtp_ssl", True))

        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()

        try:
            if c.get("smtp_user") and c.get("smtp_pass"):
                server.login(c["smtp_user"], c["smtp_pass"])
            server.sendmail(
                msg["From"], [c["email"]], msg.as_string()
            )
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass

        return True, f"验证码已发到 {_mask_email(c['email'])}"
    except Exception as e:  # noqa: BLE001
        # 发送失败要把 pending 撤掉，别留个永远验不过的码
        _state.pending.pop(fp, None)
        return False, f"发邮件失败：{type(e).__name__}"


def _mask_email(addr: str) -> str:
    """把邮箱遮一半，界面上显示用。"""
    if not addr or "@" not in addr:
        return "***"
    name, domain = addr.split("@", 1)
    if len(name) <= 2:
        return f"{name[0]}***@{domain}"
    return f"{name[:2]}***@{domain}"


def verify(task: str, code: str, who: str = "") -> tuple[bool, str]:
    """验码。返回 (通过, 提示)。

    who 用来记录失败次数，防止暴力猜。
    """
    now = time.time()

    # 锁了吗
    until = _state.locked_until.get(who or "_", 0)
    if until > now:
        left = int(until - now)
        return False, f"试错太多次，{left} 秒后再试"

    fp = _fingerprint(task)
    p = _state.pending.get(fp)
    if p is None:
        return False, "没有待验证的操作（可能过期了，重新发起一次）"

    if p.expires < now:
        # ⚠️ 这里原来只删 pending，**没有上锁**。
        #
        #    红队 G 组打出来的：
        #        注入一个过期码 → verify 返回"过期了" → 删掉 pending
        #        → lock_remaining 返回 0（没锁）
        #
        #    而同一个文件里 _expire_pending() 做的事是：
        #        删 pending + 锁 LOCK_AFTER_EXPIRE
        #
        #    两处逻辑不一致。设计意图很清楚（LOCK_AFTER_EXPIRE
        #    这个常量的存在就说明了）：过期之后要锁一段时间，
        #    不让立刻重发。
        #
        #    实测：走 verify 这条路过期不锁，等于"码过期了马上就能
        #    要一个新的"——攻击者可以让码不停过期、不停重发，
        #    每一次重发都是一次新的猜测机会。
        #
        #    现在跟 _expire_pending 对齐。
        _state.pending.pop(fp, None)
        _locked[fp] = now + LOCK_AFTER_EXPIRE
        return False, "验证码过期了"

    if p.tries >= MAX_TRIES:
        _state.pending.pop(fp, None)
        _state.locked_until[who or "_"] = now + LOCK_SECONDS
        return False, f"这个码试错太多次，锁 {LOCK_SECONDS // 60} 分钟"

    p.tries += 1

    if not secrets.compare_digest(str(code).strip(), p.code):
        left = MAX_TRIES - p.tries
        if left <= 0:
            # 错满三次：**强制锁**
            #
            # 跟普通过期不一样：
            #     过期 → 可以重新发码
            #     错三次 → 十分钟内**不许重发**
            #
            # 因为错三次可能是有人在猜。再给他重发的权利，
            # 等于让他在十分钟里试 10 万种组合。
            _state.pending.pop(fp, None)
            _state.locked_until[who or "_"] = now + LOCK_SECONDS
            _force_locked[fp] = now + LOCK_SECONDS
            return False, (
                f"验证码连错 {MAX_TRIES} 次，锁 {LOCK_SECONDS // 60} 分钟。\n"
                f"这段时间里不能重新获取验证码——"
                f"这是为了防止有人猜。"
            )
        return False, f"验证码不对（还能试 {left} 次）"

    # 通过：一次性，立刻作废；同时记下"这个任务允许执行了"
    _state.pending.pop(fp, None)
    mark_verified(task)
    return True, "验证通过"


def has_pending(task: str) -> bool:
    """这个任务有没有正在等验证的码。"""
    p = _state.pending.get(_fingerprint(task))
    if p is None:
        return False
    if p.expires < time.time():
        _state.pending.pop(_fingerprint(task), None)
        return False
    return True
