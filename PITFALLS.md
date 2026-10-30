# 踩过的坑

这部分不是教程，是**血账**。

每一个坑都是真的崩过、真的丢过数据、真的让人半夜查日志。
写下来的意义只有一个：别人不用再踩一遍。

**这些问题大部分已经修了。留着是为了说清楚"为什么代码长这样"**——
很多时候代码看着绕，是因为直着走过去会掉进去。

## 踩过的坑（这部分可能比代码有用）

### 编码：`stream.read()` 会阻塞到进程结束

第一版读子进程输出用的是 `stream.read()`。**它是阻塞的，要等进程退出才返回。**

结果：心跳每 5 秒去读输出缓冲区，永远是空列表 —— `doing` 字段一直是空的，用户看不到它在做什么。

**改成 `readline()` 循环**，来一行存一行。

### 编码：Windows 子进程输出是 GBK

症状：文件里中文全对，但 QQ 那边收到一堆 `�`。

原因链：
1. 子进程 stdout 编码由 `PYTHONIOENCODING` 决定，没设时跟随系统 ANSI 代码页（中文 Windows 是 cp936/GBK）
2. 我们按 UTF-8 去解 → 全碎

**试过但没用的两种修法：**
- `subprocess.run(..., encoding="utf-8")` —— 没用，字节已经在子进程那边按 GBK 生成了
- 给子进程传 `PYTHONIOENCODING=utf-8` —— 对 `dsh.cmd` 这种批处理没用，它拉 node 时不一定带下去

**最后有效的做法：拿 bytes，先试 UTF-8 严格解码，失败再用 GBK。** 不依赖任何外部设置。

### 权限：`SetNamedSecurityInfoW failed (Win32 5)`

`dsh` 的沙箱默认是 `workspace-write` —— 只能写**一个**工作目录。

换工作目录治不了，因为沙箱只认"一个可写根"。症状是让它在另一个盘建文件，报 `Access denied [sandbox: file access denied under workspace-write]`。

**根子在这里**（`dsh --dump-config` 能看到）：

```yaml
- id: sandbox-policy
  config:
    mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
```

**塞一个环境变量就够：** `DSH_PERMISSION_MODE=danger-full-access`

**但有个坑：权限是在「会话创建时」固定的。**

带 `--session-id` 接手旧会话时，权限还是旧的，改环境变量没用。**必须删掉旧会话号重建一次。**

### 事件格式：别猜字段名，去看真的

`tool_call` 事件的真实结构是：

```json
{"type": "tool_call", "tool": "pwsh", "input": {"command": "Get-Date", "description": "..."}}
```

我一开始猜的是 `name` / `args`，写错了也不知道 —— 因为解析失败不报错，只是返回空字符串。

**如果没有实测抓一遍原始事件，这个 bug 会一直藏着。**

### `turn_end` 里的错误不是顶层 error

`dsh` 把「这一轮失败了」包在 `status` / `turn_end` 里：

```json
{"type":"status","phase":"turn_end","reason":{"kind":"error","error":{...}}}
```

只认顶层 `type=error` 的话，这类失败会被当成"跑完了没说话"，**状态还记成 ok**。

实测代价：把推理强度设成模型不支持的档位，任务真失败，但显示成功、耗时 12.6 秒、结果空字符串。

**假成功比失败更害人 —— 用户以为活干完了。**

### 推理强度：不是所有模型都认所有档位

`medium` 这个档 `deepseek-v4.1-flash` 不支持，设了会报 `UNSUPPORTED_REASONING_EFFORT`，整个任务白跑。

**能用的：`low` / `high` / `max`。**

### 沙箱的 ACL 改不了时，换个目录

