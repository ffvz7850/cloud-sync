# -*- coding: utf-8 -*-
"""跨层契约检查。

单元测试各自证明"每一层自己是对的"，但**层与层之间对不上**是另一类故障，
往往只在真机上暴露。这里把所有跨层约定集中钉住：

* 前端引用的元素 id 是否都真的存在于 HTML（少一个就是运行时 TypeError）
* 前端调的接口后端是否都实现了（少一个就是 404）
* 桌面入口 key / manifest / 网关前缀 是否三方一致
* 界面硬性约束（不用原生 confirm/alert/prompt、飞牛配色变量在位）
* 后端不顶层硬导入依赖 C 扩展的模块（飞牛裁剪 python 的坑）
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

import config       # noqa: E402
import http_api     # noqa: E402

ROOT = support.ROOT


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def read_bytes(rel):
    with open(os.path.join(ROOT, rel), "rb") as fh:
        return fh.read()


def strip_js_comments(text):
    """剥掉 JS 注释再检查：注释里正当地提到 confirm() 这类名字不该被判违规。"""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    lines = []
    for line in text.splitlines():
        idx = line.find("//")
        lines.append(line[:idx] if idx >= 0 else line)
    return "\n".join(lines)


class FrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read("app/www/index.html")
        cls.code = strip_js_comments(cls.html)

    def test_no_native_dialogs(self):
        """iframe 沙箱会静默拦掉原生 confirm/alert/prompt —— 点了完全没反应。"""
        for pattern in ("confirm(", "alert(", "prompt("):
            hits = [m.start() for m in
                    re.finditer(r"(?<![.\w])" + re.escape(pattern), self.code)]
            self.assertEqual(hits, [], "index.html 里出现了原生 %s，必须改成自绘弹层" % pattern)

    def test_custom_dialogs_present(self):
        # 自绘弹层函数必须存在（function 声明或 const 箭头函数都算）
        for name in ("askConfirm", "askInput", "askAlert"):
            hit = ("function %s(" % name in self.html) or \
                  re.search(r"\b(const|let|var)\s+%s\s*=" % name, self.html)
            self.assertTrue(hit, "index.html 缺少自绘弹层 %s" % name)

    def test_fn_brand_colors_pinned(self):
        """色值是从飞牛官网 grep 出来的，写进 :root 并用测试钉住。"""
        for token in ("--fn-blue:#0f5fff", "--fn-blue2:#4593ff",
                      "--fn-soft:#edf5ff", "--fn-text:#1f2024"):
            self.assertIn(token, self.html, "缺少飞牛配色变量 %s" % token)

    def test_localstorage_access_is_guarded(self):
        """localStorage 在 iframe 里可能抛异常，必须 try/catch 包裹。"""
        self.assertIn("function ls(", self.html)
        self.assertIn("localStorage", self.html)
        self.assertIn("catch(e)", self.html)

    def test_base_detection_does_not_guess_from_pathname_only(self):
        """必须实测探测，不能靠 pathname 砍一段推导 —— 这是最坑的 bug 源。"""
        self.assertIn("function probeBase(", self.html)
        self.assertIn("function baseCandidates(", self.html)
        self.assertIn("d.app===APP_NAME", self.html.replace(" ", ""))

    def test_no_page_header_duplicate_app_name(self):
        """桌面小窗标题栏已有应用名，页内不要再写一个 <h1>。"""
        self.assertNotIn("<h1>", self.html)

    def test_lists_scroll_internally(self):
        self.assertIn("overflow-y:auto", self.html)

    def test_no_webdav_leftover(self):
        """WebDAV 已彻底移除，界面与脚本里都不该再出现。"""
        for token in ("webdav", "WebDAV", "WEBDAV", "davUrl", "8477"):
            self.assertNotIn(token, self.code, "前端还残留 WebDAV：%s" % token)


class UploadButtonLayoutContractTest(unittest.TestCase):
    """钉住 v2.0.1 的核心修复：上传按钮必须单行完整可见。

    背景：中列曾是固定 44px，"上传 →" 被压成竖线，用户看不到按钮。
    这些断言在 CSS/DOM 层面把回归口子焊死。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = read("app/www/index.html")
        match = re.search(r"<style>(.*?)</style>", cls.html, re.DOTALL)
        css = match.group(1) if match else ""
        # 先剥掉 CSS 注释：注释会粘在选择器捕获里，导致精确匹配失效
        css = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)
        cls.css = css
        cls.rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)

    def _rule(self, selector):
        for sel, body in self.rules:
            if sel.strip() == selector:
                return body
        return None

    def test_panes_grid_columns_adaptive(self):
        """中列必须按内容自适应；两侧轨道必须 minmax(0,1fr)，否则长面包屑会把上传按钮挤出。"""
        body = self._rule("#panes")
        self.assertTrue(body, "找不到 #panes 规则")
        norm = body.replace(" ", "")
        self.assertIn("grid-template-columns:minmax(0,1fr)autominmax(0,1fr)", norm,
                      "#panes 必须用 minmax(0,1fr) auto minmax(0,1fr)，"
                      "裸写 1fr 等价 minmax(auto,1fr)，长路径会把中栏上传按钮挤出可视区")

    def test_body_is_flex_container_and_main_is_column(self):
        """高度链根：body 是 flex 容器（侧边栏 + 主区并排）；#main 内部纵向 flex，列表才能内部滚动。"""
        import re as _re
        m = _re.search(r"(?m)^body\{([^}]*)\}", self.html)
        self.assertTrue(m, "找不到 body 规则")
        norm = m.group(1).replace(" ", "")
        self.assertIn("display:flex", norm, "body 不是 flex 容器")
        # 侧边栏导航与主区并排（body 用 row），高度链由 #main 的 column 承接
        m2 = _re.search(r"(?m)^#main\{([^}]*)\}", self.html)
        self.assertTrue(m2, "找不到 #main 规则")
        norm2 = m2.group(1).replace(" ", "")
        self.assertIn("flex-direction:column", norm2, "#main 必须纵向 flex")

    def test_midbar_no_fixed_width(self):
        """#midbar 不能再有任何固定宽度约束。"""
        body = self._rule("#midbar")
        self.assertTrue(body, "找不到 #midbar 规则")
        self.assertNotRegex(body.replace(" ", ""), r"(^|[;{])width:\d",
                            "#midbar 出现了固定宽度，会把上传按钮压窄")

    def test_btn_upload_element_not_hidden(self):
        """#btnUpload 元素本身不能带 hidden 类或内联隐藏样式。"""
        match = re.search(r'<button[^>]*id="btnUpload"[^>]*>', self.html)
        self.assertTrue(match, "找不到 #btnUpload 元素")
        tag = match.group(0)
        self.assertNotIn("hidden", tag, "#btnUpload 元素带了 hidden 类")
        self.assertNotIn("display:none", tag.replace(" ", "").lower(),
                         "#btnUpload 有内联 display:none")
        self.assertNotIn("visibility:hidden", tag.replace(" ", "").lower(),
                         "#btnUpload 有内联 visibility:hidden")
        # 按钮文字必须完整存在（不是空按钮）
        self.assertIn("上传", self.html)

    def test_no_css_rule_hides_upload_button(self):
        """任何含 display:none / visibility:hidden 的 CSS 规则，
        其选择器都不得可能命中 #btnUpload 或其常驻容器（#midbar / #panes / .btn）。
        防止未来有人把按钮藏了测试还绿着。"""
        for selector, body in self.rules:
            norm = body.replace(" ", "").lower()
            if "display:none" not in norm and "visibility:hidden" not in norm:
                continue
            for sel in selector.split(","):
                sel = sel.strip()
                for target in ("#btnUpload", "#midbar", "#panes", ".btn"):
                    if target in sel:
                        self.fail("规则 %s{%s} 会隐藏上传按钮（命中 %s）"
                                  % (selector.strip(), body.strip(), target))


