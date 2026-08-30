# Sample output / 真实输出样例

All output below was captured on a real 249MB Claude Code 2.1.175 binary — a **copy** placed in a scratch directory, never the installed one.

以下全部是在真实的 249MB Claude Code 2.1.175 上跑出来的输出。跑的是**副本**，放在临时目录里，不是正在用的那个。

---

## 1. Dry run, nothing applied yet / 未处理状态

```
$ python3 strip_injections.py --dry-run --versions-dir /tmp/scratch/versions
selected patches: task-text, task-logic, changed-files, current-date
-> 2.1.175:
  task reminder (full): 0 live
  task reminder (prefix): 0 live
  task reminder (suffix): 0 live
  task logic:    0 live / 1 disabled
  current date:  1 live / 0 disabled
  changed files: 1 live / 0 disabled
  DRY RUN: would apply 2 equal-length writes
```

Note the mixed state: `task logic` was already disabled from an earlier run, so only two patches remain. `0 live / 1 disabled` and `1 live / 0 disabled` are both valid — see the gate check below.

注意这是个混合状态：`task logic` 之前就处理过了，所以只剩两处要改。`0 live / 1 disabled` 和 `1 live / 0 disabled` 都是合法的。

---

## 2. Applying / 实际执行

```
$ python3 strip_injections.py --versions-dir /tmp/scratch/versions
selected patches: task-text, task-logic, changed-files, current-date
-> 2.1.175:
  task reminder (full): 0 live
  task reminder (prefix): 0 live
  task reminder (suffix): 0 live
  task logic:    0 live / 1 disabled
  current date:  1 live / 0 disabled
  changed files: 1 live / 0 disabled
  applied 2 equal-length writes
  backup: /tmp/scratch/versions/2.1.175.bak-pre-patch-20260830-214829
  size unchanged; --version passed; inode replaced atomically
```

Wall time for the whole thing, including two full scans of the 249MB file, two 250MB copies, and launching the patched binary: **3.65 seconds**.

整个过程 **3.65 秒**，包含两次全文件扫描、两次 250MB 复制、以及启动一次处理后的 binary。

---

## 3. Running again — idempotent / 重复运行

```
$ python3 strip_injections.py --dry-run --versions-dir /tmp/scratch/versions
selected patches: task-text, task-logic, changed-files, current-date
-> 2.1.175:
  task reminder (full): 0 live
  task reminder (prefix): 0 live
  task reminder (suffix): 0 live
  task logic:    0 live / 1 disabled
  current date:  0 live / 1 disabled
  changed files: 0 live / 1 disabled
  all selected patches already applied
```

---

## 4. Selecting a subset / 只处理其中一项

```
$ python3 strip_injections.py --dry-run --only current-date --versions-dir /tmp/scratch/versions
selected patches: current-date
-> 2.1.175:
  task reminder (full): 0 live  (not selected)
  task reminder (prefix): 0 live  (not selected)
  task reminder (suffix): 0 live  (not selected)
  task logic:    0 live / 1 disabled  (not selected)
  current date:  0 live / 1 disabled
  changed files: 0 live / 1 disabled  (not selected)
  all selected patches already applied
```

The gate check still ran against all three structural targets, even though only one was selected. That check answers "do I understand this binary", not "should I patch this".

即使只选了一项，二态检查仍然对三处结构目标全部运行。那道检查回答的是"我看不看得懂这个 binary"，不是"我要不要改这里"。

---

## 5. The gate refusing to act / 闸门拒绝动手

This is a real failure, not a constructed one. The binary in question had been patched by a much older version of this tooling — one that blanked the `currentDate` span straight to spaces without leaving a marker. So it matched neither the live form nor the disabled form:

这是一次真实的失败，不是造出来的。那个 binary 曾被更早一版工具处理过——那一版直接把 `currentDate` 抹成空格、没留标记。所以它既不匹配 live 形态，也不匹配 disabled 形态：

```
$ python3 strip_injections.py --dry-run --versions-dir /tmp/scratch/versions
selected patches: task-text, task-logic, changed-files, current-date
-> 2.1.175:
Traceback (most recent call last):
  ...
RuntimeError: 2.1.175: currentDate gate is ambiguous (0, 0); inspect this version manually
```

Inspecting that offset showed the leftover:

去看那个偏移，留下的痕迹是：

```
..._&&{attachedProject:_},                      }}
```

Exactly 22 spaces — the length of `currentDate:T07(yPH())`.

正好 22 个空格，也就是 `currentDate:T07(yPH())` 的长度。

**This is the gate working as intended.** Without it, the script would have either silently done nothing, or written bytes to a position it did not actually understand.

**这就是闸门该有的行为。** 没有它，脚本要么悄悄什么都不做，要么往一个它其实没看懂的位置写字节。