`D:\` 下的目录，沙箱要改安全描述符（加专用 SID + 加 deny + 把完整性标签降为 Low），那一步会失败。

**`C:\Users\<你>\Downloads` 下面是能接管的**（那里本来就有沙箱标记：deny FILE_DELETE_CHILD、专用 SID、Low 完整性标签）。

**如果 `danger-full-access` 也起不来，先把工作目录换到 Downloads 下面试试。**

### 超时杀不干净，会话就永久锁死（最难查的一个）

这个坑值得单独写，因为**从外面完全看不到**，而且症状会把人引到错误的方向上。

**症状：** 同一个任务重跑，报

```
session "session-xxx" is already owned by an active write handle
```

**第一反应是去找锁文件。** 翻遍 `.dsh/sessions/<会话>/` 只有 `session.v4.jsonl.zstd`，**没有锁文件。**

**第二反应是以为和别的任务抢会话。** 也不是——那个任务早就结束了。

**真相：** `taskkill /F /T` 杀掉了进程树，但 dsh 拉起的不只是 `node.exe`，**还有它自己再拉的 `DeepSeek Harness.exe` 子进程**。任务被强杀时，这些孙进程偶尔会脱离父进程树，`/T` 就够不着了。

它们活着、握着会话的写句柄，而**任务状态早就写成 error 了**——所以看哪儿都像"任务已经结束"。

**怎么认出来：** 这些僵尸内存只有 **4 MB 左右**，CPU 时间几乎不涨。跑了几十分钟，任务管理器里一眼扫过去根本注意不到。

实测抓到过 4 个：跑了 17~38 分钟，全是 4 MB。

**怎么修：** 杀之前先抓快照。

```python
# 错的：直接杀树，脱离父树的孙进程收不到
subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)])

# 对的：先记下所有子孙，杀完树之后按 PID 补刀
children = _child_pids_snapshot(proc.pid)   # wmic 查 ParentProcessId 关系
subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)])
for pid in children:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)])
```

**关键是"按 PID 补刀"，不是"按名字扫"。** 按名字扫（比如杀掉所有 `DeepSeek Harness.exe`）会误伤桌面端自己那个主进程——那是另一个程序，杀掉等于把用户正在用的东西关了。

**为什么这个 bug 值得记住：**

- 查文件查不到 → 你会一直往"文件锁"的方向查
- 任务状态是 error → 你会以为任务已经结束了
- 进程只有 4 MB → 任务管理器里看不见
- 三个假象叠起来，能让人查半天

**而且它会自己恶化：** 每跑几十个任务积一批僵尸，攒够了整个调度就瘫了。上线后才撞上的话，基本查不出来。

### Windows 的 os.replace 会失败，POSIX 不会

多个任务同时更新 `tasks.jsonl` 时：

```
PermissionError: [WinError 5] 拒绝访问
  os.replace('tasks.jsonl.tmp.22112', 'tasks.jsonl')
