# claude-code-strip-injections

关掉 Claude Code 塞进 context 的几样东西——以及更重要的：**下一个版本冒出新的注入时，你怎么自己找出来关掉。**

[English](README.en.md)

本文基于 Claude Code `2.1.175`。你读到时版本大概已经变了，所以这篇的重点不是抄偏移量，是抄方法。

---

## 目录

- [关的是什么](#关的是什么)
- [先读这个：不涉及反编译](#先读这个不涉及反编译)
- [风险与边界](#风险与边界)
- [怎么安全地搜这个文件](#怎么安全地搜这个文件)
- [四个样本](#四个样本)
- [通用方法：新版本冒出新注入怎么办](#通用方法新版本冒出新注入怎么办)
- [怎么验证真的生效](#怎么验证真的生效)
- [脚本用法](#脚本用法)
- [边界与已知事项](#边界与已知事项)

---

## 关的是什么

Claude Code 会往对话里插入一些你没要求的内容，用 `<system-reminder>` 包着发给模型。三类：

**1. 任务提醒**

跑满一定轮数没用过 TodoWrite / Task 工具，就插一段提醒你该建任务列表的话。三段文本分别是 384、128、250 字节。

**2. 文件改动通知（`changed_files`）**

你读过的文件被外部改动后，它会自动重读、算 diff、把文件快照拼进 context。一个大文件能拼出几千 token。

**3. 当前日期（`currentDate`）**

生成 `Today's date is X.`，和 `claudeMd`、`userEmail` 一起拼进**第一条 user message 的 system-reminder**，也就是整个 prompt 的最前排。它在每个进程启动时算一次。

第三个的代价和前两个不是一个量级，但只在特定架构下才踩得到：

如果你的跑法是**每轮都起新进程**（脚本 `claude -p --resume` 那种），跨过零点后第一轮会用新日期重算最前排，**整份 prompt cache 从头作废**。我们实测过一次：`read=0`，215,407 token 全量重写，每天一次。

普通开着终端聊天的人不会踩到，因为进程一直活着。Claude Code 自己的跨天兜底是往**末尾**追加一条 `The date has changed...`，那条不动前缀——是"每轮重启恢复"这种跑法绕开了它。

**如果你不是每轮起新进程的跑法，这一项你可以不关。** 注意 `--only` 是白名单不是排除开关，要跳过它就把另外三个列出来：`--only task-text,task-logic,changed-files`。

---

## 先读这个：不涉及反编译

Claude Code 是 **Bun 打包的 ELF**，不是 Node SEA。判据是搜特征串：`/$bunfs/`、`process.versions.bun`、`bun-build` 全部命中，而 `NODE_SEA_BLOB` / `NODE_SEA_FUSE` 全是 0。

这意味着 **JavaScript 源码以明文嵌在可执行文件里**。变量名被混淆成两三个字母（`BJ`、`_W`、`Qv`），但结构完整、字符串字面量原样躺着。你可以直接搜到这种东西：

```
case"task_reminder":{if(!BJ())return[];
```

所以这篇讲的全部操作是：**在明文里搜一个字符串，把其中几个字节换成等长的另几个字节。** 没有反编译，没有反汇编，因为不需要。

---

## 风险与边界

Anthropic 的 [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) 第 3 节「Use of our Services」列了一串禁止行为，引入句是：

> "You may not access or use, or help another person to access or use, our Services in the following ways:"

其中一条：

> "To decompile, reverse engineer, disassemble, or otherwise reduce our Services to human-readable form, except when these restrictions are prohibited by applicable law."

**我们的理解**是：这条针对的是把服务**还原成**人类可读形式，而这个 binary 的 JavaScript 本来就是明文，不存在"还原"这个动作。

**但这是我们的理解，不是法律意见。** 条款会改，上面抄的是 2026 年 8 月的版本。链接在那里，自己去读原文、自己判断。

技术上这些改动的边界：

- **不碰签名校验**，不碰任何鉴权逻辑
- **不碰网络请求**，不改任何发送给服务器的内容
- 改的主要是**本地进程往 context 里塞什么**（样本 3 有一处例外，见下）
- 不改模型本身，不移除任何工具

### 四个样本的性质不一样

（这里提到的样本编号，具体内容见后面的[四个样本](#四个样本)一节。）

- **样本 1** 是纯文本替换：把一段提示文字换成等长空格，不碰任何代码逻辑。
- **样本 2、3、4** 改的是代码本身——一个条件判断、一个函数调用、一个对象属性。同样是等长字节替换，但改变的是程序的执行路径，比抹掉一段文字深一层。

只想要最保守的那一档，用 `--only task-text`。

样本 3 还有个需要知道的副作用：它同时停掉了"文件被外部改动后自动刷新读取状态"这个动作。Edit / Write / Notebook 各自的 stale-file 保护是另外三处独立逻辑，仍然生效（见文末「边界与已知事项」），但这一项确实动到了工具周边的行为，不只是显示层。

实操上必须守的：

- **必须留备份**。脚本每次写入前自动建带时间戳的备份
- **必须等长替换**。改变文件大小会破坏 ELF 段偏移，binary 直接跑不起来
- **必须验证能启动**。脚本在替换生效前会跑一次 `--version`
- **看不懂就别动**。脚本对三处结构目标做二态检查，状态含糊就拒绝动手（见下文）

---

## 怎么安全地搜这个文件

这是个 249MB 的文件。三条红线，每一条都是实际踩出来的：

**1. 绝不整本读进内存**

`fs.readFileSync(p,'utf8')`、`open(p).read()`、`cat` 全部禁止。

**2. 注意你的 `grep` 是不是你以为的那个**

Claude Code 会往它开的每个 Bash 会话注入 shell function，把 `grep` 劫持成内置的 ugrep、`find` 劫持成 bfs。这个 function 不在任何 rc 文件里，改不掉。`type grep` 可以验。

ugrep 是为"在代码仓库里搜文件"设计的。**从 stdin 吃大流时那些优化全部失效**——撞上 binary 里一行几 KB 的巨行，再叠上 `.{0,80}xxx.{0,150}` 这种带回溯的宽匹配，内存会起飞。

要用就写 `command grep` 或 `/usr/bin/grep`。

**3. `| head -N` 不是防护**

它只截断输出，管不住上游已经吃进内存的部分。

**推荐姿势：Python 分块流式。** 内存上限 = 一个 chunk + overlap，4MB 足够，扫完 249MB 约 0.7 秒：

```python
CHUNK = 4 << 20
M = max(len(p) for p in pats)
found = {p: [] for p in pats}
win, win_off = b'', 0
with open(BIN, 'rb') as f:
    while (c := f.read(CHUNK)):
        win += c
        for p in pats:
            i = 0
            while (j := win.find(p, i)) >= 0:
                found[p].append(win_off + j); i = j + 1
        keep = min(len(win), M - 1)
        win_off += len(win) - keep
        win = win[len(win) - keep:]
```

对照：曾经有一次用 `strings <binary> | grep` 跑了 50 遍，耗时 27 分钟、swap 吃到 4.4G；同一个定位任务用上面这段 0.7 秒。

真要反复搜，就 `strings` **一次落盘**成文本文件，之后都对那个小文件搜，别反复喂原始 binary。

### 搜到的命中不一定是代码

搜任何一句 UI 文案都会命中 **2 处**。搜混淆变量名时，除了真正的代码引用之外，还会**多命中一处符号名表**：

| 位置 | 是什么 | 怎么认 |
|---|---|---|
| 高偏移（~2.35–2.42 亿） | **JS 源码本体** | 前后是可读的 JS，看得到 `case"..."` 和函数体 |
| 低偏移（~1.39 亿） | JSC 字符串常量表 | 夹杂制表符和高位字节，模板插值处是控制字符 |
| 低偏移（~1.05 亿） | 符号名表 | 一长串短标识符挨着排、夹制表符、没有任何 JS 语法 |

**要改行为，只动 JS 源码那一处。** 只想抹掉文案，则前两处都要抹。

符号名表长这样，别把它当引用去数：

```
	vKq  	cI5  	C_6  	FR5  	Rv7  	d5q  	tR7  ...
```

举个实例：搜混淆名 `Rv7` 命中 3 处，其中 1 处就是这张表，真正的代码引用只有 2 处（定义 + 一个默认参数）。引用数因符号而异，**3 不是规律**——要数的是"前后像不像 JS"，不是总数。

---

## 四个样本

按难度递增排。每一个都是 2.1.175 上实际生效的改动。

### 样本 1：抹掉一段文案（最简单）

把整段提醒文本换成等长的空格。

| 目标 | 长度 | 命中 |
|---|---|---|
| `The TodoWrite tool hasn't been used recently...applicable.` | 384 字节 | 2 处 |
| `The task tools haven't been used recently...consider using ` | 128 字节 | 2 处 |
| ` to update task status (set to in_progress when starting...applicable.` | 250 字节 | 2 处 |

task 那条不是一整段字面量，是三段交错的模板（中间夹着 `${...}` 插值和 16 字节的头），所以分段处理。

**这个做法有个坑，值得单独讲**：中间那段 ` to add new tasks and `（22 字节）没被抹，于是剩下这么个东西：

```
<128个空格>TaskCreate to add new tasks and TaskUpdate<250个空格>
```

我们一开始以为模型看不懂这串残渣，等于关掉了。**实际上模型读得懂**，而且会**正当地把它当成 prompt injection 挡掉**——它没有标签、没有来源、不属于任何被读过的文件。有一次派了七个 subagent 干活，其中两个主动报告了这段可疑内容。

**它不是无害的噪音，是在持续消耗注意力并制造假警报。**

结论：只抹文案是不够的，要么整段消失，要么从生成它的地方关掉。下面三个样本都是后者。

### 样本 2：关掉一个判断（改 4 个字节）

```
原:  case"task_reminder":{if(!BJ())return[];
新:  case"task_reminder":{if(!0   )return[];
                              ^^^^  BJ() 这 4 字节 → 0 加 3 个空格
```

`!0` 恒为真，走它自己原本就有的 `return []` 路径。**全文件只差 4 个字节**，JS 里括号内的空格合法。

这一步之后连空格都不会产生——没有残渣，也就没有样本 1 那个问题。

### 样本 3：关掉一个生成器（改 6 个字节）

```
原:  y3("changed_files",()=>Xc4(w))
新:  y3("changed_files",()=>[]    )
```

collector 返回空数组。这会从生成端同时关掉 `edited_text_file` 和 `edited_image_file`：不再隐藏地自动重读文件、算 diff、刷新读取状态，也不再生成几千 token 的文件快照。

**为什么不是只抹文案**：renderer 后面还会拼 `${H.snippet}`，抹掉固定句子会留下整份文件 diff；而在生成器里只丢掉 text attachment，那些隐藏的重读和副作用照样执行。返回空数组是最窄的完整关闭。

### 样本 4：删掉一个对象属性（改 22 个字节）

```
原:  ..._&&{attachedProject:_},currentDate:T07(yPH())}
新:  ..._&&{attachedProject:_},/*date-disabled*/     }
```

JS 对象字面量允许尾逗号，所以这个属性直接消失，不留语法残迹。

`/*date-disabled*/` 是 17 字节的注释，剩下 5 个空格补齐 22 字节。**这个标记是故意留的**——脚本靠它认出"这个位置已经处理过了"，重复运行时不会再动手。

**为什么不填一个固定的假日期**：会重演样本 1 的坑。binary 里剩下的残废字符串会被模型当成 prompt injection 正当地挡掉，还制造假警报。要么整段消失，要么别动。

---

## 通用方法：新版本冒出新注入怎么办

上面四个是 2.1.175 的。这一节才是这个仓库真正想给你的东西。

### 第 1 步：确认它是怎么进来的

Claude Code 往 prompt 里塞东西走 attachment 机制。**会话记录（jsonl）里存的是结构化事件，不是文本**：

```json
{"type":"attachment","attachment":{"type":"task_reminder","content":[],"itemCount":0}}
```

真正的文本是**发请求那一刻现拼的**：`case"<type>":` 分支 → 构造 user 消息 → 包上 `<system-reminder>` → 进 API 请求体，拼完不回头写盘。

**运行时实际观测到过的**：`task_reminder`、`command_permissions`、`deferred_tools_delta`、`mcp_instructions_delta`、`skill_listing`、`edited_text_file`。

**代码里存在、但我们没实际观测到的**：`relevant_memories`、`diagnostics`、`queued_command`、`plan_mode_reentry`。

**这条对判断"我是不是记错了"很关键**：在 jsonl 里搜不到某句话的文本，不能证明它没出现过——只能说明它没以文本形式存过盘。要查就查有没有对应的 **attachment 记录**。

### 第 2 步：找一个不会被混淆的锚点

**混淆名每个版本都变。** 同一个位置的同一个函数，一个版本叫 `nw()`，下一个版本叫 `BJ()`。照抄旧笔记里的名字会改错字节。

不会变的东西有两类：

- **字符串字面量**：`case"task_reminder":`、`"changed_files"`
- **对象属性名**：`currentDate:`

拿这些当锚点，把混淆名写成正则的通配部分：

```python
# 不要写死 BJ
TASK_LOGIC_LIVE = re.compile(
    rb'case"task_reminder":\{if\(!(?P<target>[A-Za-z_$][\w$]*\(\))\)return\[\];'
)
```

`(?P<target>...)` 捕获的就是要替换的那段，长度从匹配结果里读，不用手工数。

### 第 3 步：确认作用域，别改一个数崩三个功能

常量名是全局唯一的，但一个常量可能喂给好几个功能。改之前把引用枚举清楚：

搜常量名 → 排掉符号名表那一处 → 剩下的才是真引用 → 对每个引用点看它被谁调用 → 确认全都在你想改的路径上 → 才动手。

少这一步就可能改一个数、崩三个功能。

### 第 4 步：选一种等长改法

改完文件大小必须一字节不差。三种技巧：

| 场景 | 做法 | 例子 |
|---|---|---|
| 抹掉文本 | 换成等长空格 | 384 字节文案 → 384 个空格 |
| 关掉条件 | `!X()` → `!0` + 空格补齐 | `if(!BJ())` → `if(!0   )` |
| 改数量级 | 科学计数法只动一个字节 | `Rv7=1e4` → `Rv7=1e5` |

第三种是白送的：`1e4` 改成 `1e5` 就是 10000 变 100000，两边都是 3 字节。

遇到 `50000` 这种十进制写法，可以换成 `5e4` 再补空格——`50000` 是 5 字节、`5e4` 是 3 字节，**要补 2 个空格**。差多少补多少，每次都数一遍再写，别照抄别人的数字。

### 第 5 步：二态检查，看不懂就拒绝动手

这是整套方法里最该抄的一条。

对每个改动定义两个正则：**live**（原始形态）和 **disabled**（改过的形态）。扫完要求两者的命中数恰好是 `(1, 0)` 或 `(0, 1)`。

- `(1, 0)` = 还没改，可以改
- `(0, 1)` = 已经改过，跳过
- **其他任何情况 = 我看不懂这个 binary，拒绝动手**

这不是理论上的谨慎。写这篇时验证脚本，我们拿一份自认为"未处理过"的备份来测，脚本当场报错：

```
RuntimeError: 2.1.175: currentDate gate is ambiguous (0, 0); inspect this version manually
```

去看那个位置，实际 dump 出来是这样（结尾的 `}}` 里外层那个属于更外面的结构，不是笔误）：

```
..._&&{attachedProject:_},                      }}
```

那份备份其实**早就被一个更早的版本处理过**——那一版直接抹成空格、没留标记。所以它既不是原始形态，也不是当前脚本认识的形态。脚本正确地拒绝了。

如果没有这道检查，脚本会认为"没找到 live 形态"就是"不用改"，或者更糟——在错误的位置写字节。

### 第 6 步：改副本，原子替换

任何时刻机器上都可能有几十个进程持有这个文件。而且 Linux 不允许写正在执行的文件（`ETXTBSY`）。

```bash
cd ~/.local/share/claude/versions
cp -p <ver> <ver>.bak-orig      # 备份
cp -p <ver> <ver>.tmp           # 改副本，绝不原地改
python3 your_patch_script.py    # 改 .tmp，带前后逐字校验
./<ver>.tmp --version           # 先确认改过的能启动
mv <ver>.tmp <ver>              # 原子换 inode
```

中间文件用 `.tmp` 结尾不是随便挑的：`strip_injections.py` 会跳过这个后缀。如果你用 `.new` 之类的名字、中途放弃又留下了残留文件，下次跑脚本时它会被当成一个正式 binary 一起处理。

`mv` 在同一文件系统上是 rename：**已经在跑的进程继续用旧 inode、完全不受影响，新起的进程才吃到新的**。正在干活的窗口一个都不会被打断。

代价是它们要重启才生效——**patch 完当前窗口仍然冒出提醒是正常的，不是没生效**。

---

## 怎么验证真的生效

### 会话记录里测不出来

attachment 在 jsonl 里只存结构化对象，渲染后的文本不落盘。处理前后的 jsonl 长得一模一样。

### 能启动 ≠ 没搞坏

`--version` 只证明 ELF 还能加载。要验的是跑一个完整的多轮会话不出错。

### 唯一可行的观测口子：问跑测试的那个会话自己看到了什么

它看得见自己的 context。但**必须做 A/B 对照**：原版和处理过的版本各跑一遍同样的 prompt。只测处理过的那个、得到"我没看到"，是假阴性——模型也可能只是没提。

### 注意触发条件

任务提醒要跑满 10 轮工具调用才会出现（阈值是"距上次写入 10 轮"加"两次提醒间隔 10 轮"）。测试用的 prompt 要设计成保证 12 次以上独立工具调用，否则你测的是"它本来就不该出现"。

### 构造可观测的差异，比 A/B 更省事

如果改的是行为阈值而不是文案，可以直接构造超过阈值的输入看结果。比如改 hook 输出上限时，在输出的首尾各埋一个标记串，然后问会话"你看到哪个了"：处理前只剩开头的（尾部被截断），处理后首尾都在。

---

## 脚本用法

`strip_injections.py` 把上面四个样本做成了可重复运行的脚本。

**要求 Python 3.10 或更高**（用到了 `X | None` 形式的类型注解，3.9 及以下会直接导入失败）。纯标准库，没有第三方依赖。

```bash
# 先看会改什么，不写任何东西
python3 strip_injections.py --dry-run

# 全部应用
python3 strip_injections.py

# 只关日期，其他三个不动
python3 strip_injections.py --only current-date

# 逗号分隔可以选多个
python3 strip_injections.py --only task-logic,current-date

# 装在别处
python3 strip_injections.py --versions-dir /path/to/claude/versions
```

四个名字：`task-text`、`task-logic`、`changed-files`、`current-date`。

完整流程：

4MB 流式扫描（不整本读）→ 二态检查（含糊就拒绝）→ 建带时间戳的备份 → 复制成临时文件 → 每处写入前先断言等长、**再比对该位置的原字节** → 校验总大小不变 → 跑 `--version` → 原子替换 inode → **替换后重新全量扫描复验**。

其中两步值得单独说：

**"比对原字节"是最重要的一道闸。** 等长只保证文件大小不变，比对原字节才保证你写对了**位置**——偏移算错时它会当场中止，而不是往一个错误的地方写进一段长度合法的垃圾。

顺带一句：等长那道检查用的是 Python 的 `assert`，而 `assert` 在 `python3 -O` 下会被整个剥掉。**别用 `-O` 跑这个脚本。**

**最后那次复验发生在替换之后。** 如果复验不通过，脚本会抛异常，但新文件已经就位了。这种情况下回滚要靠它刚才建的那个备份。

跳过规则：名字里含 `.bak` 或 `.original`、以 `.tmp` 结尾、或以点开头的文件都不处理——**备份是你的回滚点，不能被一起改掉。** 注意 `.tmp` 是后缀匹配，`foo.tmp.bin` 这样的名字不会被跳过。

（脚本自己产生的临时文件叫 `.<原文件名>.patch-<pid>`，以点开头，靠上面第三条被跳过。`.tmp` 那一条是留给你手工操作时用的。）

`--dry-run` 不写任何东西，但二态检查照跑，所以它也可能直接报 `gate is ambiguous` 然后退出。那是预期行为，不是 dry-run 坏了——它在告诉你这个 binary 的状态它看不懂。

重复运行安全：已经处理过的会报 `all selected patches already applied`。

**每次 Claude Code 版本变动（升级、回退、重装）之后都要重新跑一次。** 我们曾经在一次回退后忘了跑，被任务提醒骚扰了三周才发现。

如果你像我们一样关掉了自动更新，平时就不用管它。

---

## 边界与已知事项

**stale-file guard 没有被改。** 样本 3 关掉的是文件改动**通知**。Edit / Write / Notebook 各自的"文件在你读过之后被修改"保护是另外三处独立逻辑，仍然生效：读过一个文件、外部改动它、再调用 Edit，仍然会被拒绝并要求重读。

**残余风险**：用 Bash / sed / Python 写文件时，模型不会主动知道外部改动。不过 shell 写入本来就绕过 stale guard，跟这个改动无关——干活时在写之前读一下当前文件，是任何情况下都该有的习惯。

**`--skip-version-check` 不要在正常使用中开。** 它存在的唯一理由是拿假样本测试脚本本身（假文件不能执行，`--version` 必然失败）。开了它就等于放弃"改完还能不能启动"这道检查。

**`--only task-text` 单独使用时**，二态检查仍然会对全部三处结构目标运行。这是故意的：那道检查回答的是"我看不看得懂这个 binary"，不是"我要不要改这里"。

**版本号会过时。** 本文所有偏移量、混淆名（`BJ`、`Xc4`、`T07`、`yPH`）都只属于 2.1.175，仅供理解用。脚本按结构定位，不依赖它们。

---

## License

MIT