class ElementIdTest(unittest.TestCase):
    """前端 $("...") 引用的 id 必须都在 HTML 里存在。"""

    @classmethod
    def setUpClass(cls):
        cls.html = read("app/www/index.html")
        cls.ids = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', cls.html))
        cls.used = set(re.findall(r'\$\("([A-Za-z0-9_\-]+)"\)', cls.html))

    def test_all_referenced_ids_exist(self):
        missing = sorted(self.used - self.ids)
        self.assertEqual(missing, [], "前端引用了不存在的元素 id：%s" % missing)

    def test_no_orphan_critical_ids(self):
        for required in ("localList", "cloudList", "localCrumb", "cloudCrumb",
                         "queueBody", "queueStat", "selInfo", "cmpOut", "logBox",
                         "toast", "inUrl", "inUser",
                         "inPass", "btnUpload", "btnCompare", "cmpLocal", "cmpCloud"):
            self.assertIn(required, self.ids, "关键元素 %s 不见了" % required)


class ApiRouteContractTest(unittest.TestCase):
    """前端调用的每个接口，后端必须实现。"""

    @classmethod
    def setUpClass(cls):
        cls.html = read("app/www/index.html")
        cls.routes = set(http_api._ROUTES.keys())

    def _calls(self):
        """抓 api("path", ...) 调用。带 body 的视为 POST，否则 GET。"""
        found = []
        for match in re.finditer(r'api\(\s*"([^"]+)"((?:[^;]|\n(?!//)){0,220})',
                                 self.html):
            path = match.group(1)
            tail = match.group(2) or ""
            method = "POST" if "body" in tail or 'method:"POST"' in tail else "GET"
            found.append((method, "/" + path.split("?")[0].lstrip("/")))
        return found

    def test_every_call_has_a_route(self):
        missing = []
        for method, route in self._calls():
            if (method, route) not in self.routes:
                missing.append("%s %s" % (method, route))
        self.assertEqual(sorted(set(missing)), [],
                         "前端调了后端没有的接口：%s" % sorted(set(missing)))

    def test_calls_were_actually_found(self):
        """防止正则失效导致上面的断言空跑（测试假绿的老毛病）。"""
        self.assertGreater(len(self._calls()), 12)

    def test_mutations_use_post(self):
        methods = dict((route, method) for method, route in self._calls())
        for route in ("/api/connect", "/api/upload/enqueue", "/api/compare",
                      "/api/upload/retry", "/api/cloud/mkdir"):
            self.assertEqual(methods.get(route), "POST", "%s 应该用 POST" % route)


class FpkManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fields = {}
        for line in read("manifest").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                cls.fields[key.strip()] = value.strip()
        cls.ui = json.loads(read("app/ui/config"))

    def test_required_manifest_fields(self):
        for key in ("appname", "version", "display_name", "desc", "source",
                    "platform", "desktop_uidir", "desktop_applaunchname"):
            self.assertTrue(self.fields.get(key), "manifest 缺少 %s" % key)

    def test_version_matches_code(self):
        self.assertEqual(self.fields["version"], config.APP_VERSION,
                         "manifest 版本号与 config.APP_VERSION 不一致")

    def test_version_format(self):
        self.assertRegex(self.fields["version"], r"^\d+\.\d+\.\d+$")

    def test_desktop_entry_key_matches(self):
        launch = self.fields["desktop_applaunchname"]
        self.assertIn(launch, self.ui[".url"],
                      "app/ui/config 的入口 key 必须等于 desktop_applaunchname")

    def test_entry_is_iframe(self):
        entry = self.ui[".url"][self.fields["desktop_applaunchname"]]
        self.assertEqual(entry["type"], "iframe", "用户要求的是飞牛桌面内嵌小窗")

    def test_gateway_prefix_matches_appname(self):
        entry = self.ui[".url"][self.fields["desktop_applaunchname"]]
        self.assertEqual(entry["gatewayPrefix"], "/app/" + self.fields["appname"])
        self.assertNotIn(".", entry["gatewayPrefix"])
        self.assertEqual(entry["gatewaySocket"], "app.sock")
        self.assertEqual(entry["url"], "/app/" + self.fields["appname"])

    def test_appname_matches_code(self):
        self.assertEqual(self.fields["appname"], config.APP_NAME)

    def test_python_runtime_declared(self):
        self.assertIn("python312", self.fields.get("install_dep_apps", ""))

    def test_changelog_present_and_single_line(self):
        changelog = self.fields.get("changelog", "")
        self.assertTrue(changelog)
        self.assertNotIn("\n", changelog)

    def test_manifest_has_no_webdav(self):
        blob = read("manifest").lower()
        self.assertNotIn("webdav 服务", blob)
        self.assertNotIn("8477", blob)