```

**POSIX 允许原子替换一个正被打开的文件，Windows 直接拒绝。**

三个任务时撞不上，十个一压就有一半进程崩在启动阶段——状态卡在 pending，其实进程早退了。

**修法不是加锁**（试过，没用——锁只保护自己那段代码，挡不住别人已经打开的文件句柄），**是失败重试。**

### 后台线程碰界面，只有一条路：Signal

写桌面端时撞了两次同一个墙。

```
❌ QTimer.singleShot(0, lambda: self._show(data))   从后台线程调
✅ self.got_data.emit(data)                          信号
```

**从一个非主线程调 `QTimer`，那个计时器不会进主线程的事件循环，回调永远不执行。**

问题是它**不报错、不崩溃，只是静默失效**。表现是：二维码一直显示"正在取二维码…"，而后台其实每次都拿到了数据。

第一次在扫码头那里撞上，修好之后写 `request_once` 又踩了一遍——设置页的路径输入框永远空着。

**记住：跨线程改界面，用 Signal。没有第二种办法。**

### 界面闪烁，不是"多画了一次"

用户说"桌面端自己在乱闪，点哪儿都没反应"。查了半天颜色、样式、定时器，都不是。

**真正的原因是一次刷新 435 毫秒。**

```
任务列表每 3 秒刷一次
每次把 102 个 TaskRow 全销毁再全新建一遍
→ 界面有 15% 的时间在重建控件
→ 加上布局连锁反应，看起来就是持续闪烁
→ 主线程被占着，点什么都没反应
```

**修法：结构没变就原地更新文字，不重建。**

```
之前 435 毫秒 → 现在 17 毫秒
```

**顺带挖出两个同类问题：**

- 转圈控件的定时器写在构造函数里，卡片一创建就挂一个 60ms 的，谁也停不了。改成跟着可见性开关。
- `QScrollArea` 的 `setWidgetResizable(True)` + 内容里有 `addStretch()` = 布局死循环。Qt 要算"内容高度取决于可用空间 → 可用空间取决于滚动条 → 滚动条取决于内容高度"，永远算不完。

**这三个的共同点：都是在定时回调里无条件重做昂贵的事。**

### 但那次闪烁其实不是代码的问题

上面这些确实该修，**但用户当时看到的那个闪，是耳机压在笔记本触摸板上。**

触摸板被持续按住，系统一直在发鼠标事件，界面就一直重绘。代码没问题。

**这条写在这儿是因为它比前面三条更值钱：**

排查性能问题，第一件事是**先确认现象真的是软件造成的**。

当时的做法是错的——用户一说"闪"，我直接开始读代码、装计数器、量重绘次数。量出来的数字都是真实的（435 毫秒、3266 次 PaletteChange），**但它们解释不了那个闪**，于是我在这条错误的线索上越挖越深。

**正确的第一句应该是：「你手上、桌上、触摸板上有没有压着东西？」**

一个三十秒的问题，能省掉一下午。

### 路径少算一级，整个功能静默失效

`engine/scheduler.py` 要读 `tasks.jsonl`，它自己住在 `engine/` 子目录里：

```python
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))   # ← 这是 engine/，不是桥目录
```

于是它一直在读 `engine/tasks.jsonl`——**那个文件不存在。读不存在的文件返回空列表，而空列表让所有并发检查都通过。**

```
配置写的是"最多同时跑 2 个"
派 6 个任务
→ 6 个全跑起来了
→ 没有任何报错
```

**这个 bug 的可怕之处就是它不报错。** 文件读不到是"正常"的、列表是空的是"正常"的、检查通过也是"正常"的。**功能整个失效，日志一片安宁。**

正确的写法：

```python
BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
```

**教训：凡是"读文件 → 根据内容做判断"的代码，先把路径打出来看一眼。** 读不到和读到空，在很多逻辑里是一回事，但它们的含义差了十万八千里。

### 先占位再检查，等于自己占了位子

修好路径之后，`conc = 1` 的配置**连第一个任务都排队，再也不跑**。

因为顺序反了：

```python
rec = cmd_dispatch(...)      # 写进文件，状态 pending
if has_slot(pid, limit):     # 这时它自己已经在文件里了
```

而 `pending` 是要算占位的（否则并发控制形同虚设）。**所以是"我自己刚占了一个位子，然后问还有没有空位"。**

```python
has_room = has_slot(pid, limit)   # 先问
rec = cmd_dispatch(...)           # 再落盘
if has_room: spawn()
```

**任何"检查 → 占用"的逻辑，顺序必须是先检查后占用。** 听起来是废话，但代码里一旦把"创建记录"和"占用资源"混在同一步，就很容易写成反过来。

### 失败却显示成功：`return` 早了一行

401 的任务，状态 `ok`，结果栏里躺着一句错误信息。

```python
except LLMError as e:
    messages.append({..., "content": f"（模型调用失败：{e}）"})
    prog.doing = f"失败：{e}"
    _save(workdir, messages, prog)
    return prog          # ← 这时候 prog.messages 还是空的
