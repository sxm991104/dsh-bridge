# dsh-bridge

让 QQ 那边的 AI 能把活派给电脑上另一个自己，干完再回来说一声。

**这不是一个「聊天机器人框架」。** 它解决的是一个很具体的问题：

> 你在外面，用手机 QQ 让 AI 帮忙改点东西。但它只在 QQ 里，没有终端、没有文件系统，什么都动不了。

这个桥就是那条线：QQ 端把事写下来 → 电脑端接过来干 → 干完把结果送回去。

---

## 它长什么样

```
        你在QQ里说事
              │
              ▼
    ┌──────────────────┐
    │  QQ 端的 AI       │  跟你确认清楚要做什么
    │  (AstrBot 插件)   │  然后写成一条任务
    └────────┬─────────┘
             │  dispatch（0.2 秒返回，不等）
             ▼
    ┌──────────────────┐
    │  tasks.jsonl     │  任务清单
    └────────┬─────────┘
             │  后台进程去跑
             ▼
    ┌──────────────────┐
    │  电脑端的 AI      │  dsh qqme "任务"，带工具、带人格
    │  (dsh headless)   │  真去改文件、跑命令
    └────────┬─────────┘
             │  写出结果 + 完成通知
             ▼
    ┌──────────────────┐
    │  outbox.jsonl    │  完成箱
    └────────┬─────────┘
             │  插件盯这个文件
             ▼
        主动推给你
```

---

## 为什么这么设计

### 一、派活必须「不等」

最早写的是同步等结果。**真干活要几分钟，而 QQ 那边等不了那么久。**

结果就是：电脑端明明 194 秒跑完了、成功了，QQ 端 2 分钟就断了，只能说一句"超时"。

**更糟的是分不清"还在跑"和"卡死了"** —— 两个都叫超时。

所以拆成两个动作：
- `dispatch` 写一条就返回，0.2 秒
- 后台进程慢慢跑，跑多久都行

### 二、要有一个「同一个窗口」

每次 `dsh qqme "任务"` 都是新进程。**新进程不知道上次干了什么，等于每次都是第一次见你。**

`headless` 支持 `--session-id <id>`，文档写的是 **adopt**（接手已存在的会话），不是新建。

实测：
```
进程 A：记住暗号「鲸鱼七号」
进程 B（完全新开）：问暗号 → 「鲸鱼七号」
```

**换进程，但窗口是同一个。** 会话号存在 `.session_id`，第一次跑的时候从事件流里抓出来。

### 三、心跳要说清「在做什么」

只报"还在跑"没用 —— 用户看不到它在干什么，跑错了也没法及时收住。

所以心跳（`.progress.json`）里带了 `doing`：从 `--json` 事件流的 `tool_call` 里取最后一个工具调用。

```
正在跑，已 20 秒（心跳 3 秒前）
在做：pwsh: Get-PSDrive C（check disk space）
```

### 四、任务要有「交差规则」

派过一次"查资料"的活，**跑了 28 分钟还没完。**

查事件流才发现：核心结论十分钟就拿到了，后面全在追新冒出来的相关论文（查"压缩摘要"追到 YaRN，从 YaRN 追到 Infini-attention），遇到 429 限流还一遍遍重试。

**每一步都合理，合起来无底洞。** 因为"调研"这类任务没有天然终点：写代码跑通了就是通了；查资料永远能再查一个。

所以派活时会自动附加三条规则（不改任务内容，只加判断标准）：
1. **能回答了就交** —— 手上的东西够了就停，别去找"更全面的资料"
2. **拿不到就记下来、停** —— 重试不超过两次，失败也要报
3. **别扩大范围** —— 查到别的东西记一句就够，别顺着往下查

---

## 装之前，先看这几条

**这玩意儿只在 Windows 上跑过。** 里面有一半代码是为了绕开 Windows 的坑。

**你需要：**
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh` 命令能跑）
- 一个能跑 `dsh headless` 的 profile（下面讲怎么配）
- Python 3.10+

**你还需要知道：**
- 它默认给电脑端**全权限**（`danger-full-access`），跟桌面版同一个权限级别
- 所以「能看到它在干什么」和「能随时停掉」不是可选项，是必须的

---

## 配置

### 1. 准备一个 headless profile

在 `$DSH_HOME/profiles/` 下建一个目录（比如 `qqme`），放一个 `cordis.patch.yml`：

```yaml
# 用官方 API key 通道（按量付费，支持图片）
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: deepseek-official
    model: deepseek-flash

# 人格（可选，但强烈建议写——不然它就是个没有性格的执行器）
- id: system-prompt
  config:
    personaPrefix: |
      你是……（写你想要的样子）
```

**为什么用 `deepseek-official` 而不是别的通道：**

- 有每日额度上限的通道，超了任务就哑了，而人在外面时没法充值
- 有些代理通道是纯文本的，会直接拒绝图片（见下面「踩过的坑」）

### 2. 设置环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 官方 API key | 从 `$DSH_HOME/.credentials.yaml` 读 |
| `DSH_HOME` | DSH 数据目录 | `~/.dsh` |
| `DSH_CMD` | dsh 可执行文件路径 | 从 PATH 找 |
| `DSH_PROFILE_FILE` | qqme 的 patch 文件 | `$DSH_HOME/profiles/qqme/cordis.patch.yml` |

`bridge.py` 会自己从 `$DSH_HOME/.credentials.yaml` 里捞 `DEEPSEEK_API_KEY`，**不用把 key 写进代码。**

---

## 用起来

```bash
# 派一条任务，立刻返回
python bridge.py dispatch "在 D:\Project\demo 下建个 README.md，写三行简介"

# 真干活（一般在后台调）
python bridge.py run --id t-20261027120000

# 看进展
python bridge.py live
# → {"running_tasks": [...], "progress": {"doing": "write_file: README.md", ...}}

# 看某个任务
python bridge.py status --id t-20261027120000

# 停掉卡住的
python bridge.py cancel --id t-20261027120000

# 看它卡在哪儿（拿不到东西时会写这里）
python bridge.py ask

# 调模型和推理强度
python tune_model.py show
python tune_model.py set --model deepseek-flash --effort high

# 给项目加 git 防护（别让 AI 的草稿进仓库）
python guard_git.py list
python guard_git.py apply D:\Project\某个项目
```

**任务状态：**

| 状态 | 含义 |
|---|---|
| `pending` | 排队中 |
| `running` | 正在跑（心跳里有 `doing`） |
| `ok` | 做完了，有结果 |
| `empty` | 跑完了但没输出 |
| `error` | 出错 |
| `timeout` | 超时，进程已连根杀掉 |
| `cancelled` | 手动停的 |

---

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

---

## 已知限制

- **只在 Windows 上验证过。** Linux/macOS 应该能跑，但沙箱那部分行为不一样。
- **`ask.jsonl` 不一定写得出来。** 规则里要求它卡住时写一行，实测模型经常不写，而是把失败直接写在答案里。通道留着，但不强求。
- **依赖 dsh 的内部机制**（`DSH_PERMISSION_MODE`、`--session-id`、`--json` 事件格式）。dsh 升级可能失效。
- **没有鉴权。** 这是本机工具，别暴露到公网。

---

## 关于开源

这份代码里**大部分注释写的不是"这行做什么"，而是"为什么不能那样写"**。

因为那些坑每一个都花了不少时间，其中几个反复改了三四轮才找对。

**如果你要用它，直接抄走没关系。但如果你要改它，麻烦先看一眼注释里那些"哪里错了"——省下的是你自己的时间。**

---

## License

MIT