class CmdScriptsTest(unittest.TestCase):
    REQUIRED = ("main", "install_init", "install_callback", "upgrade_init",
                "upgrade_callback", "uninstall_init", "uninstall_callback",
                "config_init", "config_callback")

    def test_all_present(self):
        for name in self.REQUIRED:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "cmd", name)),
                            "缺少 cmd/%s" % name)

    def test_lf_and_no_bom(self):
        for name in self.REQUIRED:
            raw = read_bytes(os.path.join("cmd", name))
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "cmd/%s 带 BOM" % name)
            self.assertNotIn(b"\r\n", raw, "cmd/%s 是 CRLF，NAS 上会 bad interpreter" % name)

    def test_status_exit_codes(self):
        """status 运行中必须返回 0、未运行返回 3。"""
        main = read("cmd/main")
        self.assertIn("exit 0", main)
        self.assertIn("exit 3", main)

    def test_main_avoids_sourcing_settings(self):
        main = read("cmd/main")
        self.assertIn("sed -n", main)
        self.assertNotIn("source ${CONF_FILE}", main)

    def test_main_uses_pidfile_not_pgrep(self):
        main = read("cmd/main")
        self.assertIn("kill -0", main)
        self.assertNotIn("pgrep", main)

    def test_no_webdav_in_scripts(self):
        for name in self.REQUIRED:
            body = read(os.path.join("cmd", name))
            for token in ("WEBDAV", "webdav", "8477", "--no-webdav"):
                self.assertNotIn(token, body, "cmd/%s 还残留 %s" % (name, token))


class AssetTest(unittest.TestCase):
    def test_wizard_dir_exists(self):
        self.assertTrue(os.path.isdir(os.path.join(ROOT, "wizard")))

    def test_wizard_contains_only_uninstall_wizard(self):
        entries = [e for e in os.listdir(os.path.join(ROOT, "wizard"))
                   if not e.startswith(".")]
        # uninstall 是合法的卸载向导（让用户在卸载时选择保留/清除数据），其余应为空
        self.assertEqual([e for e in entries if e != "uninstall"], [],
                         "wizard/ 里除 uninstall 外不该有文件，实际有：%s" % entries)

    def test_icons_present_and_sized(self):
        import struct
        for rel, want in (("ICON.PNG", 64), ("ICON_256.PNG", 256),
                          ("app/ui/images/icon_64.png", 64),
                          ("app/ui/images/icon_256.png", 256)):
            raw = read_bytes(rel)[:33]
            self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n", "%s 不是合法 PNG" % rel)
            width, height = struct.unpack(">II", raw[16:24])
            self.assertEqual((width, height), (want, want), "%s 尺寸不对" % rel)

    def test_config_json_files_valid(self):
        for rel in ("config/privilege", "config/resource", "app/ui/config"):
            json.loads(read(rel))

    def test_runs_as_root(self):
        self.assertEqual(json.loads(read("config/privilege"))["defaults"]["run-as"], "root")

    def test_no_c_extension_hard_imports(self):
        """飞牛自带 python312 是裁剪构建。任何模块顶层硬导入依赖 C 扩展的标准库，
        失败都是进程级的。用 AST 判定（try 里的软导入是合法的能力探测）。"""
        import ast
        risky = ("lzma", "bz2", "gzip", "zlib", "zipfile", "tarfile",
                 "sqlite3", "hashlib", "ssl", "ctypes", "cryptography")
        offenders = []
        server_dir = os.path.join(ROOT, "app", "server")
        for name in sorted(os.listdir(server_dir)):
            if not name.endswith(".py"):
                continue
            source = read(os.path.join("app", "server", name))
            tree = ast.parse(source)

            def walk(body, guarded):
                for stmt in body:
                    if isinstance(stmt, ast.Try):
                        walk(stmt.body, True)
                        for handler in stmt.handlers:
                            walk(handler.body, True)
                        walk(stmt.orelse, guarded)
                        walk(stmt.finalbody, guarded)
                        continue
                    if isinstance(stmt, (ast.If, ast.For, ast.While, ast.With)):
                        walk(stmt.body, guarded)
                        walk(stmt.orelse, guarded)
                        continue
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        continue
                    if isinstance(stmt, ast.Import):
                        for alias in stmt.names:
                            root = alias.name.split(".")[0]
                            if root in risky and not guarded:
                                offenders.append("%s:%d import %s"
                                                 % (name, stmt.lineno, alias.name))
                    elif isinstance(stmt, ast.ImportFrom):
                        root = (stmt.module or "").split(".")[0]
                        if root in risky and not guarded:
                            offenders.append("%s:%d from %s"
                                             % (name, stmt.lineno, stmt.module))

            walk(tree.body, False)
        self.assertEqual(offenders, [],
                         "顶层硬导入了依赖 C 扩展的模块：%s" % offenders)

    def test_removed_modules_are_gone(self):
        """v2.0 移除的模块不该再出现在 server 目录。"""
        server_dir = os.path.join(ROOT, "app", "server")
        for gone in ("doubao.py", "webdav.py", "crypto.py", "credentials.py", "jobs.py"):
            self.assertFalse(os.path.isfile(os.path.join(server_dir, gone)),
                             "%s 应该已被移除" % gone)

    def test_static_files_present(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "app/www/index.html")))