```

`prog.messages = messages` 写在函数末尾的正常路径上，**错误路径直接 return 跳过了它**。

上层 `final_text()` 从 `prog.messages` 里找最后一条 assistant 消息——找不到，只能退回 `prog.doing`，而那是"失败：接口返回 401…"，不以判定用的前缀开头，**于是被判成成功**。

修了两处：

1. 错误路径补上 `prog.messages = messages`
2. 判定从"看一个前缀"改成"三条里任意一条成立"——文案一旦被包一层就漏判，不能只认字面

**假成功比失败害人。** 用户看到 `ok`，以为活干完了，不会去查。

### 少一行 `addWidget`，控件被垃圾回收后一碰就崩

给设置页加配置列表，重写了一整块，漏了：

```python
lay.addWidget(card)      # ← 这行没了
```

后果：

```
card 建出来了
控件也塞进去了（cl.addWidget(...)）
但没有任何人引用 card
→ Qt 的垃圾回收把整棵子树销毁
→ self.ed_base 指向一个死掉的 C++ 对象
→ RuntimeError: libshiboken: Internal C++ object (QLineEdit) already deleted
```

**排查过程值得记下来，因为我先错了两次：**

| 猜想 | 怎么否掉的 |
|---|---|
| 重复创建，后一个覆盖前一个 | 搜赋值语句，只有一处 |
| layout 嵌套导致控件被 reparent | 读了完整结构，是对的 |

最后给 `QLineEdit.__init__` 打桩数了个数——**一共造了 7 个，死了 2 个**，死的正好是 `card` 里那两个。这才想到去确认它到底有没有被加进父容器。

**教训：`RuntimeError: Internal C++ object already deleted` 只有一个含义——这个对象的所有权链断了。** 别去猜哪行代码删了它，直接查它是不是根本没挂到父控件上。

### 回调存在共享变量里，并发时互相覆盖

```python
self._pending_cb = callback
self._once.emit(data)
```

两个请求同时发出，**后一个把前一个的回调覆盖掉**。前一个的数据回来时，调的是别人的回调。

表现：**「这次打开设置页什么都没有，下次又有了」。**

改成把回调跟着数据一起发出去：

```python
self._once.emit((data, callback))
```

**间歇性的 bug 最难查，因为单发一个请求永远测不出来。** 这类问题的共同点是"有个共享槽位"，一搜就能搜到——`self._pending_xxx`、`self._current_xxx` 这种命名，看到就该警觉。

### 服务商说「等 1 秒」，等 1 秒没用

`kimi-k2.6` 的免费额度是**每分钟 3 次请求**。跑一个任务要好几轮，必然撞上：

```
429 {"error":{"message":"... request reached organization max RPM: 3,
                please try again after 1 seconds"}}
```

第一版照着那个 `after 1 seconds` 等 1 秒——**没用**。因为限制是"每分钟 3 次"，等 1 秒还在同一个窗口里，必然再撞。

```
照它说的等 → 5 次重试全废 → 任务失败
```

改成从错误信息里读出**真实配额**，自己算：

```python
rpm = 3
间隔 = 60 / rpm * 1.2     # 24 秒，多留 20% 余量
```

**服务商告诉你的"等几秒"通常是统一话术，不是真实配额。真实配额得自己从错误信息里挖。**

### 推送发了，用户什么都没收到

日志写着「推了 1 条完成通知」，用户那边一片空白。

推送是把一条事件塞进队列，走**跟正常回复一样的链路**。而 `reply_with_quote` 是全局开关，于是推送也被包了一层引用：

```
MessageChain(chain=[Reply(...), Plain(...)])
```

- QQ 支持引用 → 没事
- **飞书不支持 → 整条消息发送失败**，adapters 报 `Failed to send the message chain`

**同一个开关，在 A 平台无害、在 B 平台致命。**

修了两处：

1. 推送对不支持引用的平台（飞书/Telegram/Slack/Discord）直接发，不走 pipeline
2. 引用守卫插件同时接管飞书的 `send`，把 `Reply` 一律摘掉

**还有第二层问题：** 原来只记"最后说话的那个会话"。在 QQ 说了话，目标就切到 QQ；那之后 QQ 一掉线，**推送彻底发不出去——明明飞书还通着**。

改成每个平台各记一份，发的时候挨个试，哪个活着走哪个。

### 留着一个过时的工具，比没有更糟

问「现在能用哪些模型」，模型调了 `tune_computer_model`——那是 DSH 时代的工具，管的是 codebuddy 三条通道，返回：

```
任务端现在走 codebuddy，模型 deepseek-v4.1-flash
```

**全是早就不存在的信息。** 新加的 `list_computer_models` 没被选中，因为两个工具名字都在讲"模型"，模型分不清哪个是对的。

**直接删掉旧工具。**

工具不是越多越好。**两个功能重叠的工具同时存在，模型选错的概率不是 50%，而是往更"像"的那个偏。** 而它看起来还很合理——所以用户会被误导，还以为是系统出了问题。

### 飞书返回 21001，查配置永远查不出来

飞书 API 全部返回：

```
{"code":21001,"msg":"Access denied due to platform policy."}
```

配置检查了一遍又一遍：App ID 对的、Secret 对的、权限全开了、版本已发布、可用范围没改。**一切正常，就是不通。**

搜下来不关我们的事：

- `2026-10-13` 起陆续有人报同样的错
- 飞书客服统一回复"技术排查中"，有人等了好几天
- 最后有人靠**重新创建一个企业账号**解决
- 标题点名了「豆包账号对自建应用的调用权限被收回」

**抖音/豆包渠道注册的飞书租户被单独限制了。** 换一个纯 `feishu.cn` 注册的企业账号，立刻恢复。

**这类问题的教训：查配置查不出来的时候，先去确认现象是不是别人的问题。** 我们的第一反应永远是"我哪里写错了"，但有些时候答案是"它那边不让你用了"。

---

