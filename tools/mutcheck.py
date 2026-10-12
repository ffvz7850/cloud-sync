# -*- coding: utf-8 -*-
"""反向验证（变异测试）：把关键分支改坏，确认测试**真的有能力**变红。

「测试全绿」本身不构成证据 —— 必须证明测试能发现回归。每条变异都应当让
指定命令失败；若某条变异后测试仍然全绿，说明那条断言是空的（假绿）。

跑法（在工程根目录执行）：
    python tools/mutcheck.py               # 跑全部变异，约 10 分钟
    python tools/mutcheck.py --anchors     # 只体检锚点，秒级
    python tools/mutcheck.py --only 关键词  # 只跑说明里含关键词的变异（调试用）
可选的路径覆盖（换机器/换环境时用）：
    DD_PY=/usr/bin/python3 DD_NODE=/usr/bin/node DD_NODE_PATH=... \\
        python tools/mutcheck.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))      # 本脚本在 tools/ 下，工程根在上一级

# 被改坏的文件的原件**不能放在工程目录里**（会被打包脚本收走）。每次开跑前把
# 所有目标文件各存一份；跑到最后写一个"成功戳"。于是
#   「备份比成功戳新」= 上一轮没跑完 → 启动时自动还原。
# 为什么不放在工程内：万一某次中断把变异态留在源码里，那份坏文件会被打进
# fpk / src.zip。放临时目录既躲开打包，也不给工程目录添垃圾。
# 为什么不用"跑完就删备份"来判断：删除在部分受限环境下会被拦掉，于是一个已经
# 全绿的 10 分钟长跑最后以非 0 退出、汇总行也不打印 —— 看起来像失败。写文件不会。
# 曾经真的踩过：还原只写在 `finally` 里，进程被打断就留在源码里（两次）。
BAKDIR = os.environ.get("DD_MUTBAK") or os.path.join(
    tempfile.gettempdir(), "doubao-drive-mutbak")
STAMP = os.path.join(BAKDIR, "last-success")

PY = os.environ.get("DD_PY") or sys.executable


def _find_node():
    """与 tests/run_all.py 用同一套探测逻辑，保证两边跑的是同一个 node。

    直接复用而不是各写一份：接手方换环境时只需改一处。
    """
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    try:
        from run_all import _find_node as impl
        return impl()
    except Exception:
        return shutil.which("node")


def _node_modules():
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    try:
        from run_all import _node_modules as impl
        return impl()
    except Exception:
        return os.environ.get("NODE_PATH", "")


NODE = os.environ.get("DD_NODE") or _find_node()
NODE_MODULES = os.environ.get("DD_NODE_PATH") or _node_modules()

ENV = dict(os.environ)
if NODE_MODULES:
    ENV["NODE_PATH"] = NODE_MODULES
ENV["PYTHONIOENCODING"] = "utf-8"

PY_TEST = lambda name: [PY, "tests/" + name]          # noqa: E731
JS_TEST = lambda name: ([NODE, "tests/" + name] if NODE else None)  # noqa: E731

# (说明, 目标文件, 原文, 替换成, 应当变红的测试)
MUTATIONS = [
    # ---- 双向差异 ----
    ("云端多出的文件不再上报（「仅云端」永远是空的）", "app/server/verify.py",
     '        "cloudExtra": cloud_extra,', '        "cloudExtra": [],',
     PY_TEST("test_verify.py")),
    ("比对不归并父子项（批量删除会把同一子树删两遍）", "app/server/verify.py",
     '    local_extra = _prune_descendants(result.get("notUploaded") or [])',
     '    local_extra = list(result.get("notUploaded") or [])',
     PY_TEST("test_verify.py")),
    ("比对不再剔除 .ddpart 临时文件", "app/server/verify.py",
     "    for extra in ALWAYS_EXCLUDES:\n"
     "        if extra not in out:\n"
     "            out.append(extra)\n"
     "    return out",
     "    return out",
     PY_TEST("test_verify.py")),

    # ---- 下载 ----
    ("下载不校验大小（半截文件当成功）", "app/server/openlist.py",
     "        if want and written != want:", "        if False:",
     PY_TEST("test_download.py")),
    ("下载忽略 206/200（忽略 Range 时会把全量内容追加到半截文件后面）",
     "app/server/openlist.py",
     "            if resp.status == 206 and done:", "            if False:",
     PY_TEST("test_download.py")),
    ("跨源直链也带上 token（凭据泄露给第三方）", "app/server/openlist.py",
     "            if self._same_origin(url):", "            if True:",
     PY_TEST("test_download.py")),
    ("raw_url 相对地址不再用 base_url 补全", "app/server/openlist.py",
     '            return self.base_url().rstrip("/") + url', "            return url",
     PY_TEST("test_download.py")),
    ("下载不写临时文件，直接写目标路径", "app/server/openlist.py",
     "        part = local_path + \".ddpart\"", "        part = local_path",
     PY_TEST("test_download.py")),

    # ---- 删除 ----
    ("本地删除不再拦白名单根目录（可能 rmtree('/vol1')）", "app/server/http_api.py",
     "            if os.path.normcase(target) in root_norms:",
     "            if False:",
     PY_TEST("test_api.py")),
    ("越界的本地删除不再被拒", "app/server/http_api.py",
     "            if not is_path_allowed(target, roots):\n"
     '                failed.append({"path": raw, "error": "路径不在允许访问的范围内"})',
     "            if False:\n"
     '                failed.append({"path": raw, "error": "路径不在允许访问的范围内"})',
     PY_TEST("test_api.py")),
    ("云端删除不再按父目录分组", "app/server/openlist.py",
     "                self.remove(parent, names)\n"
     "                removed.extend(p for _, p in pairs)",
     '                raise OpenlistError("mutant")\n'
     "                removed.extend(p for _, p in pairs)",
     PY_TEST("test_download.py")),
    ("云端删除放行根目录", "app/server/openlist.py",
     '            if not p:\n'
     '                failures.append({"path": str(raw), "error": "不允许删除云端根目录"})\n'
     "                continue",
     "            if False:\n"
     '                failures.append({"path": str(raw), "error": "不允许删除云端根目录"})\n'
     "                continue",
     PY_TEST("test_download.py")),

    # ---- 下载队列 ----
    ("下载任务不再打 direction 标记（前端会渲染成上传）",
     "app/server/download_queue.py",
     '                task["direction"] = DIRECTION', '                task["direction"] = ""',
     PY_TEST("test_download.py")),
    ("下载失败不再走指数退避重试", "app/server/download_queue.py",
     "                self._handle_failure(task, exc)", "                self._mark_failed(task, exc)",
     PY_TEST("test_download.py")),

    # ---- 前端差异面板 ----
    ("前端不再展示「仅云端」分组", "app/www/index.html",
     '  if(kind==="cloud")return r.cloudExtra||[];', '  if(kind==="cloud")return [];',
     JS_TEST("test_ui.js")),
    ("前端勾选状态不再生效（批量按钮恒为空）", "app/www/index.html",
     "    diffEntries(k).forEach(e=>{if(diffState.sel.has(diffKey(k,e.path)))picked[k].push(e);});",
     "    diffEntries(k).forEach(e=>{if(false)picked[k].push(e);});",
     JS_TEST("test_ui.js")),
    ("批量清除不再需要二次确认", "app/www/index.html",
     "async function doDiffClear(specs){",
     "async function doDiffClear(specs){\n  return;",
     JS_TEST("test_ui.js")),

    # ---- v2.9.1 状态标签筛选开关 + 「已同步」明细 ----
    ("「已同步」的目录也被塞进明细（路径存在被当成内容齐全）", "app/server/verify.py",
     "            uploaded_dirs += 1\n            continue",
     "            uploaded_dirs += 1\n            uploaded.append(entry)\n            continue",
     PY_TEST("test_verify.py")),
    ("已同步明细不再截断（上万条明细全量回传）", "app/server/verify.py",
     "    if uploaded_truncated:\n        uploaded = uploaded[:UPLOADED_LIMIT]",
     "    if False:\n        uploaded = uploaded[:UPLOADED_LIMIT]",
     PY_TEST("test_verify.py")),
    ("已同步计数退化成明细条数（截断时徽标数字跟着缩水）", "app/server/verify.py",
     '    same_total = int(result.get("uploadedFiles") or 0) or len(same)',
     "    same_total = len(same)",
     PY_TEST("test_verify.py")),
    ("状态标签点了取消失败（再点一次不再恢复完整列表）", "app/www/index.html",
     "      diffState.filter=(diffState.filter===k)?null:k;",
     "      diffState.filter=k;",
     JS_TEST("test_ui.js")),
    ("状态标签点了但列表照旧渲染全部（筛选形同虚设）", "app/www/index.html",
     '  const kinds=filt?[filt]:["local","cloud","mismatch"];',
     '  const kinds=["local","cloud","mismatch"];',
     JS_TEST("test_ui.js")),
    ("「已同步」被当成可操作组（渲染出勾选框与操作按钮）", "app/www/index.html",
     "        if(m.readonly){", "        if(false){",
     JS_TEST("test_ui.js")),
    ("「全选」无视筛选（会勾上看不见的行）", "app/www/index.html",
     '  const kinds=diffState.filter?[diffState.filter]:["local","cloud","mismatch"];\n  const out=[];',
     '  const kinds=["local","cloud","mismatch"];\n  const out=[];',
     JS_TEST("test_ui.js")),
    ("筛到只读组时「全选」仍可点", "app/www/index.html",
     '  $("diffAll").disabled=!!(filt&&DIFF_GROUP_META[filt].readonly);',
     '  $("diffAll").disabled=false;',
     JS_TEST("test_ui.js")),
    ("徽标数字改用明细条数（截断时比真实数量少）", "app/www/index.html",
     "  const nSame=Number(r.sameCount||0);",
     '  const nSame=diffEntries("same").length;',
     JS_TEST("test_ui.js")),

    # ---- v2.9.2 比对两侧来源可选 ----
    ("右侧声明的 side 被忽略（只剩「左本地右云端」一种组合）", "app/server/http_api.py",
     '        side = normalize_side(node.get("side"), default_side)',
     "        side = default_side",
     PY_TEST("test_api.py")),
    ("云端来源被当成本地扫描（两侧同构失效）", "app/server/http_api.py",
     "    if side == SOURCE_LOCAL:", "    if True:",
     PY_TEST("test_api.py")),
    ("云端目录不存在时不再拦（静默当成空目录）", "app/server/http_api.py",
     "        if node is None:", "        if False:",
     PY_TEST("test_api.py")),
    ("云端路径不是目录时不再拦", "app/server/http_api.py",
     '        if not node.get("is_dir"):', "        if False:",
     PY_TEST("test_api.py")),
    ("条目的 owner 标反（前端会把两侧的路径对调）", "app/server/verify.py",
     '            "owner": "left",', '            "owner": "right",',
     PY_TEST("test_verify.py")),
    ("大小不一致的条目不再给出右侧路径", "app/server/verify.py",
     '            e["peerAbsPath"] = cloud.get("absPath") or ""',
     '            e["peerAbsPath"] = ""',
     PY_TEST("test_verify.py")),
    ("云端复制不再按父目录分组（同一批拆成逐条发）", "app/server/http_api.py",
     "                data = client.copy(src_dir, dst_dir, names, overwrite=True)",
     "                data = client.copy(src_dir, dst_dir, names[:1], overwrite=True)",
     PY_TEST("test_api.py")),
    ("云端复制整组失败后不再降级逐条（一个坏条目拖垮全组）", "app/server/http_api.py",
     "                for name, path in members:", "                for name, path in []:",
     PY_TEST("test_api.py")),
    ("云端复制放行根目录（等于把整个存储再拷一份）", "app/server/http_api.py",
     '            if src == "/":', "            if False:",
     PY_TEST("test_api.py")),
    ("本地复制放行存储空间根目录", "app/server/http_api.py",
     "            if os.path.normcase(src) in root_norms:", "            if False:",
     PY_TEST("test_api.py")),
    ("本地复制不拦「目标在源内部」（会无限自我复制）", "app/server/http_api.py",
     "                if is_path_allowed(dst, [src]):", "                if False:",
     PY_TEST("test_api.py")),
    # 目标目录的落点判据换过：以前判 `dst` 本身，现在判 dst 的**父目录** ——
    # 比对复制的目标常常是还不存在的目录，判 dst 自己等于把整批都放过。
    ("本地复制的目标目录越过白名单", "app/server/http_api.py",
     "            if not is_path_allowed(parent, roots):", "            if False:",
     PY_TEST("test_api.py")),

    # ---- v2.9.2 前端：来源切换 ----
    ("切换来源时不清旧路径（拿一串本地路径去比云端）", "app/www/index.html",
     '  cmpInputOf(which).value=cmpPaneOf(which)?(cmpRootFor(which)||""):"";',
     "  ;",
     JS_TEST("test_ui.js")),
    ("前端忽略服务端回传的侧别（文案退回按栏位猜）", "app/www/index.html",
     '  return (side==="local"||side==="cloud")?side:cmpSide[which];',
     "  return cmpSide[which];",
     JS_TEST("test_ui.js")),
    ("两侧同类时不加左右（会出现「用云端覆盖云端」）", "app/www/index.html",
     '  return side===other?(which==="left"?"左侧":"右侧")+sideName(side):sideName(side);',
     "  return sideName(side);",
     JS_TEST("test_ui.js")),
    ("云端 → 云端也说成「上传」", "app/www/index.html",
     '  if(transferVerb(from,to)==="复制")', "  if(false)",
     JS_TEST("test_ui.js")),
    ("前端不用服务端的 absPath / peerAbsPath（自己按栏位拼）", "app/www/index.html",
     '  const want=(e.owner&&e.owner!==which)?"peerAbsPath":"absPath";',
     '  const want="absPath";',
     JS_TEST("test_ui.js")),
    ("本地侧路径留空也照发比对请求", "app/www/index.html",
     "  if(missing.length){", "  if(false){",
     JS_TEST("test_ui.js")),
    ("服务端报错时不定位到出问题的那一侧", "app/www/index.html",
     '    if(s.indexOf(w==="left"?"左侧":"右侧")>=0)cmpInputOf(w).classList.add("bad");',
     "    ;",
     JS_TEST("test_ui.js")),
    ("云端 ↔ 云端误走上传队列（把目标当成云端目录上传）", "app/www/index.html",
     '    if(fromSide==="local"&&toSide==="cloud"){', "    if(true){",
     JS_TEST("test_ui.js")),

    # ---- v2.9.3 两侧同源时不许互相改写 ----
    ("两侧同源时两个框仍读同一个栏（浏览一侧就改写另一侧）", "app/www/index.html",
     '      if(!pane[w])return;\n      const st=state[pane[w]];',
     "      const st=state[cmpSide[w]];",
     JS_TEST("test_ui.js")),
    ("按栏位名字硬分配（不管栏的实际来源类型）", "app/www/index.html",
     "    if(paneSide(p)===cmpSide[w]&&!taken[p]){out[w]=p;taken[p]=true;}",
     "    if(!taken[p]){out[w]=p;taken[p]=true;}",
     JS_TEST("test_ui.js")),
    ("剩下的框不再认领空栏（交叉来源时有一侧没有栏）", "app/www/index.html",
     '    const p=["local","cloud"].find(x=>paneSide(x)===cmpSide[w]&&!taken[x]);\n'
     "    if(p){out[w]=p;taken[p]=true;}",
     "    const p=null;\n    if(p){out[w]=p;taken[p]=true;}",
     JS_TEST("test_ui.js")),
    # 「分不到栏就留空」这条判据换过名字：paneCompareRoot(side) → cmpRootFor(which)
    # —— 后者先过一遍「这一侧到底有没有栏」，两侧同源时才不会预填出同一个位置。
    ("切到同源时仍预填出同款路径（切完就注定比不出差异）", "app/www/index.html",
     '  cmpInputOf(which).value=cmpPaneOf(which)?(cmpRootFor(which)||""):"";',
     '  cmpInputOf(which).value=cmpRootFor(which)||"";',
     JS_TEST("test_ui.js")),
    ("「比对 ⇄」又把两侧填成同一个位置", "app/www/index.html",
     '      if(pane[w])cmpInputOf(w).value=paneCompareRoot(pane[w])||"";',
     '      cmpInputOf(w).value=paneCompareRoot(cmpSide[w])||"";',
     JS_TEST("test_ui.js")),
    ("两侧同位置时报错不标红（用户不知道该改哪个框）", "app/www/index.html",
     '    ["left","right"].forEach(w=>cmpInputOf(w).classList.add("bad"));\n    toast("两侧都是"',
     '    toast("两侧都是"',
     JS_TEST("test_ui.js")),
    ("两侧同源时不再给出引导", "app/www/index.html",
     '  el.classList.add("warn");',
     '  if(false)el.classList.add("warn");',
     JS_TEST("test_ui.js")),
    ("恢复旧存档时不纠正同值（升级后仍卡在比不出差异）", "app/www/index.html",
     "  blankUnfollowedDup();",
     "  ;",
     JS_TEST("test_ui.js")),

    # ---- v2.9.4 左栏自己也能是 OpenList 源（界面上会有两个云端栏）----
    ("左栏永远当成本地栏（无视设置里的来源模式）", "app/www/index.html",
     '  return ((state.config&&state.config.localSourceMode)==="cloud")?"cloud":"local";',
     '  return "local";',
     JS_TEST("test_ui.js")),
    ("比对来源默认值不跟栏走（左栏换成 OpenList 后左侧仍声明为「本地」）",
     "app/www/index.html",
     '    if(!cmpSideTouched[w])cmpSide[w]=paneSide(w==="left"?"local":"cloud");',
     "    ;",
     JS_TEST("test_ui.js")),
    ("用户切过来源也不记账（下次仍被「跟栏」改掉）", "app/www/index.html",
     "  cmpSideTouched[which]=true;",
     "  ;",
     JS_TEST("test_ui.js")),
    ("旧存档里的侧别被当成「没选过」（丢掉用户明确选过的侧别）", "app/www/index.html",
     "  return {left:uiState.cmpLeftSide!==undefined,right:uiState.cmpRightSide!==undefined};",
     "  return {left:false,right:false};",
     JS_TEST("test_ui.js")),
    ("「界面上没有该类型的栏」被说成「只有一个栏」（补救办法是错的）", "app/www/index.html",
     "  if(!avail.length){",
     "  if(false){",
     JS_TEST("test_ui.js")),
    # 原先这里给的是 applyConfig 里「单边默认值 + cmpBarAuto」那两句；那两件事
    # 现在分工了：默认来源在 applyConfig 用 forEach 给两侧一起刷（另有条目守着），
    # 「跟随浏览位置」的恢复落在 setCmpSide 的 cmpBarAuto 上 —— 换 left 来源时
    # 必须把冻结在旧路径上的比对条解冻，否则左框会一直停在失效的本地路径。
    ("换了左栏来源后不恢复「跟随浏览位置」（比对条冻在旧路径上）", "app/www/index.html",
     '  cmpBarAuto=true;\n  clearCmpError();',
     '  clearCmpError();',
     JS_TEST("test_ui.js")),

    # ---- 本轮新逻辑（原先一点变异覆盖都没有） ----
    ("list_all 不再探测服务端单页上限（服务端夹小 per_page 时中间条目整段漏掉）",
     "app/server/openlist.py",
     "            if (not probed and page == 1 and len(content) < per_page",
     "            if (False and page == 1 and len(content) < per_page",
     PY_TEST("test_openlist.py")),
    ("上传收尾不再释放认领（_running 残留 → 任务永久卡在「排队中」）",
     "app/server/upload_queue.py",
     "            self._release(tid)",
     "            pass",
     PY_TEST("test_upload_queue.py")),
    ("curl 退出码非 0 但已吐出半截响应 → 被当成真响应用下去（目录列表少一半却不报错）",
     "app/server/httpclient.py",
     "    if proc.returncode != 0:",
     "    if False:",
     PY_TEST("test_httpclient.py")),
    ("云端复制不带 overwrite（目标已存在时整个 OpenList 任务直接失败）",
     "app/server/http_api.py",
     "                data = client.copy(src_dir, dst_dir, names, overwrite=True)",
     "                data = client.copy(src_dir, dst_dir, names, overwrite=False)",
     PY_TEST("test_api.py")),
    ("上传按钮不再两步确认（一次误点就把整个目录传上去）", "app/www/index.html",
     "  if(!uploadConfirmTimer){", "  if(false){",
     JS_TEST("test_ui.js")),
]


def _bak_path(rel):
    return os.path.join(BAKDIR, rel.replace("/", "__"))


def _stamp_time():
    return os.path.getmtime(STAMP) if os.path.exists(STAMP) else None


def restore_leftovers(files):
    """还原上一轮中断留下的变异；返回被还原的文件列表。

    判据是「备份的 mtime 比成功戳新」而不是「锚点是否匹配」：备份才是唯一权威的
    原件，这样即使某条变异的锚点后来因重构失效，也不会把已污染的文件当成正常。
    """
    stamp = _stamp_time()
    fixed = []
    for rel in files:
        bak = _bak_path(rel)
        if not os.path.exists(bak):
            continue
        if stamp is not None and os.path.getmtime(bak) <= stamp:
            continue
        path = os.path.join(ROOT, rel)
        with open(bak, encoding="utf-8") as fh:
            saved = fh.read()
        now = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if now != saved:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(saved)
            fixed.append(rel)
    if fixed:
        print("！检测到上一轮中断残留，已还原：%s" % "、".join(fixed))
    if fixed:
        mark_success()          # 还原完立刻打戳，下次不再重复还原
    return fixed


def mark_success():
    os.makedirs(BAKDIR, exist_ok=True)
    with open(STAMP, "w", encoding="utf-8") as fh:
        fh.write("ok %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))


def check_anchors():
    """只体检锚点：每条必须**恰好命中一次**。

    为什么单独做这一步：锚点是代码的文本快照，重构会让它悄悄失效 ——
    在全量跑里表现为安静的 `SKIP`（不变红、不中止），是覆盖面的盲区。
    重构后先跑这个（秒级）再跑全量（10 分钟），省一轮等待。
    """
    bad = 0
    for desc, rel, old, new, _cmd in MUTATIONS:
        path = os.path.join(ROOT, rel)
        text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        n = text.count(old)
        if n == 0:
            print("  [失效] %s（%s）" % (desc, rel))
            bad += 1
        elif n > 1:
            print("  [命中 %d 次，不唯一] %s（%s）" % (n, desc, rel))
            bad += 1
    print("\n锚点体检：共 %d 条，有问题 %d 条" % (len(MUTATIONS), bad))
    return 1 if bad else 0


def main():
    if "--anchors" in sys.argv:
        return check_anchors()

    picked = MUTATIONS
    if "--only" in sys.argv:
        kw = sys.argv[sys.argv.index("--only") + 1] if len(sys.argv) > sys.argv.index("--only") + 1 else ""
        picked = [m for m in MUTATIONS if kw in m[0]]
        if not picked:
            print("没有说明里含「%s」的变异" % kw)
            return 1
        print("只跑 %d 条（说明含「%s」）\n" % (len(picked), kw))

    files = sorted({rel for _, rel, _, _, _ in picked})
    os.makedirs(BAKDIR, exist_ok=True)
    restore_leftovers(sorted({rel for _, rel, _, _, _ in MUTATIONS}))
    for rel in files:
        shutil.copyfile(os.path.join(ROOT, rel), _bak_path(rel))

    if NODE is None and any(cmd is not None and cmd[0] != PY for *_x, cmd in picked):
        print("！没找到 node，前端相关的变异无法执行。")
        print("  装一个 node，或用 DD_NODE=/path/to/node 指定。")

    missing, survived, skipped, done = [], [], [], 0
    for desc, rel, old, new, cmd in picked:
        path = os.path.join(ROOT, rel)
        original = open(path, encoding="utf-8").read()
        if old not in original:
            missing.append("%s（%s）" % (desc, rel))
            print("  SKIP  锚点没匹配上：%s" % desc)
            continue
        if cmd is None:
            skipped.append(desc)
            print("  SKIP  缺 node，跳过：%s" % desc)
            continue
        done += 1
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original.replace(old, new, 1))
            proc = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True,
                                  errors="replace")
            red = proc.returncode != 0
            print("  %s %s" % ("变红 ✓" if red else "仍全绿 ✗", desc))
            if not red:
                survived.append(desc)
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)

    # 全部跑完、源码已归位 → 打成功戳（"备份比戳新"才代表中断）
    mark_success()

    print("\n反向验证：跑了 %d 条变异；%d 条未被测试发现，%d 条锚点失效"
          % (done, len(survived), len(missing)))
    for item in survived:
        print("  [未被发现] %s" % item)
    for item in missing:
        print("  [锚点失效] %s" % item)
    return 1 if (survived or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