class DocTest(unittest.TestCase):
    def test_docs_exist(self):
        for rel in ("docs/API参考.md", "docs/需求与设计.md", "README.md"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), "缺少 %s" % rel)


class LayoutV21ContractTest(unittest.TestCase):
    """v2.1.0 布局契约：钉死「进入多层文件夹后中栏上传按钮消失」的回归口子。

    根因：`1fr` 轨道与 `.pane` 默认 min-width:auto，深路径把面包屑 min-content
    撑大 → 网格右溢 → #panes 的 overflow:hidden 把中栏按钮裁到可视区外。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = read("app/www/index.html")
        match = re.search(r"<style>(.*?)</style>", cls.html, re.DOTALL)
        css = match.group(1) if match else ""
        css = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)
        cls.rules = dict((sel.strip(), body) for sel, body in
                         re.findall(r"([^{}]+)\{([^{}]*)\}", css))

    def _rule(self, selector):
        return self.rules.get(selector)

    def test_pane_allows_shrink(self):
        body = self._rule(".pane")
        self.assertTrue(body, "找不到 .pane 规则")
        self.assertIn("min-width:0", body.replace(" ", ""),
                      ".pane 必须有 min-width:0，否则 1fr 轨道撑不住深路径")

    def test_pane_body_allows_shrink(self):
        body = self._rule(".pane-body")
        self.assertTrue(body, "找不到 .pane-body 规则")
        self.assertIn("min-width:0", body.replace(" ", ""))

    def test_pane_head_allows_shrink(self):
        """面包屑的直接父级必须也能收缩 —— 链条少一环就前功尽弃。"""
        body = self._rule(".pane-head")
        self.assertTrue(body, "找不到 .pane-head 规则")
        self.assertIn("min-width:0", body.replace(" ", ""),
                      ".pane-head 缺 min-width:0，收缩链条在面包屑上一级断链")

    def test_crumb_allows_shrink(self):
        body = self._rule(".crumb")
        self.assertIn("min-width:0", body.replace(" ", ""),
                      ".crumb 自身也要能收缩，否则仍会把父级撑大")

    def test_crumb_items_not_squashed(self):
        """深路径下面包屑条目不能被压变形（flex:none）。"""
        for selector in (".crumb a", ".crumb .sep"):
            body = self._rule(selector)
            self.assertTrue(body, "找不到 %s 规则" % selector)
            self.assertIn("flex:none", body.replace(" ", ""),
                          "%s 必须 flex:none，否则层级条目会被 1fr 挤扁" % selector)

    def test_midbar_does_not_grow(self):
        body = self._rule("#midbar")
        self.assertIn("flex:none", body.replace(" ", ""),
                      "#midbar 不能参与伸缩，否则会被两侧内容挤压")

    def test_crumb_single_line(self):
        body = self._rule(".crumb")
        self.assertTrue(body, "找不到 .crumb 规则")
        norm = body.replace(" ", "")
        self.assertIn("flex-wrap:nowrap", norm, "面包屑必须不换行（深度不再影响布局尺寸）")
        self.assertIn("white-space:nowrap", norm)
        self.assertIn("overflow-x:auto", norm, "面包屑要能横向滚动")

    def test_midbar_min_width(self):
        body = self._rule("#midbar")
        self.assertTrue(body, "找不到 #midbar 规则")
        self.assertRegex(body.replace(" ", ""), r"min-width:1[0-9]{2}px",
                         "#midbar 需要 min-width（≥118px）保证「上传 →」单行完整")

    def test_btn_upload_nowrap(self):
        body = self._rule("#btnUpload")
        self.assertTrue(body, "找不到 #btnUpload 规则")
        self.assertIn("white-space:nowrap", body.replace(" ", ""))

    def test_form_row_no_wrap(self):
        """设置页表单行统一栅格、不允许换行错位。"""
        body = self._rule(".frow")
        self.assertTrue(body, "找不到 .frow 规则")
        self.assertIn("flex-wrap:nowrap", body.replace(" ", ""),
                      ".frow 必须 nowrap，窄窗口下控件才会收缩而不是掉到 label 下方")

    def test_queue_controls_present(self):
        for need in ('id="btnRetryAll"', 'id="queueFilter"', 'data-filter="failed"'):
            self.assertIn(need, self.html, "队列新增控件缺失：%s" % need)


class SettingsLayoutV21ContractTest(unittest.TestCase):
    """v2.1.0 设置页布局契约：统一栅格、控件可收缩、复选框整行可点、
    元素 id 不变、去掉内联定宽。"""

    SETTINGS_IDS = ("cfgConc", "cfgRetry", "cfgVerify",
                    "cfgExcl", "cfgLocal", "cfgCloud", "cfgLocalCloudRoot",
                    "cfgAutoScan", "cfgFullScanInterval", "cfgScanScheduleTime",
                    "cfgBindingScanConc",
                    "btnSaveCfg", "cfgMsg", "btnViewLog", "btnDisconnect", "logBox")

    @classmethod
    def setUpClass(cls):
        cls.html = read("app/www/index.html")
        match = re.search(r"<style>(.*?)</style>", cls.html, re.DOTALL)
        css = re.sub(r"/\*.*?\*/", " ", match.group(1) if match else "", flags=re.DOTALL)
        cls.rules = dict((sel.strip(), body) for sel, body in
                         re.findall(r"([^{}]+)\{([^{}]*)\}", css))

    @staticmethod
    def _norm(body):
        return re.sub(r"\s+", " ", body or "").strip()

    def _rule(self, selector):
        return self.rules.get(selector)

    def test_rules_parsed_guard(self):
        """防正则失效导致下面断言空跑（假绿老毛病）。"""
        self.assertGreater(len(self.rules), 25,
                           "只解析到 %d 条 CSS 规则，解析器可能失效" % len(self.rules))

    def test_frow_unified_grid(self):
        body = self._rule(".frow")
        self.assertTrue(body, "找不到 .frow 规则")
        self.assertIn("flex-wrap:nowrap", self._norm(body))
        self.assertIn("min-height:38px", self._norm(body))

    def test_frow_controls_can_shrink(self):
        matched = [body for sel, body in self.rules.items()
                   if sel.startswith(".frow input")]
        self.assertTrue(matched, "找不到 .frow input 规则")
        joined = self._norm(" ".join(matched))
        self.assertIn("min-width:0", joined, "设置控件必须 min-width:0，窄窗才收缩不掉行")
        self.assertIn("flex:1 1 auto", joined, "设置控件应 flex:1 1 auto 占满剩余宽度")

    def test_frow_label_chk_clickable(self):
        body = self._rule(".frow label.chk")
        self.assertTrue(body, "找不到 .frow label.chk 规则")
        norm = self._norm(body)
        self.assertIn("width:auto", norm, "复选框标签不能沿用固定 120px，否则仅文字可点")
        self.assertIn("flex:1 1 auto", norm)
        self.assertIn("cursor:pointer", norm, "整行可点需要 cursor:pointer")

    def test_frow_actions_and_btnrow(self):
        self.assertIsNotNone(self._rule(".frow.actions"), "缺少 .frow.actions 规则")
        body = self._rule(".frow.btnrow .btn")
        self.assertTrue(body, "找不到 .frow.btnrow .btn 规则")
        self.assertIn("flex:1 1 0", self._norm(body), "底部按钮应等分宽度")

    def test_settings_ids_present(self):
        ids = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', self.html))
        for need in self.SETTINGS_IDS:
            self.assertIn(need, ids, "设置页元素 %s 不见了（id 被改）" % need)

    def test_no_inline_fixed_width(self):
        """设置页控件不得再有内联定宽（v2.0 的 width:80px 历史遗留）。"""
        self.assertNotIn("width:80px", self.html)
        self.assertNotIn("width: 80px", self.html)
        for cid in ("cfgConc", "cfgRetry"):
            match = re.search(r'<input[^>]*id="%s"[^>]*>' % cid, self.html)
            self.assertTrue(match, "找不到 #%s 元素" % cid)
            tag = match.group(0)
            self.assertNotIn("style=", tag, "#%s 仍带内联样式（应交给栅格）" % cid)
            self.assertNotIn("width", tag, "#%s 仍带内联宽度" % cid)

    def test_retry_all_starts_hidden(self):
        """「全部重试」初始隐藏，仅由 JS 在存在失败项时显示。"""
        match = re.search(r'<button[^>]*id="btnRetryAll"[^>]*>', self.html)
        self.assertTrue(match, "找不到 #btnRetryAll 元素")
        self.assertIn("hidden", match.group(0),
                      "#btnRetryAll 初始应带 hidden，避免无失败项时误显示")

    def test_queue_filter_four_states(self):
        for state in ("all", "failed", "done", "active"):
            self.assertIn('data-filter="%s"' % state, self.html,
                          "队列筛选缺少 %s 档" % state)


class BindingContractTest(unittest.TestCase):
    """钉住「目录绑定」功能的跨层契约：标签页、关键元素、弹层、路由。"""

    @classmethod
    def setUpClass(cls):
        cls.html = read("app/www/index.html")
        cls.code = strip_js_comments(cls.html)
        cls.ids = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', cls.html))

    def test_bindings_tab_button(self):
        self.assertIn('data-tab="bindings"', self.html,
                      "缺少「同步备份」标签页按钮")

    def test_bindings_page_exists(self):
        self.assertIn('id="page-bindings"', self.html)

    def test_bindings_critical_ids(self):
        for need in ("bindList", "bindToolbar", "btnNewBinding",
                     "btnScanAll", "bindHint"):
            self.assertIn(need, self.ids,
                          "绑定页关键元素 %s 不见了" % need)

    def test_binding_form_dialog(self):
        for need in ("bindOverlay", "bindDlg", "bdName", "bdLocal",
                     "bdTargets", "bdAuto", "bdExcl",
                     "bdAddTarget", "bdSaveBtn", "bdCancelBtn"):
            self.assertIn(need, self.ids,
                          "绑定表单缺少元素 %s" % need)

    def test_picker_dialog(self):
        for need in ("pickerOverlay", "pickerDlg", "pkBody",
                     "pkCrumb", "pkCurrent", "pkSelectBtn",
                     "pkCancelBtn", "pkRefresh"):
            self.assertIn(need, self.ids,
                          "目录选择器缺少元素 %s" % need)

    def test_add_target_button_enables_multi_target(self):
        """一个本地目录必须能绑定多个云端目录。"""
        self.assertIn('id="bdAddTarget"', self.html)
        self.assertIn("makeTargetEditor", self.code)

    def test_auto_upload_switch_present(self):
        self.assertIn('id="bdAuto"', self.html)

    def test_binding_routes_registered(self):
        """后端必须注册全部绑定路由。"""
        required = [
            ("GET", "/api/bindings"),
            ("GET", "/api/bindings/detail"),
            ("POST", "/api/bindings/create"),
            ("POST", "/api/bindings/update"),
            ("POST", "/api/bindings/delete"),
            ("POST", "/api/bindings/scan"),
            ("POST", "/api/bindings/scan-all"),
            ("POST", "/api/bindings/upload"),
        ]
        for method, route in required:
            self.assertIn((method, route), http_api._ROUTES,
                          "缺少绑定路由 %s %s" % (method, route))

    def test_binding_module_exists(self):
        self.assertTrue(os.path.isfile(
            os.path.join(ROOT, "app", "server", "bindings.py")))

    def test_binding_file_shows_target_statuses(self):
        """前端必须渲染每个云端目标的状态（多目标独立状态）。"""
        self.assertIn("bf-tgts", self.html)
        self.assertIn("BIND_ST_TEXT", self.code)

    def test_binding_persists_to_bindings_json(self):
        """绑定必须持久化到 bindings.json。"""
        import bindings as bindings_mod
        self.assertIn("bindings.json",
                      bindings_mod.BindingStore.__init__.__code__.co_consts
                      and read("app/server/bindings.py"))

    def test_binding_upload_uses_openlist_copy(self):
        """绑定上传走 OpenList 服务端复制，而不是 HTTP 上传队列。"""
        self.assertIn("self.client.copy", read("app/server/bindings.py"))

    def test_binding_supports_manual_and_auto(self):
        """手动上传（upload_files）与自动上传（_auto_upload）都要存在。"""
        self.assertIn("def upload_files", read("app/server/bindings.py"))
        self.assertIn("def _auto_upload", read("app/server/bindings.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
