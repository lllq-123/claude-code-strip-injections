# Sample output / 真实输出样例

All output below was captured on the official Claude Code 2.1.280 linux-x64 binary (sha256 `1e08503d…`, 233,709,640 bytes) — a **copy** placed in a scratch directory, never the installed one. Section 7 is kept from the first release, which was captured on 2.1.175.

以下全部是在官方 Claude Code 2.1.280 linux-x64 原件（sha256 `1e08503d…`，233,709,640 字节）上跑出来的输出。跑的是**副本**，放在临时目录里，不是正在用的那个。第 7 节保留自第一版，是在 2.1.175 上跑的。

---

## 1. Dry run on an untouched binary / 未处理状态

```
$ python3 strip_injections.py --dry-run --versions-dir /tmp/scratch/versions
selected patches: task-text, task-logic, changed-files, current-date, user-email
note: live/disabled describe the JS source text only. '@ module #N (bytecode)'
      means that text is not what runs -- see the README section on bytecode.
-> 2.1.280:
  task reminder (full): 2 live  @ module #333 (bytecode) + 1 outside JS source
  task reminder (prefix): 2 live  @ module #333 (bytecode) + 1 outside JS source
  task reminder (suffix): 2 live  @ module #333 (bytecode) + 1 outside JS source
  task logic:    1 live / 0 disabled  @ module #333 (bytecode)
  current date:  1 live / 0 disabled  @ module #333 (bytecode)
  changed files: 1 live / 0 disabled  @ module #333 (bytecode)
  user email:    1 live / 0 disabled  @ module #333 (bytecode)
  pronouns:      1 live / 0 disabled  (not selected)  @ module #333 (bytecode)
  ! bytecode: task-logic, current-date, changed-files, user-email
    sit in a module that runs from bytecode, so editing their source text
    has no effect at runtime. See --source-fallback.
  DRY RUN: would apply 10 equal-length writes
```

Every code patch lands in module #333, and that module runs from precompiled bytecode. Without `--source-fallback` the script would still write these bytes — and nothing would change at runtime. `pronouns` is found but not selected: it is only applied when named with `--only`.

所有改代码的补丁都落在 333 号模块里，而这个模块走预编译字节码。不加 `--source-fallback`，脚本照样会写这些字节——但运行时什么都不会变。`pronouns` 找到了但没选：它只在 `--only` 点名时才打。

---

## 2. The previous version of this script, same binary / 旧版脚本跑同一个文件

```
$ python3 strip_injections.py --dry-run --versions-dir /tmp/scratch/versions
selected patches: task-text, task-logic, changed-files, current-date
-> 2.1.280:
Traceback (most recent call last):
  ...
RuntimeError: 2.1.280: currentDate gate is ambiguous (0, 0); inspect this version manually
```

Since 2.1.258 the `currentDate` field is written as a template literal, which the old pattern did not recognise. The gate refused to act — correctly.

2.1.258 起 `currentDate` 那段改成了模板字面量，旧正则认不出来。二态检查拒绝动手——这是它该有的行为。

---

## 3. Applying, with the module switched to source / 实际执行，并让模块走源码

```
$ python3 strip_injections.py --source-fallback --versions-dir /tmp/scratch/versions
selected patches: task-text, task-logic, changed-files, current-date, user-email
note: live/disabled describe the JS source text only. '@ module #N (bytecode)'
      means that text is not what runs -- see the README section on bytecode.
-> 2.1.280:
  task reminder (full): 2 live  @ module #333 (bytecode) + 1 outside JS source
  task reminder (prefix): 2 live  @ module #333 (bytecode) + 1 outside JS source
  task reminder (suffix): 2 live  @ module #333 (bytecode) + 1 outside JS source
  task logic:    1 live / 0 disabled  @ module #333 (bytecode)
  current date:  1 live / 0 disabled  @ module #333 (bytecode)
  changed files: 1 live / 0 disabled  @ module #333 (bytecode)
  user email:    1 live / 0 disabled  @ module #333 (bytecode)
  pronouns:      1 live / 0 disabled  (not selected)  @ module #333 (bytecode)
  source fallback: module #333 runs from bytecode; will clear 16 bytes of its directory record
  applied 11 equal-length writes
  backup: /tmp/scratch/versions/2.1.280.bak-pre-patch-20260928-132228
  size unchanged; --version passed; inode replaced atomically
```

Eleven writes: ten patches plus the 16-byte directory-record change. Wall time for the whole thing, including two full scans, two 234MB copies and launching the patched binary: **3.76 seconds**.

11 处写入：10 处补丁，加上模块目录记录那 16 个字节。整个过程 **3.76 秒**，包含两次全文件扫描、两次 234MB 复制、以及启动一次处理后的 binary。

Whether it actually took effect is not something this output can tell you — see the request-body comparison in the README (「怎么验证真的生效」/ "How to verify it actually took effect").

改完到底生没生效，这份输出回答不了——见 README 里那张请求体对照表。

---

## 4. Running again — idempotent / 重复运行

```
$ python3 strip_injections.py --dry-run --source-fallback --versions-dir /tmp/scratch/versions
selected patches: task-text, task-logic, changed-files, current-date, user-email
note: live/disabled describe the JS source text only. '@ module #N (bytecode)'
      means that text is not what runs -- see the README section on bytecode.
-> 2.1.280:
  task reminder (full): 0 live
  task reminder (prefix): 0 live
  task reminder (suffix): 0 live
  task logic:    0 live / 1 disabled  @ module #333 (source)
  current date:  0 live / 1 disabled  @ module #333 (source)
  changed files: 0 live / 1 disabled  @ module #333 (source)
  user email:    0 live / 1 disabled  @ module #333 (source)
  pronouns:      1 live / 0 disabled  (not selected)  @ module #333 (source)
  source fallback: module #333 already runs from source
  all selected patches already applied
```

Module #333 now reads `(source)`.

333 号模块现在显示 `(source)`。

---

## 5. Selecting a subset / 只处理其中一项

```
$ python3 strip_injections.py --dry-run --only current-date --versions-dir /tmp/scratch/versions
selected patches: current-date
note: live/disabled describe the JS source text only. '@ module #N (bytecode)'
      means that text is not what runs -- see the README section on bytecode.
-> 2.1.280:
  task reminder (full): 0 live  (not selected)
  task reminder (prefix): 0 live  (not selected)
  task reminder (suffix): 0 live  (not selected)
  task logic:    0 live / 1 disabled  (not selected)  @ module #333 (source)
  current date:  0 live / 1 disabled  @ module #333 (source)
  changed files: 0 live / 1 disabled  (not selected)  @ module #333 (source)
  user email:    0 live / 1 disabled  (not selected)  @ module #333 (source)
  pronouns:      1 live / 0 disabled  (not selected)  @ module #333 (source)
  all selected patches already applied
```

The gate check still ran against the three original structural targets, even though only one was selected. That check answers "do I understand this binary", not "should I patch this".

即使只选了一项，二态检查仍然对原来那三处结构目标全部运行。那道检查回答的是"我看不看得懂这个 binary"，不是"我要不要改这里"。

---

## 6. `--source-fallback` refusing to act / 回退拒绝动手

An untouched copy; first only `pronouns` is applied (no fallback, so it has no effect yet). Then a fallback is requested for the default set, which does not include `pronouns`:

一份没动过的副本，先只打 `pronouns`（没回退，所以还没生效）。然后对默认那一组要求回退，而默认那一组不含 `pronouns`：

```
$ python3 strip_injections.py --only pronouns --versions-dir /tmp/scratch/other
  ...
  pronouns:      1 live / 0 disabled  @ module #333 (bytecode)
  ! bytecode: pronouns
    sit in a module that runs from bytecode, so editing their source text
    has no effect at runtime. See --source-fallback.
  applied 1 equal-length write
  ...

$ python3 strip_injections.py --dry-run --source-fallback --versions-dir /tmp/scratch/other
  ...
  pronouns:      0 live / 1 disabled  (not selected)  @ module #333 (bytecode)
Traceback (most recent call last):
  ...
RuntimeError: 2.1.280: module #333 also contains the pronouns patch from an earlier run, which is not selected now. Falling back would make that edit take effect too. Add pronouns to --only, or start again from an unpatched binary.
```

Falling back switches the whole module, so an earlier edit that was sitting there inert would suddenly start working. The script will not do that behind your back.

回退切换的是整个模块，一个早先打过、一直没生效的改动会突然开始起作用。脚本不会背着你这么做。

---

## 7. The gate refusing to act (2.1.175) / 闸门拒绝动手（2.1.175）

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
