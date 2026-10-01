/**
 * 前端测试（v2.0 OpenList 上传）：用 jsdom 真跑页面脚本，做 DOM 级断言。
 *
 * 两个刻意的设计（沿用 v1 的教训）：
 *  1. **严格网关桩** —— 只放行 `/app/cloud-sync/` 开头的请求，其余一律 404。
 *     这样如果前端把接口地址算成 `/app/`（"砍掉最后一段"那个经典 bug），
 *     数据就渲染不出来，测试必挂。用宽容的桩会把这类 bug 掩盖掉。
 *  2. **本地不跑浏览器** —— 沙箱里的无头浏览器经常静默失败（退出码 0 但没输出），
 *     所以这里用 jsdom 真跑脚本、直接断言 DOM，比截图硬。
 */

const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "app", "www", "index.html"), "utf-8");
const APP = "cloud-sync";
const ORIGIN = "http://nas.local";
const ALLOWED_PREFIX = ORIGIN + "/app/" + APP + "/";

let passed = 0;
let failed = 0;
const failures = [];

function check(name, condition, detail) {
  if (condition) {
    passed++;
    process.stdout.write("  ok   " + name + "\n");
  } else {
    failed++;
    failures.push(name + (detail ? "  → " + detail : ""));
    process.stdout.write("  FAIL " + name + (detail ? "  → " + detail : "") + "\n");
  }
}

function eq(name, actual, expected) {
  check(name, actual === expected, "期望 " + JSON.stringify(expected) + "，实际 " + JSON.stringify(actual));
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** 等页面上的异步链跑完。留够时间，避免"偶发失败"变成噪声。 */
async function flush(rounds) {
  for (let i = 0; i < (rounds || 10); i++) {
    await sleep(15);
  }
}

// ---------------------------------------------------------------------------
// 假后端
// ---------------------------------------------------------------------------

/**
 * 假 Response。必须**同时**实现 text() 与 json() ——
 * 前端探测基地址走的是 r.json()，只给 text() 的话探测会全部失败，
 * 表现成"页面打开但数据全空"，很容易误判成产品 bug。
 */
function makeResponse(status, payload) {
  const body = typeof payload === "string" ? payload : JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(body),
    json: () => Promise.resolve(JSON.parse(body)),
  };
}

/** v2.0 默认后端：已连接 OpenList，本地/云端各两行，队列空。 */
function makeBackend(overrides) {
  const state = {
    calls: [],
    tasks: [],
    paused: false,
    enqueuePayload: null,
    mkdirPayload: null,
    retryPayload: null,
    downloadPayload: null,
    localRemovePayload: null,
    cloudRemovePayload: null,
    syncCopyPayload: null,
    comparePayload: null,
    // v2.9.0：compare 现在同时给出「顶层差异」（localExtra/cloudExtra/mismatch），
    // 界面就是照着这三组渲染的。
    // v2.9.1：多了第四组 `same`（已同步，只读）。`sameCount` 是**全量**文件数
    // （可能大于 same.length —— 明细有回传上限），徽标必须用它而不是列表长度。
    // v2.9.2：条目带 `absPath`（它**在自己那一侧**的绝对路径）与 `owner`
    // （absPath 属于左还是右）。两侧都可能是云端之后，前端不能再靠栏位拼路径。
    compareResult: {
      notUploaded: [{ path: "ShowA/a.mkv", name: "a.mkv", size: 2048 }],
      sizeMismatch: [{ path: "ShowB/b.mkv", name: "b.mkv", size: 4096, cloudSize: 1000 }],
      uploaded: [{ path: "done1.mkv", name: "done1.mkv", size: 1024, isDir: false },
                 { path: "done2.txt", name: "done2.txt", size: 10, isDir: false }],
      cloudOnly: [{ path: "cloud-only.txt", size: 5 }],
      notUploadedCount: 1,
      sizeMismatchCount: 1,
      uploadedFiles: 2,
      uploadedDirs: 1,
      uploadedTruncated: false,
      cloudOnlyCount: 1,
      pendingBytes: 6144,
      localExtra: [{ path: "ShowA/a.mkv", name: "a.mkv", size: 2048, isDir: false,
                     absPath: "/vol1/1000/ShowA/a.mkv", owner: "left" }],
      cloudExtra: [{ path: "cloud-only.txt", name: "cloud-only.txt", size: 5, isDir: false,
                     absPath: "/cloud-only.txt", owner: "right" },
                   { path: "Extra", name: "Extra", size: 0, isDir: true,
                     absPath: "/Extra", owner: "right" }],
      mismatch: [{ path: "ShowB/b.mkv", name: "b.mkv", size: 4096, cloudSize: 1000,
                   isDir: false, absPath: "/vol1/1000/ShowB/b.mkv",
                   peerAbsPath: "/ShowB/b.mkv", owner: "left" }],
      same: [{ path: "done1.mkv", name: "done1.mkv", size: 1024, isDir: false },
             { path: "done2.txt", name: "done2.txt", size: 10, isDir: false }],
      localExtraCount: 1, cloudExtraCount: 2, mismatchCount: 1,
      sameCount: 2, sameListed: 2,
      localExtraBytes: 2048, cloudExtraBytes: 5, mismatchBytes: 4096,
    },
  };

  function respond(pathname, query, method, init) {
    switch (pathname) {
      case "api/info":
        return { ok: true, app: APP, version: "2.0.0" };
      case "api/connection":
        return { ok: true, connection: {
          online: true, configured: true,
          baseUrl: "http://192.168.0.10:5244", username: "admin",
          user: { username: "admin" } } };
      case "api/config":
        if (method === "POST") return { ok: true, config: JSON.parse(init.body) };
        return { ok: true, config: {
          localRoot: "", cloudRoot: "/", uploadConcurrency: 2, maxAttempts: 3,
          skipExisting: true, verifyAfterUpload: true, scanDepth: 3,
          excludeNames: ["*.tmp"] } };
      case "api/local/list":
        return { ok: true, path: "/vol1/1000", aggregate: false, entries: [
          { name: "ShowA", path: "/vol1/1000/ShowA", isDir: true, size: 0, mtime: 1700000000 },
          { name: "a.mkv", path: "/vol1/1000/a.mkv", isDir: false, size: 2048, mtime: 1700000100 },
        ] };
      case "api/cloud/list":
        return { ok: true, path: "/", entries: [
          { name: "ShowA", path: "/ShowA", isDir: true, size: 0, modified: "2026-09-01T10:00:00" },
          { name: "b.mkv", path: "/b.mkv", isDir: false, size: 512, modified: "2026-09-02T10:00:00" },
        ] };
      case "api/upload/list":
        return { ok: true, tasks: state.tasks,
                 counts: { done: 0, failed: 0, pending: 0, uploading: 0 },
                 paused: !!state.paused };
      case "api/log":
        return { ok: true, lines: ["line1\n", "line2\n"] };
      default:
        return null;
    }
  }

  function respondPost(pathname, init) {
    switch (pathname) {
      case "api/connect":
        return { ok: true, connection: { online: true, configured: true,
                 baseUrl: "http://192.168.0.10:5244", username: "admin",
                 user: { username: "admin" } } };
      case "api/connect/test":
        return { ok: true, diagnose: { steps: [
          { ok: true, name: "网络", detail: "通" },
          { ok: true, name: "登录", detail: "成功" } ] } };
      case "api/disconnect":
        return { ok: true };
      case "api/upload/enqueue": {
        state.enqueuePayload = JSON.parse(init.body);
        const n = state.enqueuePayload.items
          ? state.enqueuePayload.items.length
          : (state.enqueuePayload.localPaths || []).length;
        return { ok: true, added: n };
      }
      case "api/upload/retry": {
        state.retryPayload = JSON.parse(init.body);
        if (state.retryPayload && state.retryPayload.all) {
          const nf = state.tasks.filter((t) => t.status === "failed").length;
          return { ok: true, retried: nf };
        }
        return { ok: true };
      }
      case "api/upload/cancel":
      case "api/upload/remove":
      case "api/upload/clear":
        return { ok: true };
      case "api/upload/pause": {
        const body = JSON.parse(init.body);
        state.paused = !!body.paused;
        return { ok: true, paused: state.paused };
      }
      case "api/cloud/mkdir":
        state.mkdirPayload = JSON.parse(init.body);
        return { ok: true };
      case "api/compare": {
        const req = JSON.parse(init.body);
        state.comparePayload = req;
        // 请求里两侧各带 side；服务端原样回传，前端据此决定文案与动作方向。
        const L = req.left || { side: "local", path: "/vol1/1000" };
        const R = req.right || { side: "cloud", path: "/" };
        return { ok: true, left: L, right: R, depth: 3,
                 localRoot: L.side === "local" ? L.path : (R.side === "local" ? R.path : ""),
                 cloudRoot: L.side === "cloud" ? L.path : (R.side === "cloud" ? R.path : "/"),
                 result: state.compareResult };
      }
      case "api/sync/copy": {
        state.syncCopyPayload = JSON.parse(init.body);
        const n = (state.syncCopyPayload.items || []).length;
        return { ok: true, side: state.syncCopyPayload.side, copied: n,
                 target: state.syncCopyPayload.target, failed: [], tasks: [] };
      }
      case "api/download/enqueue": {
        state.downloadPayload = JSON.parse(init.body);
        const n = (state.downloadPayload.items || []).length;
        return { ok: true, added: n };
      }
      case "api/local/remove": {
        state.localRemovePayload = JSON.parse(init.body);
        const paths = state.localRemovePayload.paths || [];
        return { ok: true, removed: paths.length, paths: paths, failed: [] };
      }
      case "api/cloud/remove": {
        state.cloudRemovePayload = JSON.parse(init.body);
        const paths = state.cloudRemovePayload.paths || [];
        return { ok: true, removed: paths.length, paths: paths, failed: [] };
      }
      case "api/config":
        return { ok: true, config: JSON.parse(init.body) };
      default:
        return null;
    }
  }

  const backend = function (url, init) {
    state.calls.push({ url, method: (init && init.method) || "GET",
                       body: init && init.body ? JSON.parse(init.body) : null });

    // 严格网关：只放行本应用前缀
    if (!url.startsWith(ALLOWED_PREFIX)) {
      return Promise.resolve(makeResponse(404, "not found"));
    }

    const rest = url.slice(ALLOWED_PREFIX.length);
    const split = rest.indexOf("?");
    const pathname = split < 0 ? rest : rest.slice(0, split);
    const query = split < 0 ? "" : rest.slice(split + 1);
    const method = (init && init.method) || "GET";

    let payload;
    if (overrides && overrides[pathname]) {
      payload = overrides[pathname](query, method, init);
    } else if (method === "POST") {
      payload = respondPost(pathname, init);
    } else {
      payload = respond(pathname, query, method, init);
    }

    if (!payload) {
      return Promise.resolve(makeResponse(404, "not found"));
    }
    return Promise.resolve(makeResponse(200, payload));
  };

  backend.state = state;
  return backend;
}

// ---------------------------------------------------------------------------
// 建页面
// ---------------------------------------------------------------------------

function makeWindow(options) {
  options = options || {};
  const backend = options.backend || makeBackend();
  const virtualConsole = new VirtualConsole();
  const pageErrors = [];
  virtualConsole.on("jsdomError", (err) => pageErrors.push(String(err && err.message || err)));

  const nativeCalls = [];

  // v2.8.5：记录页面每次 setInterval 的间隔值。
  // 「轮询自适应」的节奏存在 `let taskInterval` 里 —— 顶层 let/const 不会挂到
  // window 上（jsdom 与浏览器一致），拿不到。但它的**可观察副作用**是去调
  // setInterval(fn, N)，所以从间隔值反推，既确定性又不必真等 8 秒。
  const intervals = [];

  const dom = new JSDOM(HTML, {
    url: options.url || (ORIGIN + "/app/" + APP),
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole,
    beforeParse(window) {
      // fetch 收到的可能是**相对地址**（前端探测基地址时就是这么调的）。
      // 真实浏览器会按文档地址解析，桩必须自己补上这一步 ——
      // 否则 '/' 开头的路径被当成绝对地址比较，探测永远失败。
      window.fetch = function (url, init) {
        const absolute = /^https?:/i.test(url)
          ? url
          : new URL(url, window.location.href).href;
        return backend(absolute, init);
      };
      window.alert = (m) => { nativeCalls.push(["alert", m]); };
      window.confirm = (m) => { nativeCalls.push(["confirm", m]); return false; };
      window.prompt = (m) => { nativeCalls.push(["prompt", m]); return null; };
      // 预置 localStorage（模拟"上次运行记住了状态"）；必须在 breakStorage 之前
      if (options.storage) {
        Object.keys(options.storage).forEach((k) => {
          try { window.localStorage.setItem(k, options.storage[k]); } catch (e) {}
        });
      }
      if (options.breakStorage) {
        Object.defineProperty(window, "localStorage", {
          configurable: true,
          get() {
            throw new window.DOMException("denied by sandbox", "SecurityError");
          },
        });
      }
      // 只在用例显式要求时包一层：默认不干预页面计时器，
      // 免得给既有用例引入难以察觉的行为变化。
      if (options.captureTimers) {
        const realSetInterval = window.setInterval.bind(window);
        window.setInterval = function (fn, delay) {
          intervals.push(delay);
          return realSetInterval(fn, delay);
        };
      }
    },
  });

  return { dom, window: dom.window, document: dom.window.document,
           backend, nativeCalls, pageErrors, intervals };
}

// ---------------------------------------------------------------------------
// 用例
// ---------------------------------------------------------------------------

async function testBaseDetection() {
  console.log("\n[基地址探测 · 已连接]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  check("连接面板已隐藏", doc.getElementById("page-connect").classList.contains("hidden"));
  eq("本地列表 2 行", doc.querySelectorAll("#localList .flist-row").length, 2);
  eq("云端列表 2 行", doc.querySelectorAll("#cloudList .flist-row").length, 2);
  check("启动过程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testBaseFallback() {
  console.log("\n[基地址探测 · 带文件名的入口]");
  // 入口 URL 带着 index.html 时，第一个候选（当目录用）必然失败，
  // 必须靠正则从 /app/<name> 里取——这条路径直接对应那个"满屏 404"的历史 bug。
  const env = makeWindow({ url: ORIGIN + "/app/" + APP + "/index.html" });
  await flush();
  const rows = env.document.querySelectorAll("#localList .flist-row");
  check("列表有内容（说明探测兜到了正确前缀）", rows.length === 2,
        "渲染了 " + rows.length + " 行");
  env.window.close();
}

async function testUnconnectedShowsConnectPanel() {
  console.log("\n[未连接时弹连接面板]");
  const backend = makeBackend({
    "api/connection": () => ({ ok: true, connection: {
      online: false, configured: false, baseUrl: "", username: "" } }),
  });
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  check("连接面板可见", !doc.getElementById("page-connect").classList.contains("hidden"));
  check("未连接时传输页被隐藏（连接面板独占显示）",
        doc.getElementById("page-transfer").classList.contains("hidden"));
  check("未连接时设置页被隐藏",
        doc.getElementById("page-settings").classList.contains("hidden"));

  // 填表登录
  doc.getElementById("inUrl").value = "http://192.168.0.10:5244";
  doc.getElementById("inUser").value = "admin";
  doc.getElementById("inPass").value = "secret";
  doc.getElementById("btnDoLogin").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  const login = env.backend.state.calls.find((c) => c.url.endsWith("/api/connect"));
  check("发出了登录请求", !!login);
  check("请求体带了地址/用户名/密码",
        login && login.body && login.body.baseUrl === "http://192.168.0.10:5244" &&
        login.body.username === "admin" && login.body.password === "secret",
        JSON.stringify(login && login.body));
  check("登录后面板隐藏", doc.getElementById("page-connect").classList.contains("hidden"));
  check("登录后传输页恢复显示（连接面板退出）",
        !doc.getElementById("page-transfer").classList.contains("hidden"));
  check("登录后设置页仍隐藏（恢复的是当前标签页）",
        doc.getElementById("page-settings").classList.contains("hidden"));
  env.window.close();
}

async function testConnectPanelExclusiveWithTabs() {
  console.log("\n[已连接时连接面板与标签页互斥]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  check("启动后传输页可见（前置条件）",
        !doc.getElementById("page-transfer").classList.contains("hidden"));

  // 已连接状态下手动触发 showConnect → 应只显示连接面板
  env.window.showConnect();
  check("showConnect 后连接面板可见",
        !doc.getElementById("page-connect").classList.contains("hidden"));
  check("点连接按钮后传输页被隐藏",
        doc.getElementById("page-transfer").classList.contains("hidden"));
  check("点连接按钮后设置页被隐藏",
        doc.getElementById("page-settings").classList.contains("hidden"));

  // 在连接面板显示时点任一标签页 → 应退出连接面板回到该标签页
  doc.querySelector('[data-tab="settings"]').dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  check("点标签页后连接面板退出",
        doc.getElementById("page-connect").classList.contains("hidden"));
  check("点标签页后切到对应面板",
        !doc.getElementById("page-settings").classList.contains("hidden"));
  check("点标签页后传输页保持隐藏",
        doc.getElementById("page-transfer").classList.contains("hidden"));

  // 再回传输页，上传按钮所在面板应恢复
  doc.querySelector('[data-tab="transfer"]').dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  check("切回传输页后上传按钮面板恢复",
        !doc.getElementById("page-transfer").classList.contains("hidden"));
  check("上传按钮在 DOM 中且不带 hidden 类",
        !doc.getElementById("btnUpload").classList.contains("hidden"));
  env.window.close();
}

async function testSelectAndEnqueue() {
  console.log("\n[勾选本地上传到云端目录]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  const fileRow = Array.from(doc.querySelectorAll("#localList .flist-row"))
    .find((el) => el.textContent.indexOf("a.mkv") >= 0);
  check("找得到本地文件行", !!fileRow);
  fileRow.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  eq("选择计数更新", doc.getElementById("selInfo").textContent, "已选 1 项");

  doc.getElementById("btnUpload").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  const payload = env.backend.state.enqueuePayload;
  check("发出了入队请求", !!payload);
  check("走的是 localPaths + cloudDir 模式",
        payload && Array.isArray(payload.localPaths) && payload.cloudDir === "/",
        JSON.stringify(payload));
  check("勾选的文件在清单里",
        payload && payload.localPaths.indexOf("/vol1/1000/a.mkv") >= 0,
        JSON.stringify(payload && payload.localPaths));
  check("提示了加入数量", doc.getElementById("toast").textContent.indexOf("已加入") >= 0,
        doc.getElementById("toast").textContent);
  eq("入队后选择被清空", doc.getElementById("selInfo").textContent, "未选择");
  env.window.close();
}

async function testQueueRenderingAndRetry() {
  console.log("\n[传输列表渲染与手动重传]");
  const backend = makeBackend();
  backend.state.tasks = [
    { id: "t1", name: "big.mkv", remotePath: "/big.mkv", size: 1000, uploaded: 500,
      status: "uploading", speed: 1048576 },
    { id: "t2", name: "fail.mkv", remotePath: "/fail.mkv", size: 100, uploaded: 0,
      status: "failed", error: "网络错误", attempts: 3 },
    { id: "t3", name: "ok.mkv", remotePath: "/ok.mkv", size: 10, uploaded: 10,
      status: "done" },
  ];
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;

  const rows = doc.querySelectorAll("#queueBody .qrow");
  eq("队列渲染 3 行", rows.length, 3);
  check("上传中的任务有进度条", !!doc.querySelector("#queueBody .prog"));
  const text = doc.getElementById("queueBody").textContent;
  check("进度百分比显示出来", text.indexOf("50%") >= 0, text);
  check("失败原因显示出来", text.indexOf("网络错误") >= 0, text);

  // 失败任务的「重传」按钮
  const retryBtn = Array.from(doc.querySelectorAll("#queueBody .linkbtn"))
    .find((b) => b.textContent === "重传");
  check("失败任务有重传按钮", !!retryBtn);
  retryBtn.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  check("重传请求带了任务 id",
        env.backend.state.retryPayload && env.backend.state.retryPayload.id === "t2",
        JSON.stringify(env.backend.state.retryPayload));
  env.window.close();
}

async function testRetryAllButton() {
  console.log("\n[全部重试按钮：仅有失败项时出现]");
  // 无失败项 → 隐藏
  const b1 = makeBackend();
  b1.state.tasks = [
    { id: "t1", name: "a.mkv", remotePath: "/a.mkv", size: 10, uploaded: 10, status: "done" },
    { id: "t2", name: "b.mkv", remotePath: "/b.mkv", size: 10, uploaded: 0, status: "pending" },
  ];
  const env1 = makeWindow({ backend: b1 });
  await flush();
  check("无失败项时「全部重试」不可见",
        env1.document.getElementById("btnRetryAll").classList.contains("hidden"),
        env1.document.getElementById("btnRetryAll").className);
  env1.window.close();

  // 有失败项 → 可见，点击发出 {all:true}
  const b2 = makeBackend();
  b2.state.tasks = [
    { id: "t1", name: "a.mkv", remotePath: "/a.mkv", size: 10, uploaded: 10, status: "done" },
    { id: "t2", name: "b.mkv", remotePath: "/b.mkv", size: 10, uploaded: 0, status: "failed", error: "连接超时" },
  ];
  const env2 = makeWindow({ backend: b2 });
  await flush();
  const doc = env2.document;
  check("有失败项时「全部重试」可见",
        !doc.getElementById("btnRetryAll").classList.contains("hidden"));
  doc.getElementById("btnRetryAll").dispatchEvent(
    new env2.window.MouseEvent("click", { bubbles: true }));
  await flush();
  const body = env2.backend.state.retryPayload;
  check("点击发出 {all:true}", !!(body && body.all === true), JSON.stringify(body));
  check("提示了重试数量", doc.getElementById("toast").textContent.indexOf("已重新加入") >= 0,
        doc.getElementById("toast").textContent);
  env2.window.close();
}

async function testQueueFilter() {
  console.log("\n[队列筛选]");
  const backend = makeBackend();
  backend.state.tasks = [
    { id: "t1", name: "up.mkv", remotePath: "/up.mkv", size: 100, uploaded: 50, status: "uploading" },
    { id: "t2", name: "bad.mkv", remotePath: "/bad.mkv", size: 10, uploaded: 0, status: "failed", error: "err" },
    { id: "t3", name: "ok.mkv", remotePath: "/ok.mkv", size: 10, uploaded: 10, status: "done" },
  ];
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  const clickFilter = (f) => doc.querySelector('#queueFilter .fbtn[data-filter="' + f + '"]')
    .dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));

  eq("默认「全部」：3 行", doc.querySelectorAll("#queueBody .qrow").length, 3);
  clickFilter("failed");
  eq("筛选「失败」：只 1 行", doc.querySelectorAll("#queueBody .qrow").length, 1);
  check("失败筛选只留失败行",
        doc.querySelector("#queueBody .qrow").textContent.indexOf("bad.mkv") >= 0);
  clickFilter("done");
  eq("筛选「已完成」：1 行", doc.querySelectorAll("#queueBody .qrow").length, 1);
  clickFilter("active");
  eq("筛选「进行中」：1 行", doc.querySelectorAll("#queueBody .qrow").length, 1);
  clickFilter("all");
  eq("切回「全部」恢复 3 行", doc.querySelectorAll("#queueBody .qrow").length, 3);
  env.window.close();

  // 无匹配结果 → 空状态文案
  const backend2 = makeBackend();
  backend2.state.tasks = [
    { id: "t1", name: "ok.mkv", remotePath: "/ok.mkv", size: 10, uploaded: 10, status: "done" },
  ];
  const env2 = makeWindow({ backend: backend2 });
  await flush();
  const doc2 = env2.document;
  doc2.querySelector('#queueFilter .fbtn[data-filter="failed"]').dispatchEvent(
    new env2.window.MouseEvent("click", { bubbles: true }));
  eq("无匹配时显示空状态", doc2.querySelectorAll("#queueBody .qrow").length, 0);
  check("空状态文案正确",
        doc2.getElementById("queueBody").textContent.indexOf("没有符合条件的任务") >= 0,
        doc2.getElementById("queueBody").textContent);
  env2.window.close();
}

async function testQueueStatusColors() {
  console.log("\n[队列状态分色徽标]");
  const backend = makeBackend();
  backend.state.tasks = [
    { id: "t1", name: "bad.mkv", remotePath: "/bad.mkv", size: 10, uploaded: 0, status: "failed", error: "连接超时：无法连接服务器" },
    { id: "t2", name: "ok.mkv", remotePath: "/ok.mkv", size: 10, uploaded: 10, status: "done" },
  ];
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  const failedBadge = doc.querySelector("#queueBody .qstat.st-failed");
  check("失败行状态带 st-failed 类", !!failedBadge);
  check("成功行状态带 st-done 类", !!doc.querySelector("#queueBody .qstat.st-done"));
  check("失败徽标 title 给全文",
        failedBadge && failedBadge.getAttribute("title") === "连接超时：无法连接服务器",
        failedBadge && failedBadge.getAttribute("title"));
  check("头部统计按状态分色",
        !!doc.querySelector("#queueStat .st-done") && !!doc.querySelector("#queueStat .st-failed"));
  env.window.close();
}

async function testFormMemory() {
  console.log("\n[表单记忆：浏览位置 / 对比参数 / 设置草稿 / 筛选]");
  const preset = {
    localPath: "/vol1/1000/Sub",
    cloudPath: "/ShowA",
    cmpLocal: "/vol1/1000/Cmp",
    cmpCloud: "/cld",
    cmpDepth: "5",
    cfgDraft: { cfgConc: "6" },
    queueFilter: "failed",
  };
  const backend = makeBackend({
    "api/local/list": (query) => {
      const m = /path=([^&]*)/.exec(query || "");
      const p = m ? decodeURIComponent(m[1]) : "";
      return { ok: true, path: p || "/vol1/1000", aggregate: !p, entries: [
        { name: "a.mkv", path: (p || "/vol1/1000") + "/a.mkv", isDir: false, size: 1, mtime: 1 }] };
    },
  });
  backend.state.tasks = [
    { id: "t1", name: "bad.mkv", remotePath: "/bad.mkv", size: 10, uploaded: 0, status: "failed", error: "e" },
    { id: "t2", name: "ok.mkv", remotePath: "/ok.mkv", size: 10, uploaded: 10, status: "done" },
  ];
  const env = makeWindow({ backend, storage: { dd_ui_state: JSON.stringify(preset) } });
  await flush();
  const doc = env.document;

  const localCall = env.backend.state.calls.find((c) => c.url.indexOf("/api/local/list") >= 0);
  check("本地浏览位置被恢复（按记忆路径请求）",
        !!localCall && decodeURIComponent(localCall.url).indexOf("/vol1/1000/Sub") >= 0,
        localCall && localCall.url);
  const cloudCall = env.backend.state.calls.find((c) => c.url.indexOf("/api/cloud/list") >= 0);
  check("云端浏览位置被恢复",
        !!cloudCall && decodeURIComponent(cloudCall.url).indexOf("/ShowA") >= 0,
        cloudCall && cloudCall.url);
  eq("对比本地目录回填", doc.getElementById("cmpLocal").value, "/vol1/1000/Cmp");
  eq("对比云端目录回填", doc.getElementById("cmpCloud").value, "/cld");
  eq("对比深度回填", doc.getElementById("cmpDepth").value, "5");
  eq("未保存的设置草稿回填", doc.getElementById("cfgConc").value, "6");
  check("队列筛选状态被记忆（失败）",
        doc.querySelector('#queueFilter .fbtn[data-filter="failed"]').classList.contains("active"));
  eq("记忆筛选生效（只渲染失败行）", doc.querySelectorAll("#queueBody .qrow").length, 1);

  // 改值后写盘（防抖 300ms）
  doc.getElementById("cmpLocal").value = "/vol1/1000/New";
  doc.getElementById("cmpLocal").dispatchEvent(new env.window.Event("change", { bubbles: true }));
  doc.getElementById("cfgRetry").value = "7";
  doc.getElementById("cfgRetry").dispatchEvent(new env.window.Event("change", { bubbles: true }));
  await sleep(500);
  const saved = JSON.parse(env.window.localStorage.getItem("dd_ui_state"));
  eq("对比参数写入本地存储", saved.cmpLocal, "/vol1/1000/New");
  eq("设置草稿写入本地存储", saved.cfgDraft && saved.cfgDraft.cfgRetry, "7");
  check("未持久化密码字段", saved.password === undefined && saved.otp === undefined,
        JSON.stringify(saved));
  env.window.close();
}

async function testFormMemoryStorageDisabled() {
  console.log("\n[存储禁用时记忆逻辑不炸]");
  const env = makeWindow({ breakStorage: true });
  await flush();
  const doc = env.document;
  doc.getElementById("cmpLocal").value = "/x";
  doc.getElementById("cmpLocal").dispatchEvent(new env.window.Event("change", { bubbles: true }));
  doc.getElementById("cfgConc").value = "8";
  doc.getElementById("cfgConc").dispatchEvent(new env.window.Event("change", { bubbles: true }));
  await sleep(400);
  check("禁用存储时页面照常渲染", doc.querySelectorAll("#localList .flist-row").length === 2,
        "渲染了 " + doc.querySelectorAll("#localList .flist-row").length + " 行");
  check("禁用存储时没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testCompareFlow() {
  console.log("\n[目录比对已并入文件传输页]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  check("不再有「目录对比」标签页", !doc.querySelector('[data-tab="compare"]'));
  check("没有独立的 page-compare 页", !doc.getElementById("page-compare"));
  check("比对参数就在传输页里", !!doc.getElementById("cmpLocal") &&
        !!doc.getElementById("cmpCloud") && !!doc.getElementById("btnCompare"));
  check("差异面板默认隐藏（没比对就没有结果）",
        doc.getElementById("diffPane").classList.contains("hidden"));

  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpCloud").value = "/";
  doc.getElementById("btnCompare").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  check("比对后差异面板出现",
        !doc.getElementById("diffPane").classList.contains("hidden"));
  const head = doc.getElementById("diffHead").textContent;
  check("统计里有「仅本地」", head.indexOf("仅本地") >= 0, head);
  check("统计里有「仅云端」", head.indexOf("仅云端") >= 0, head);
  check("统计里有「大小不一致」", head.indexOf("大小不一致") >= 0, head);

  const out = doc.getElementById("cmpOut").textContent;
  check("结果里能看出本地多出的文件", out.indexOf("ShowA/a.mkv") >= 0, out);
  check("结果里能看出云端多出的文件", out.indexOf("cloud-only.txt") >= 0, out);
  check("结果里标出大小不一致的行", out.indexOf("ShowB/b.mkv") >= 0, out);
  check("云端多出的目录也列出来了", out.indexOf("Extra") >= 0, out);
  eq("三组差异行合计 4 行", doc.querySelectorAll("#cmpOut .drow").length, 4);
  eq("每个分组都有表头", doc.querySelectorAll("#cmpOut .dgrp-head").length, 3);

  env.window.close();
}

async function testDiffBatchUpload() {
  console.log("\n[差异结果 · 批量上传]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpCloud").value = "/";
  doc.getElementById("btnCompare").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  const upBtn = doc.getElementById("diffUpload");
  check("默认没有勾选 → 上传按钮禁用", upBtn.disabled === true);

  doc.getElementById("diffAll").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  check("全选后上传按钮可用", doc.getElementById("diffUpload").disabled === false);
  check("全选后下载按钮可用", doc.getElementById("diffDownload").disabled === false);
  check("全选后清除按钮可用", doc.getElementById("diffClear").disabled === false);
  eq("全选勾上了 4 行", doc.querySelectorAll("#cmpOut .dchk:checked").length, 4);

  // 「大小不一致」行会先弹确认框（覆盖是破坏性动作）
  doc.getElementById("diffUpload").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await sleep(30);
  check("覆盖前弹了确认框", !doc.getElementById("overlay").classList.contains("hidden"));
  check("全程没有调用原生 alert", env.nativeCalls.length === 0);
  doc.getElementById("dlgOk").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  const payload = env.backend.state.enqueuePayload;
  check("发出了上传队列请求", !!payload, JSON.stringify(payload));
  // 仅本地 1 项 + 大小不一致 1 项（云端多出的不参与上传）
  eq("只把可上传的 2 项入队", payload && payload.items.length, 2);
  const localItem = payload.items.find((i) => i.localPath.indexOf("a.mkv") >= 0);
  check("本地路径拼对了",
        localItem && localItem.localPath === "/vol1/1000/ShowA/a.mkv",
        JSON.stringify(localItem));
  check("云端路径拼对了",
        localItem && localItem.remotePath === "/ShowA/a.mkv",
        JSON.stringify(localItem));
  check("没有把「仅云端」的项也传上去",
        !payload.items.some((i) => i.localPath.indexOf("cloud-only") >= 0),
        JSON.stringify(payload.items));

  eq("已提交的行从结果里摘掉（剩云端多出的 2 行）",
     doc.querySelectorAll("#cmpOut .drow").length, 2);
  env.window.close();
}

async function testDiffBatchDownload() {
  console.log("\n[差异结果 · 批量下载]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpCloud").value = "/";
  doc.getElementById("btnCompare").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  doc.getElementById("diffAll").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  doc.getElementById("diffDownload").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await sleep(30);
  doc.getElementById("dlgOk").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  const payload = env.backend.state.downloadPayload;
  check("发出了下载队列请求", !!payload, JSON.stringify(payload));
  eq("云端多出 2 项 + 不一致 1 项 = 3 项", payload && payload.items.length, 3);
  const dirItem = payload.items.find((i) => i.remotePath === "/Extra");
  check("目录项带上了 isDir（服务端才知道要递归展开）",
        dirItem && dirItem.isDir === true, JSON.stringify(dirItem));
  check("目录落到本地对应路径",
        dirItem && dirItem.localPath === "/vol1/1000/Extra", JSON.stringify(dirItem));
  const fileItem = payload.items.find((i) => i.remotePath === "/cloud-only.txt");
  check("云端文件路径正确", !!fileItem, JSON.stringify(payload.items));
  check("带了云端大小（用于下载后校验）",
        fileItem && fileItem.size === 5, JSON.stringify(fileItem));
  check("没有把「仅本地」的项也下载下来",
        !payload.items.some((i) => i.remotePath.indexOf("a.mkv") >= 0));
  env.window.close();
}

async function testDiffClearNeedsConfirm() {
  console.log("\n[差异结果 · 清除必须二次确认，且只清多余项]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpCloud").value = "/";
  doc.getElementById("btnCompare").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  doc.getElementById("diffAll").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  doc.getElementById("diffClear").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await sleep(30);

  const dlg = doc.getElementById("overlay");
  check("弹出确认框", !dlg.classList.contains("hidden"));
  check("确认文案写明不可撤销",
        doc.getElementById("dlgBody").textContent.indexOf("不可撤销") >= 0,
        doc.getElementById("dlgBody").textContent);
  check("确认文案列了样例路径",
        doc.getElementById("dlgBody").textContent.indexOf("a.mkv") >= 0);

  // 先取消 → 一个请求都不该发
  doc.getElementById("dlgCancel").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  check("取消后没有删除本地",
        env.backend.state.localRemovePayload === null);
  check("取消后没有删除云端",
        env.backend.state.cloudRemovePayload === null);

  // 再确认 → 两侧各删各的多余项
  doc.getElementById("diffClear").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await sleep(30);
  doc.getElementById("dlgOk").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  const lp = env.backend.state.localRemovePayload;
  const cp = env.backend.state.cloudRemovePayload;
  eq("本地只删「仅本地」那 1 项", lp && lp.paths.length, 1);
  check("本地删的是绝对路径",
        lp && lp.paths[0] === "/vol1/1000/ShowA/a.mkv", JSON.stringify(lp));
  eq("云端只删「仅云端」那 2 项", cp && cp.paths.length, 2);
  check("云端删的是云端路径",
        cp && cp.paths.indexOf("/cloud-only.txt") >= 0 && cp.paths.indexOf("/Extra") >= 0,
        JSON.stringify(cp));
  check("两侧都没有把「大小不一致」当多余项删掉",
        lp.paths.indexOf("/vol1/1000/ShowB/b.mkv") < 0 &&
        cp.paths.indexOf("/ShowB/b.mkv") < 0,
        JSON.stringify([lp, cp]));
  env.window.close();
}

async function testDiffRowActions() {
  console.log("\n[差异结果 · 单行手动操作]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpCloud").value = "/";
  doc.getElementById("btnCompare").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  // 云端多出的那个文件，点它自己的「下载」
  const row = Array.from(doc.querySelectorAll("#cmpOut .drow"))
    .find((r) => r.textContent.indexOf("cloud-only.txt") >= 0);
  check("找得到云端多出的行", !!row);
  const dl = Array.from(row.querySelectorAll("button"))
    .find((b) => b.textContent === "下载");
  check("行上带「下载」按钮", !!dl);
  dl.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  const payload = env.backend.state.downloadPayload;
  eq("只下载这一行", payload && payload.items.length, 1);
  check("下的是这个文件",
        payload && payload.items[0].remotePath === "/cloud-only.txt",
        JSON.stringify(payload));

  // 「大小不一致」行给的是覆盖方向，单行点下去不再重复弹确认框
  const misRow = Array.from(doc.querySelectorAll("#cmpOut .drow"))
    .find((r) => r.textContent.indexOf("ShowB/b.mkv") >= 0);
  const cover = Array.from(misRow.querySelectorAll("button"))
    .find((b) => b.textContent === "用云端覆盖");
  check("不一致行给的是「用云端覆盖」", !!cover);
  env.backend.state.enqueuePayload = null;
  cover.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  check("单行覆盖没有弹确认框",
        doc.getElementById("overlay").classList.contains("hidden"));
  check("覆盖不会误发上传请求", env.backend.state.enqueuePayload === null,
        JSON.stringify(env.backend.state.enqueuePayload));
  const coverPayload = env.backend.state.downloadPayload;
  eq("覆盖的是这一行", coverPayload && coverPayload.items.length, 1);
  check("方向是「云端 → 本地」",
        coverPayload && coverPayload.items[0].remotePath === "/ShowB/b.mkv" &&
        coverPayload.items[0].localPath === "/vol1/1000/ShowB/b.mkv",
        JSON.stringify(coverPayload && coverPayload.items[0]));
  env.window.close();
}

async function compareOnce(env, doc) {
  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpCloud").value = "/";
  doc.getElementById("btnCompare").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
}

function filterTap(env, doc) {
  return async (key) => {
    const chip = doc.querySelector('#diffHead [data-filter="' + key + '"]');
    chip.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
    await flush();
  };
}

async function testDiffStatusFilter() {
  console.log("\n[差异结果 · 状态标签是筛选开关]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  await compareOnce(env, doc);

  const chips = () => Array.from(doc.querySelectorAll("#diffHead [data-filter]"));
  eq("四个状态标签都在", chips().length, 4);
  check("标签是可点的 button", chips().every((b) => b.tagName === "BUTTON"));
  eq("默认没有标签处于选中态", doc.querySelectorAll("#diffHead .dstat.on").length, 0);
  eq("默认渲染三个分组头", doc.querySelectorAll("#cmpOut .dgrp-head").length, 3);
  eq("默认渲染 4 行差异", doc.querySelectorAll("#cmpOut .drow").length, 4);
  check("默认不铺开「已同步」（否则差异会被已同步的条目淹没）",
        doc.getElementById("cmpOut").textContent.indexOf("已同步（") < 0);

  const tap = filterTap(env, doc);
  const nCompare = () => env.backend.state.calls
    .filter((c) => c.url.indexOf("/api/compare") >= 0).length;
  const before = nCompare();

  // --- 点「仅本地」→ 只看这一类 ---
  await tap("local");
  eq("筛「仅本地」只剩 1 行", doc.querySelectorAll("#cmpOut .drow").length, 1);
  eq("筛「仅本地」只剩 1 个分组头", doc.querySelectorAll("#cmpOut .dgrp-head").length, 1);
  check("列表里就是本地多出的那条",
        doc.getElementById("cmpOut").textContent.indexOf("ShowA/a.mkv") >= 0);
  const localChip = doc.querySelector('#diffHead [data-filter="local"]');
  check("被选中的标签有选中态", localChip.classList.contains("on"));
  eq("选中态对辅助技术可见（aria-pressed）", localChip.getAttribute("aria-pressed"), "true");
  eq("同时只有一个标签被选中", doc.querySelectorAll("#diffHead .dstat.on").length, 1);
  check("切换筛选没有重新请求后端（纯前端重绘）", nCompare() === before,
        "比对请求数 " + before + " → " + nCompare());

  // --- 再点同一枚 → 恢复完整列表 ---
  await tap("local");
  eq("再点一次恢复 4 行", doc.querySelectorAll("#cmpOut .drow").length, 4);
  eq("再点一次恢复 3 个分组头", doc.querySelectorAll("#cmpOut .dgrp-head").length, 3);
  eq("取消后没有标签处于选中态", doc.querySelectorAll("#diffHead .dstat.on").length, 0);

  // --- 互斥：直接点另一枚就切过去，不会叠加 ---
  await tap("local");
  await tap("cloud");
  eq("点另一枚直接切过去（仍只有 1 个选中）",
     doc.querySelectorAll("#diffHead .dstat.on").length, 1);
  eq("切到「仅云端」→ 2 行", doc.querySelectorAll("#cmpOut .drow").length, 2);
  check("内容确实是云端多出的",
        doc.getElementById("cmpOut").textContent.indexOf("cloud-only.txt") >= 0);
  check("切过去后「仅本地」的选中态已摘掉",
        !doc.querySelector('#diffHead [data-filter="local"]').classList.contains("on"));

  await tap("mismatch");
  eq("切到「大小不一致」→ 1 行", doc.querySelectorAll("#cmpOut .drow").length, 1);
  check("不一致的那条在列表里",
        doc.getElementById("cmpOut").textContent.indexOf("ShowB/b.mkv") >= 0);

  env.window.close();
}

async function testDiffFilterEmptyGroup() {
  console.log("\n[差异结果 · 筛到空分类要有明确交代]");
  const backend = makeBackend();
  // 造一个「仅云端」为空的场景
  backend.state.compareResult.cloudExtra = [];
  backend.state.compareResult.cloudExtraCount = 0;
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  await compareOnce(env, doc);

  const tap = filterTap(env, doc);
  check("数字为 0 的标签照样可点（禁用会让人以为坏了）",
        doc.querySelector('#diffHead [data-filter="cloud"]').disabled !== true);

  await tap("cloud");
  eq("筛到空分类只剩 1 个分组头", doc.querySelectorAll("#cmpOut .dgrp-head").length, 1);
  eq("空分类渲染 0 行", doc.querySelectorAll("#cmpOut .drow").length, 0);
  check("明确写出这一类是空的",
        doc.getElementById("cmpOut").textContent.indexOf("这一类当前没有条目") >= 0,
        doc.getElementById("cmpOut").textContent);

  await tap("cloud");
  eq("取消筛选后回到完整列表（本地 1 + 不一致 1）",
     doc.querySelectorAll("#cmpOut .drow").length, 2);
  env.window.close();
}

async function testDiffSameGroupReadonly() {
  console.log("\n[差异结果 ·「已同步」是只读视图]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  await compareOnce(env, doc);

  const tap = filterTap(env, doc);
  await tap("same");

  const out = doc.getElementById("cmpOut");
  eq("筛「已同步」→ 2 行", out.querySelectorAll(".drow").length, 2);
  eq("已同步行没有勾选框", out.querySelectorAll(".drow .dchk").length, 0);
  eq("已同步行没有操作按钮", out.querySelectorAll(".drow .dops button").length, 0);
  eq("已同步行标了 readonly", out.querySelectorAll(".drow.readonly").length, 2);
  check("已同步行带只读标记", out.textContent.indexOf("已同步") >= 0);
  check("已同步的文件名看得见", out.textContent.indexOf("done1.mkv") >= 0);
  check("分组头交代了「另有目录两边都存在」",
        out.textContent.indexOf("个目录两边都存在") >= 0, out.textContent.slice(0, 160));
  check("只读组不给「全选本组」按钮", !out.querySelector("[data-grp]"));
  check("筛到只读组时「全选」被禁用", doc.getElementById("diffAll").disabled === true);
  check("徽标数字用全量计数 sameCount（不是列出来的条数）",
        doc.querySelector('#diffHead [data-filter="same"]').textContent.indexOf("2") >= 0,
        doc.querySelector('#diffHead [data-filter="same"]').textContent);

  await tap("same");
  check("取消筛选后「全选」恢复可用", doc.getElementById("diffAll").disabled === false);
  env.window.close();
}

async function testDiffSameTruncated() {
  console.log("\n[差异结果 · 已同步明细被截断时数字不能缩水]");
  const backend = makeBackend();
  backend.state.compareResult.same = [
    { path: "done1.mkv", name: "done1.mkv", size: 1024, isDir: false }];
  backend.state.compareResult.sameCount = 2;      // 全量 2 条
  backend.state.compareResult.sameListed = 1;     // 明细只回了 1 条（触到回传上限）
  backend.state.compareResult.uploadedTruncated = true;
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  await compareOnce(env, doc);

  const chip = doc.querySelector('#diffHead [data-filter="same"]');
  eq("徽标用全量计数（2），不是列出来的 1 条",
     chip.textContent.replace(/\s/g, ""), "2已同步");

  const tap = filterTap(env, doc);
  await tap("same");
  const out = doc.getElementById("cmpOut").textContent;
  check("分组头如实说明只列出了一部分",
        out.indexOf("仅列出前 1 条") >= 0, out.slice(0, 200));
  eq("明细确实只有 1 行", doc.querySelectorAll("#cmpOut .drow").length, 1);
  env.window.close();
}

async function testDiffFilterKeepsSelection() {
  console.log("\n[差异结果 · 筛选不干扰勾选与单条操作]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  await compareOnce(env, doc);

  const tap = filterTap(env, doc);

  // 在完整列表里勾上「仅本地」那一行。
  // 必须用 .click() 而不是 dispatchEvent —— 前者走 checkbox 的默认行为
  // （真的把 checked 翻成 true），后者在 jsdom 里不改 checked，
  // 页面的处理器读到的就是"刚被取消勾选"，于是这一行永远加不进选择集。
  const chk = doc.querySelector("#cmpOut .dchk");
  chk.click();
  await flush();
  check("勾选后「上传选中」可用", doc.getElementById("diffUpload").disabled === false);

  // 切到别的分类：勾选按 key 存，不该因为看不见就被清掉 ——
  // 这正是「筛选只影响显示、不影响其它操作」的要求。
  await tap("cloud");
  check("被筛走的行仍然保持着勾选（按钮计数还在）",
        doc.getElementById("diffUpload").disabled === false,
        doc.getElementById("diffUpload").textContent);
  await tap("local");
  eq("筛回来时勾选原样还在", doc.querySelectorAll("#cmpOut .dchk:checked").length, 1);

  // 筛选态下「全选」只作用于**看得见**的组
  await tap("cloud");
  doc.getElementById("diffAll").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  eq("筛到「仅云端」时全选只勾上这 2 行",
     doc.querySelectorAll("#cmpOut .dchk:checked").length, 2);
  await tap("cloud");
  eq("取消筛选后能看到跨组累计的勾选", doc.querySelectorAll("#cmpOut .dchk:checked").length, 3);

  // 单条操作在筛选态下照常可用
  env.backend.state.downloadPayload = null;
  const row = Array.from(doc.querySelectorAll("#cmpOut .drow"))
    .find((r) => r.textContent.indexOf("cloud-only.txt") >= 0);
  const dl = Array.from(row.querySelectorAll("button")).find((b) => b.textContent === "下载");
  dl.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  const payload = env.backend.state.downloadPayload;
  eq("筛选态下单条「下载」照常发出", payload && payload.items.length, 1);
  check("下的就是这一行",
        payload && payload.items[0].remotePath === "/cloud-only.txt",
        JSON.stringify(payload));

  // 清空勾选不受筛选影响
  await tap("local");
  doc.getElementById("diffNone").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  eq("清空勾选清的是全部，不只是当前可见的",
     doc.querySelectorAll("#cmpOut .dchk:checked").length, 0);
  check("清空后「上传选中」重新禁用", doc.getElementById("diffUpload").disabled === true);

  env.window.close();
}

async function testCompareFromPanes() {
  console.log("\n[在传输页勾选两个文件夹后比对]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  // 本地勾 ShowA（目录），云端勾 ShowA（目录）
  const localDir = Array.from(doc.querySelectorAll("#localList .flist-row"))
    .find((el) => el.textContent.indexOf("ShowA") >= 0);
  const cloudDir = Array.from(doc.querySelectorAll("#cloudList .flist-row"))
    .find((el) => el.textContent.indexOf("ShowA") >= 0);
  check("找得到本地目录行", !!localDir);
  check("找得到云端目录行", !!cloudDir);
  localDir.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  cloudDir.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  await sleep(20);

  eq("比对前本地路径跟随浏览位置（此处是聚合根）", doc.getElementById("cmpLocal").value, "");
  doc.getElementById("btnComparePick").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();

  eq("本地路径换成了勾选的文件夹", doc.getElementById("cmpLocal").value, "/vol1/1000/ShowA");
  eq("云端路径换成了勾选的文件夹", doc.getElementById("cmpCloud").value, "/ShowA");
  const call = env.backend.state.comparePayload;
  // v2.9.2：两侧各自带 side 提交 —— 光给路径已经无法表达"这一侧是本地还是云端"。
  eq("请求里用的是勾选的本地目录",
     call && call.left && call.left.path, "/vol1/1000/ShowA");
  eq("请求里用的是勾选的云端目录",
     call && call.right && call.right.path, "/ShowA");
  eq("左侧来源是本地", call && call.left && call.left.side, "local");
  eq("右侧来源是云端", call && call.right && call.right.side, "cloud");
  env.window.close();
}

async function testCompareTabMigration() {
  console.log("\n[老的 compare 页签兜底]");
  // 老用户 localStorage 里存的还是 "compare"，不能切到不存在的页面（全屏空白）
  const env = makeWindow({ storage: { dd_ui_state: JSON.stringify({ tab: "compare" }) } });
  await flush();
  const doc = env.document;
  check("落到文件传输页", !doc.getElementById("page-transfer").classList.contains("hidden"));
  check("没有空白页",
        doc.querySelectorAll(".page:not(.hidden)").length === 1,
        doc.querySelectorAll(".page:not(.hidden)").length);
  check("启动过程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

/* ================= v2.9.2 比对两侧的来源都可选 ================= */

function cmpSideBox(doc, which) {
  return doc.getElementById(which === "left" ? "cmpSrcLeft" : "cmpSrcRight");
}
function cmpSideBtn(doc, which, side) {
  return cmpSideBox(doc, which).querySelector('[data-cmp-side="' + side + '"]');
}
async function tapCmpSide(env, doc, which, side) {
  cmpSideBtn(doc, which, side).dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
}
async function tapCompare(env, doc) {
  doc.getElementById("btnCompare").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
}
function tapRowAct(env, doc, act) {
  const b = doc.querySelector('#cmpOut .drow [data-act="' + act + '"]');
  b.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  return flush();
}
/** 通用差异桩：条目的 absPath 与两侧的根对得上，便于断言"用的是服务端给的路径"。 */
function pairResult(leftRoot, rightRoot) {
  return {
    notUploaded: [], sizeMismatch: [], uploaded: [], cloudOnly: [],
    localExtra: [{ path: "a.mkv", name: "a.mkv", size: 2048, isDir: false,
                   absPath: leftRoot + "/a.mkv", owner: "left" }],
    cloudExtra: [{ path: "b.mkv", name: "b.mkv", size: 5, isDir: false,
                   absPath: rightRoot + "/b.mkv", owner: "right" }],
    mismatch: [{ path: "c.mkv", name: "c.mkv", size: 4096, cloudSize: 1000, isDir: false,
                absPath: leftRoot + "/c.mkv", peerAbsPath: rightRoot + "/c.mkv",
                owner: "left" }],
    same: [],
    localExtraCount: 1, cloudExtraCount: 1, mismatchCount: 1,
    sameCount: 0, sameListed: 0,
    localExtraBytes: 2048, cloudExtraBytes: 5, mismatchBytes: 4096,
  };
}

async function testCompareSideDefaultsAndSwitch() {
  console.log("\n[v2.9.2 · 两侧来源切换]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  check("默认左侧是本地", cmpSideBtn(doc, "left", "local").classList.contains("on"));
  check("默认右侧是云端", cmpSideBtn(doc, "right", "cloud").classList.contains("on"));
  eq("左侧输入框初始是本地路径", doc.getElementById("cmpLocal").value, "");

  await tapCmpSide(env, doc, "left", "cloud");
  check("切到云端后该按钮点亮", cmpSideBtn(doc, "left", "cloud").classList.contains("on"));
  check("原来那枚熄灭", !cmpSideBtn(doc, "left", "local").classList.contains("on"));
  eq("切到同源时不再预填出同款路径", doc.getElementById("cmpLocal").value, "");
  check("左侧输入框提示改成云端",
        doc.getElementById("cmpLocal").placeholder.indexOf("云端") >= 0,
        doc.getElementById("cmpLocal").placeholder);
  eq("来源开关带 aria-pressed",
     cmpSideBtn(doc, "left", "cloud").getAttribute("aria-pressed"), "true");

  await tapCmpSide(env, doc, "left", "local");
  check("切回本地后恢复", cmpSideBtn(doc, "left", "local").classList.contains("on"));
  eq("路径跟着切回本地面板", doc.getElementById("cmpLocal").value, "");
  check("来源切换全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

/* ============ v2.9.3：两侧同源时不许互相改写 ============
   两侧选了同一种来源时，下面只有一个同类型的栏，它只能带动一侧的路径。旧写法让
   两个框都去读那个栏，于是「浏览右侧」会把**左侧也改成同一个路径**，点比对必然报
   「两侧是同一个位置」—— 而云端只有一个浏览栏，用户没有任何办法把它们分开。 */

async function testSameSourceCloudKeepsPeer() {
  console.log("\n[v2.9.3 · 云端↔云端：浏览右侧不再改写左侧]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  await tapCmpSide(env, doc, "left", "cloud");
  eq("切到同源时不再预填出同款路径", doc.getElementById("cmpLocal").value, "");
  check("同源时给出引导", doc.getElementById("cmpHint").classList.contains("warn"));
  check("引导里说要手动填另一侧",
        doc.getElementById("cmpHint").textContent.indexOf("手动填写") >= 0,
        doc.getElementById("cmpHint").textContent);

  doc.getElementById("cmpLocal").value = "/cloud-a";
  await env.window.loadCloud("/cloud-b");
  await flush();
  eq("浏览云端栏 → 右侧跟上", doc.getElementById("cmpCloud").value, "/cloud-b");
  eq("左侧不被覆盖（本次修复的核心）", doc.getElementById("cmpLocal").value, "/cloud-a");

  env.backend.state.compareResult = pairResult("/cloud-a", "/cloud-b");
  await tapCompare(env, doc);
  const call = env.backend.state.comparePayload;
  eq("提交的是两个不同的云端目录",
     (call && call.left.path) + "|" + (call && call.right.path), "/cloud-a|/cloud-b");
  check("两侧来源都是云端",
        !!(call && call.left.side === "cloud" && call.right.side === "cloud"));
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testSameSourceLocalSymmetric() {
  console.log("\n[v2.9.3 · 本地↔本地：对称，浏览左侧不改写右侧]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  await tapCmpSide(env, doc, "right", "local");
  eq("切到同源时右侧不预填同款路径", doc.getElementById("cmpCloud").value, "");
  doc.getElementById("cmpCloud").value = "/vol1/1000/b";
  await env.window.loadLocal("/vol1/1000/a");
  await flush();
  eq("浏览本地栏 → 左侧跟上", doc.getElementById("cmpLocal").value, "/vol1/1000/a");
  eq("右侧不被覆盖", doc.getElementById("cmpCloud").value, "/vol1/1000/b");
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testCrossSourcePanesDoNotCollide() {
  console.log("\n[v2.9.3 · 交叉来源：一栏一框，各跟各的]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  await tapCmpSide(env, doc, "left", "cloud");
  await tapCmpSide(env, doc, "right", "local");
  check("同源引导撤掉", !doc.getElementById("cmpHint").classList.contains("warn"));
  await env.window.loadCloud("/cloud-x");
  await flush();
  eq("云端栏 → 左侧（左侧来源是云端）", doc.getElementById("cmpLocal").value, "/cloud-x");
  await env.window.loadLocal("/vol1/1000/y");
  await flush();
  eq("本地栏 → 右侧（右侧来源是本地）", doc.getElementById("cmpCloud").value, "/vol1/1000/y");
  eq("左框没被本地栏改掉", doc.getElementById("cmpLocal").value, "/cloud-x");
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testDefaultSidesStillFollowPanes() {
  console.log("\n[v2.9.3 · 默认左右异源：仍然两侧各自跟随]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  await env.window.loadCloud("/cloud-z");
  await flush();
  await env.window.loadLocal("/vol1/1000/z");
  await flush();
  eq("右侧跟云端栏", doc.getElementById("cmpCloud").value, "/cloud-z");
  eq("左侧跟本地栏", doc.getElementById("cmpLocal").value, "/vol1/1000/z");
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testSameSourcePickFillsOneSide() {
  console.log("\n[v2.9.3 · 「比对 ⇄」在两侧同源时只填一侧]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  await tapCmpSide(env, doc, "left", "cloud");
  doc.getElementById("btnComparePick").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  eq("云端栏填给了右侧", doc.getElementById("cmpCloud").value, "/");
  eq("左侧没被填成同一个位置", doc.getElementById("cmpLocal").value, "");
  check("同位置照样给出可操作的报错",
        doc.getElementById("toast").textContent.indexOf("改到别的目录") >= 0,
        doc.getElementById("toast").textContent);
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testSamePositionMarksBothInputs() {
  console.log("\n[v2.9.3 · 两侧同位置时两个框都标红]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  await tapCmpSide(env, doc, "left", "cloud");
  doc.getElementById("cmpLocal").value = "/same";
  doc.getElementById("cmpCloud").value = "/same";
  await tapCompare(env, doc);
  check("左侧标红", doc.getElementById("cmpLocal").classList.contains("bad"));
  check("右侧标红", doc.getElementById("cmpCloud").classList.contains("bad"));
  check("提示让用户改到别的目录",
        doc.getElementById("toast").textContent.indexOf("改到别的目录") >= 0,
        doc.getElementById("toast").textContent);
  check("没有发出比对请求", env.backend.state.comparePayload === null,
        JSON.stringify(env.backend.state.comparePayload));
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

/* 旧版本会把两个框同步成同一个云端路径并存进记忆 —— 升级后这份存档还在。
   恢复时必须把它纠正掉，否则用户一打开就卡在"点上比对就报错"的状态里。 */
async function testRestoreDedupesStoredSameSource() {
  console.log("\n[v2.9.3 · 旧存档里的同值被纠正]");
  const env = makeWindow({ storage: { dd_ui_state: JSON.stringify({
    tab: "transfer",
    cmpLeftSide: "cloud", cmpRightSide: "cloud",
    cmpLocal: "/dup", cmpCloud: "/dup",
  }) } });
  await flush();
  const doc = env.document;
  eq("有栏的一侧保留", doc.getElementById("cmpCloud").value, "/dup");
  eq("无栏的一侧留空，不留在比不出差异的状态", doc.getElementById("cmpLocal").value, "");
  check("恢复后给出引导", doc.getElementById("cmpHint").classList.contains("warn"));
  check("恢复过程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

/* ============ v2.9.4：左栏自己也能是 OpenList 源 ============
   设置页的「文件传输左侧来源」可以把左栏换成 OpenList 路径。那时界面上会同时存在
   **两个云端栏**（左栏 + 右栏），而比对框↔栏的映射是按**栏位名字**分配的
   （左框↔本地栏），隐含假设「一种来源类型只有一个栏」。两个框于是去抢同一个栏，
   只有一个跟得上：在左栏里进文件夹，上方比对条左侧的地址不跟着动。 */

/** 把左栏配成 OpenList 源的后端桩（配置项可用 extra 覆盖，保存时真写回去）。 */
function cloudLeftBackend(extra) {
  const cfg = Object.assign({
    localRoot: "", cloudRoot: "/", uploadConcurrency: 2, maxAttempts: 3,
    skipExisting: true, verifyAfterUpload: true, scanDepth: 3,
    excludeNames: ["*.tmp"],
    localSourceMode: "cloud", localSourceRoot: "/本地存储",
  }, extra || {});
  const backend = makeBackend({
    "api/config": (query, method, init) => {
      if (method === "POST") Object.assign(cfg, JSON.parse(init.body));
      return { ok: true, config: cfg };
    },
  });
  backend.state.cfg = cfg;
  return backend;
}
async function tapSaveCfg(env, doc) {
  doc.getElementById("btnSaveCfg").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
}

async function testCloudLeftPaneFollowsItsOwnPane() {
  console.log("\n[v2.9.4 · 左栏是 OpenList 源：两个框各跟各的栏]");
  const env = makeWindow({ backend: cloudLeftBackend() });
  await flush();
  const doc = env.document;
  const L = () => doc.getElementById("cmpLocal").value;
  const R = () => doc.getElementById("cmpCloud").value;

  check("左侧来源默认跟着左栏走：云端",
        cmpSideBtn(doc, "left", "cloud").classList.contains("on"));
  check("左侧不再点亮「本地」", !cmpSideBtn(doc, "left", "local").classList.contains("on"));
  eq("左框填的是左栏（OpenList）的起始路径", L(), "/本地存储");
  eq("右框填的是云端起始路径", R(), "/");
  check("两侧同源但各有各的栏 → 不打「只有一个栏」的警告",
        !doc.getElementById("cmpHint").classList.contains("warn"),
        doc.getElementById("cmpHint").textContent);
  check("说明里的默认值按栏的实际来源写",
        doc.getElementById("cmpHint").textContent.indexOf("默认左云端") >= 0,
        doc.getElementById("cmpHint").textContent);

  await env.window.loadLocal("/本地存储/电影");
  await flush();
  eq("在左栏进文件夹 → 左框跟上（本次修复的核心）", L(), "/本地存储/电影");
  eq("右框不动", R(), "/");

  await env.window.loadCloud("/备份/相册");
  await flush();
  eq("进右栏 → 右框跟上", R(), "/备份/相册");
  eq("左框不动", L(), "/本地存储/电影");

  env.backend.state.compareResult = pairResult("/本地存储/电影", "/备份/相册");
  await tapCompare(env, doc);
  const call = env.backend.state.comparePayload;
  check("两侧都按云端提交",
        !!(call && call.left.side === "cloud" && call.right.side === "cloud"),
        JSON.stringify(call));
  eq("左侧提交的是左栏那个目录", call && call.left.path, "/本地存储/电影");
  eq("右侧提交的是右栏那个目录", call && call.right.path, "/备份/相册");
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testCloudLeftPaneNoLocalPaneHint() {
  console.log("\n[v2.9.4 · 左栏是 OpenList 源时选「本地」：要说清没有本地栏]");
  const env = makeWindow({ backend: cloudLeftBackend() });
  await flush();
  const doc = env.document;
  await tapCmpSide(env, doc, "left", "local");

  const hint = doc.getElementById("cmpHint");
  check("给出引导", hint.classList.contains("warn"));
  check("说清是「界面上没有本地栏」，而不是「只有一个云端栏」",
        hint.textContent.indexOf("没有本地栏") >= 0, hint.textContent);
  check("指出原因是左栏换成了 OpenList 源",
        hint.textContent.indexOf("OpenList") >= 0, hint.textContent);
  check("给出可操作的补救（切成云端）", hint.textContent.indexOf("云端") >= 0, hint.textContent);

  doc.getElementById("cmpLocal").value = "/vol1/1000/x";
  await env.window.loadLocal("/本地存储/别的");
  await flush();
  eq("左框声明的是「本地」，不许被云端栏改写",
     doc.getElementById("cmpLocal").value, "/vol1/1000/x");
  eq("右框照常跟右栏", doc.getElementById("cmpCloud").value, "/");
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testLocalSourceModeSwitchRefollows() {
  console.log("\n[v2.9.4 · 设置里把左栏换成 OpenList：比对条左侧跟着切]");
  const env = makeWindow({ backend: cloudLeftBackend({ localSourceMode: "fs" }) });
  await flush();
  const doc = env.document;
  check("初始左侧是本地", cmpSideBtn(doc, "left", "local").classList.contains("on"));

  // 先手填一次路径：这会让比对条退出「跟随浏览位置」的状态。
  // 换来源模式必须把这个状态恢复回来 —— 否则左框会一直冻在一串已经失效的本地路径上。
  const box = doc.getElementById("cmpLocal");
  box.value = "/vol1/1000/old";
  box.dispatchEvent(new env.window.Event("input", { bubbles: true }));
  await flush();
  await env.window.loadLocal("/vol1/1000/other");
  await flush();
  eq("手填过就不再被浏览覆盖（前提成立）", doc.getElementById("cmpLocal").value,
     "/vol1/1000/old");

  doc.getElementById("cfgLocalMode").value = "cloud";
  doc.getElementById("cfgLocalCloudRoot").value = "/本地存储";
  await tapSaveCfg(env, doc);

  check("保存后左侧来源跟着栏变成云端",
        cmpSideBtn(doc, "left", "cloud").classList.contains("on"));
  eq("左框不再留着那串本地路径，换成左栏的新起始路径",
     doc.getElementById("cmpLocal").value, "/本地存储");

  await env.window.loadLocal("/本地存储/剧集");
  await flush();
  eq("换完来源后恢复跟随：在左栏进文件夹，左框照样跟",
     doc.getElementById("cmpLocal").value, "/本地存储/剧集");
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testTouchedSideNotOverriddenByModeSwitch() {
  console.log("\n[v2.9.4 · 用户选过的侧别不被「跟栏」改掉]");
  const env = makeWindow({ backend: cloudLeftBackend({ localSourceMode: "fs" }) });
  await flush();
  const doc = env.document;
  await tapCmpSide(env, doc, "right", "local");
  check("右侧被用户切成本地", cmpSideBtn(doc, "right", "local").classList.contains("on"));

  doc.getElementById("cfgLocalMode").value = "cloud";
  await tapSaveCfg(env, doc);

  check("用户选过的右侧来源保留",
        cmpSideBtn(doc, "right", "local").classList.contains("on"));
  check("没选过的左侧仍然跟着栏变成云端",
        cmpSideBtn(doc, "left", "cloud").classList.contains("on"));
  check("全程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

async function testCompareCloudToCloud() {
  console.log("\n[v2.9.2 · 云端 ↔ 云端]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  await tapCmpSide(env, doc, "left", "cloud");
  env.backend.state.compareResult = pairResult("/cloud-a", "/cloud-b");
  doc.getElementById("cmpLocal").value = "/cloud-a";
  doc.getElementById("cmpCloud").value = "/cloud-b";
  await tapCompare(env, doc);

  const call = env.backend.state.comparePayload;
  eq("左侧提交为云端", call && call.left.side, "cloud");
  eq("左侧提交了云端路径", call && call.left.path, "/cloud-a");
  eq("右侧提交为云端", call && call.right.side, "cloud");
  eq("右侧提交了云端路径", call && call.right.path, "/cloud-b");

  const head = doc.getElementById("diffHead").textContent;
  check("徽标不再说「仅本地」", head.indexOf("仅本地") < 0, head);
  eq("两枚徽标都叫「仅云端」", (head.match(/仅云端/g) || []).length, 2);
  check("用左右区分两侧",
        head.indexOf("左侧云端") >= 0 && head.indexOf("右侧云端") >= 0, head);

  const titles = Array.prototype.map.call(
    doc.querySelectorAll("#cmpOut .dgrp-head"), g => g.textContent);
  check("分组标题写「仅左侧云端有」",
        titles.some(t => t.indexOf("仅左侧云端有") >= 0), titles.join(" | "));
  check("分组标题写「仅右侧云端有」",
        titles.some(t => t.indexOf("仅右侧云端有") >= 0), titles.join(" | "));
  check("分组标题里不再出现「本地」",
        titles.every(t => t.indexOf("本地") < 0), titles.join(" | "));

  const toRight = doc.querySelector('#cmpOut .drow [data-act="toRight"]');
  check("行内按钮是「复制到右侧」而不是「上传」",
        toRight && toRight.textContent.indexOf("复制到右侧") >= 0,
        toRight && toRight.textContent);
  const toLeft = doc.querySelector('#cmpOut .drow [data-act="toLeft"]');
  check("右侧独有行的按钮是「复制到左侧」",
        toLeft && toLeft.textContent.indexOf("复制到左侧") >= 0, toLeft && toLeft.textContent);
  const clearLeft = doc.querySelector('#cmpOut .drow [data-act="clearLeft"]');
  check("清除按钮带侧别",
        clearLeft && clearLeft.textContent.indexOf("清除左侧云端") >= 0,
        clearLeft && clearLeft.textContent);
  env.window.close();
}

async function testCompareCloudToCloudCopy() {
  console.log("\n[v2.9.2 · 云端 → 云端 走服务端复制]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  await tapCmpSide(env, doc, "left", "cloud");
  env.backend.state.compareResult = pairResult("/cloud-a", "/cloud-b");
  doc.getElementById("cmpLocal").value = "/cloud-a";
  doc.getElementById("cmpCloud").value = "/cloud-b";
  await tapCompare(env, doc);

  await tapRowAct(env, doc, "toRight");
  const p = env.backend.state.syncCopyPayload;
  check("走了 /api/sync/copy", !!p, JSON.stringify(p));
  eq("side 是云端", p && p.side, "cloud");
  eq("目标目录是右侧云端目录", p && p.target, "/cloud-b");
  eq("源路径用的是服务端给的 absPath", p && p.items[0], "/cloud-a/a.mkv");
  check("没有误发上传请求", env.backend.state.enqueuePayload === null,
        JSON.stringify(env.backend.state.enqueuePayload));
  check("没有误发下载请求", env.backend.state.downloadPayload === null,
        JSON.stringify(env.backend.state.downloadPayload));
  eq("处理完那一行从差异里摘掉", doc.querySelectorAll("#cmpOut .drow").length, 2);
  env.window.close();
}

async function testCompareLocalToLocalCopy() {
  console.log("\n[v2.9.2 · 本地 → 本地 走本地复制]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  await tapCmpSide(env, doc, "right", "local");
  env.backend.state.compareResult = pairResult("/vol1/1000/A", "/vol1/1000/B");
  doc.getElementById("cmpLocal").value = "/vol1/1000/A";
  doc.getElementById("cmpCloud").value = "/vol1/1000/B";
  await tapCompare(env, doc);

  const head = doc.getElementById("diffHead").textContent;
  check("两侧同为本地时用左右区分",
        head.indexOf("左侧本地") >= 0 && head.indexOf("右侧本地") >= 0, head);

  await tapRowAct(env, doc, "toRight");
  const p = env.backend.state.syncCopyPayload;
  eq("side 是本地", p && p.side, "local");
  eq("目标目录是右侧本地目录", p && p.target, "/vol1/1000/B");
  eq("源路径是左侧本地路径", p && p.items[0], "/vol1/1000/A/a.mkv");
  env.window.close();
}

async function testCompareEmptySideAndError() {
  console.log("\n[v2.9.2 · 空路径与失败侧提示]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  // 本地侧留空：不比对，且把那一侧标出来
  doc.getElementById("cmpLocal").value = "";
  await tapCompare(env, doc);
  check("本地侧留空不发请求", env.backend.state.comparePayload === null);
  check("本地侧输入框标红", doc.getElementById("cmpLocal").classList.contains("bad"));
  check("提示指明是左侧", doc.getElementById("toast").textContent.indexOf("左侧") >= 0,
        doc.getElementById("toast").textContent);

  // 云端侧留空 = 云端根目录，不该拦
  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpCloud").value = "";
  await tapCompare(env, doc);
  const call = env.backend.state.comparePayload;
  eq("云端侧留空按根目录提交", call && call.right.path, "/");
  check("本地侧不再标红", !doc.getElementById("cmpLocal").classList.contains("bad"));
  check("云端侧也没有标红", !doc.getElementById("cmpCloud").classList.contains("bad"));
  env.window.close();
}

async function testCompareServerSideError() {
  console.log("\n[v2.9.2 · 服务端报错时标出问题那一侧]");
  const env = makeWindow({ backend: makeBackend({
    "api/compare": () => ({ ok: false, error: "右侧：云端目录不存在（/nope）" }),
  }) });
  await flush();
  const doc = env.document;
  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpCloud").value = "/nope";
  await tapCompare(env, doc);
  check("右侧输入框被标红", doc.getElementById("cmpCloud").classList.contains("bad"));
  check("左侧不被牵连", !doc.getElementById("cmpLocal").classList.contains("bad"));
  check("错误原文展示出来",
        doc.getElementById("cmpOut").textContent.indexOf("云端目录不存在") >= 0,
        doc.getElementById("cmpOut").textContent);
  env.window.close();
}

async function testCompareResultKeepsItsOwnSides() {
  console.log("\n[v2.9.2 · 已出的结果按它自己的来源渲染]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  // 先按默认组合（左本地 / 右云端）比一次
  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpCloud").value = "/";
  await tapCompare(env, doc);
  const before = doc.getElementById("diffHead").textContent;
  check("默认组合下说的是「仅本地」", before.indexOf("仅本地") >= 0, before);
  check("默认组合下不出现左右前缀", before.indexOf("左侧") < 0, before);

  // 比对完成后把输入框那一侧的来源切到云端：
  // **已经出来的那份结果不该跟着变** —— 它的侧别是服务端回传的，
  // 跟输入框上后来选了什么无关。文案若跟着输入框走，用户眼前就是假信息。
  await tapCmpSide(env, doc, "left", "cloud");
  eq("结果文案保持原样", doc.getElementById("diffHead").textContent, before);

  // 更要紧的是动作方向也要跟着**结果**走：这份结果的左侧是本地，
  // 所以「送到右侧」就该是上传，而不是云端复制。
  env.backend.state.enqueuePayload = null;
  env.backend.state.syncCopyPayload = null;
  await tapRowAct(env, doc, "toRight");
  check("动作按结果的侧别走（上传），而不是按输入框现在选的云端",
        env.backend.state.enqueuePayload !== null,
        "enqueue=" + JSON.stringify(env.backend.state.enqueuePayload)
        + " copy=" + JSON.stringify(env.backend.state.syncCopyPayload));
  check("没有误走跨侧复制接口", env.backend.state.syncCopyPayload === null);
  env.window.close();
}

async function testComparePrerequisite() {
  console.log("\n[比对前置校验]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  doc.getElementById("cmpLocal").value = "";
  const before = env.backend.state.calls.filter((c) => c.url.indexOf("/api/compare") >= 0).length;
  doc.getElementById("btnCompare").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await sleep(30);
  const after = env.backend.state.calls.filter((c) => c.url.indexOf("/api/compare") >= 0).length;
  eq("本地目录为空时不发比对请求", after, before);
  check("给出了提示", doc.getElementById("toast").textContent.indexOf("本地目录") >= 0,
        doc.getElementById("toast").textContent);
  env.window.close();
}

async function testNoNativeDialogs() {
  console.log("\n[新建文件夹用自绘弹层]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  doc.getElementById("cloudMkdir").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await sleep(20);
  check("出现了自绘弹层", !doc.getElementById("overlay").classList.contains("hidden"));
  eq("没有调用原生 prompt", env.nativeCalls.length, 0);

  const inp = doc.querySelector("#dlgBody input");
  check("弹层里有输入框", !!inp);
  inp.value = "新目录";
  doc.getElementById("dlgOk").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  check("发出了 mkdir 请求",
        env.backend.state.mkdirPayload && env.backend.state.mkdirPayload.path === "/新目录",
        JSON.stringify(env.backend.state.mkdirPayload));
  eq("全程没有调用原生弹窗", env.nativeCalls.length, 0);
  env.window.close();
}

async function testStorageDisabled() {
  console.log("\n[本地存储被沙箱禁用]");
  const env = makeWindow({ breakStorage: true });
  await flush();
  const doc = env.document;
  check("页面没有白屏（列表照常渲染）",
        doc.querySelectorAll("#localList .flist-row").length === 2);
  check("启动过程没有抛异常", env.pageErrors.length === 0,
        env.pageErrors.join(" | "));
  env.window.close();
}

async function testXssEscaping() {
  console.log("\n[转义]");
  const evil = '<img src=x onerror=window.__pwned=1>';
  const backend = makeBackend({
    "api/local/list": () => ({ ok: true, path: "/vol1/1000", aggregate: false,
      entries: [{ name: evil, path: "/vol1/1000/" + evil, isDir: false, size: 1, mtime: 1 }] }),
    "api/cloud/list": () => ({ ok: true, path: "/", entries: [
      { name: evil, path: "/" + evil, isDir: false, size: 1, modified: "2026-09-01" }] }),
  });
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  check("本地列表里的文件名被转义（没有生成 img 元素）",
        doc.querySelectorAll("#localList img").length === 0);
  check("云端列表同理", doc.querySelectorAll("#cloudList img").length === 0);
  check("注入的脚本没有执行", env.window.__pwned === undefined);
  check("文件名原文仍然可见",
        doc.getElementById("localList").textContent.indexOf("onerror") >= 0);
  env.window.close();
}

// ---------------------------------------------------------------------------
// v2.1.0 新增用例（独立构造，不复用工程师的自测路径）
// ---------------------------------------------------------------------------

/** 取计算样式（jsdom 会解析 <style> 级联，比只查规则文本更硬）。 */
function cs(win, el, prop) {
  if (!el) return "<无元素>";
  return win.getComputedStyle(el)[prop];
}

/** 布局不变量：深层级路径下「中栏上传按钮」不得被挤出容器。
 *
 * jsdom 没有排版引擎，所以这里不量像素，而是量**收缩链条的计算样式**：
 * 从面包屑沿父链一路上到 #panes，链条上每个环节都必须 min-width:0，
 * 否则 1fr 轨道的自动最小尺寸会被内容撑大 → 网格右溢 → 按钮被裁掉。
 * 这正是「进入第三层目录按钮消失」的根因，逐环节焊死。 */
async function testLayoutShrinkChainV21() {
  console.log("\n[布局不变量 · 收缩链条与中栏按钮]");
  const env = makeWindow();
  await flush();
  const win = env.window, doc = env.document;

  // 1) 面包屑自身：单行、横向滚动、不被内容撑大
  const crumb = doc.getElementById("localCrumb");
  eq("面包屑 flex-wrap:nowrap", cs(win, crumb, "flexWrap"), "nowrap");
  eq("面包屑 white-space:nowrap", cs(win, crumb, "whiteSpace"), "nowrap");
  eq("面包屑 overflow-x:auto（深层级横向滚动）", cs(win, crumb, "overflowX"), "auto");
  eq("面包屑 min-width:0", cs(win, crumb, "minWidth"), "0px");

  // 2) 面包屑条目不被压变形（flex:none）。云端 crumb 从根开始，必有层级链接。
  const anchor = doc.querySelector("#cloudCrumb a");
  check("面包屑有层级链接（前置条件）", !!anchor);
  eq("面包屑链接不被压缩（flex-grow 0）", cs(win, anchor, "flexGrow"), "0");
  eq("面包屑链接不被压缩（flex-shrink 0）", cs(win, anchor, "flexShrink"), "0");

  // 3) 收缩链条完整：crumb → .pane-head → .pane → #panes，无一环缺失
  const chain = [];
  let el = crumb;
  while (el && el.id !== "panes") {
    chain.push(el);
    el = el.parentElement;
  }
  check("收缩链条一路接到 #panes（无中间断点）",
        el && el.id === "panes" && chain.length === 3,
        "链条长度 " + chain.length + "，末端 " + (el && el.id));
  chain.forEach((node, i) => {
    eq("链条第 " + (i + 1) + " 环 " + (node.className || "#" + node.id) + " 有 min-width:0",
       cs(win, node, "minWidth"), "0px");
  });
  check("网格列两侧轨道是 minmax(0,1fr)（防长路径挤出中栏）",
     cs(win, doc.getElementById("panes"), "gridTemplateColumns").indexOf("minmax") >= 0,
     cs(win, doc.getElementById("panes"), "gridTemplateColumns"));

  // 4) 中栏按钮：不参与伸缩 + 有最小宽度 + 文字不折行
  const mid = doc.getElementById("midbar");
  eq("#midbar 不参与伸缩（flex-grow 0）", cs(win, mid, "flexGrow"), "0");
  eq("#midbar 不参与伸缩（flex-shrink 0）", cs(win, mid, "flexShrink"), "0");
  eq("#midbar 有最小宽度 118px", cs(win, mid, "minWidth"), "118px");
  eq("#btnUpload 文字不折行", cs(win, doc.getElementById("btnUpload"), "whiteSpace"), "nowrap");
  env.window.close();

  // 5) 行为验证：换成极长/极深路径真实渲染，不炸、按钮仍在
  const deep = "/" + Array.from({ length: 14 },
    (_, i) => "很长的中文目录名称第" + i + "层级").join("/");
  const backend = makeBackend({
    "api/local/list": () => ({ ok: true, path: deep, aggregate: false,
      entries: [{ name: "深层文件.mkv", path: deep + "/深层文件.mkv",
                  isDir: false, size: 1, mtime: 1 }] }),
  });
  const env2 = makeWindow({ backend,
    storage: { dd_ui_state: JSON.stringify({ localPath: deep, cloudPath: "/" }) } });
  await flush();
  const doc2 = env2.document;
  check("深层级面包屑渲染出全部层级（>10 段）",
        doc2.getElementById("localCrumb").querySelectorAll("a").length > 10,
        doc2.getElementById("localCrumb").querySelectorAll("a").length + " 段");
  check("深层渲染后中栏仍在且上传按钮未隐藏",
        !!doc2.getElementById("midbar") &&
        !doc2.getElementById("btnUpload").classList.contains("hidden"));
  check("深层渲染过程没有抛异常", env2.pageErrors.length === 0, env2.pageErrors.join(" | "));
  env2.window.close();
}

/** 设置页布局：元素齐全、栅格约束、无内联定宽。 */
async function testSettingsLayoutV21() {
  console.log("\n[设置页布局不变量]");
  const needIds = ["cfgConc", "cfgRetry", "cfgSkip", "cfgVerify", "cfgExcl",
                   "cfgLocal", "cfgCloud", "cfgLocalMode", "cfgLocalCloudRoot",
                   "btnSaveCfg", "cfgMsg", "btnViewLog",
                   "btnDisconnect", "logBox"];
  const env = makeWindow();
  await flush();
  const win = env.window, doc = env.document;
  const missing = needIds.filter((id) => !doc.getElementById(id));
  check("所有设置项元素与 id 仍存在", missing.length === 0, "缺失：" + missing.join(","));

  const row = doc.querySelector("#page-settings .frow");
  eq("设置行 nowrap（窄窗不掉行）", cs(win, row, "flexWrap"), "nowrap");
  eq("设置行统一最小行高 38px", cs(win, row, "minHeight"), "38px");
  eq("控件可收缩 min-width:0", cs(win, doc.getElementById("cfgConc"), "minWidth"), "0px");
  eq("控件占满剩余宽度 flex-grow:1", cs(win, doc.getElementById("cfgConc"), "flexGrow"), "1");
  const chk = doc.querySelector("#page-settings .frow label.chk");
  eq("复选框整行可点 cursor:pointer", cs(win, chk, "cursor"), "pointer");
  eq("复选框行不固定宽度 width:auto", cs(win, chk, "width"), "auto");
  const btn = doc.querySelector("#page-settings .frow.btnrow .btn");
  eq("底部按钮等分 flex-grow:1", cs(win, btn, "flexGrow"), "1");
  eq("底部按钮等分 flex-shrink:1", cs(win, btn, "flexShrink"), "1");

  // 去掉内联定宽的历史遗留
  ["cfgConc", "cfgRetry"].forEach((id) => {
    const style = doc.getElementById(id).getAttribute("style") || "";
    check(id + " 无内联宽度（交给栅格）", style.indexOf("width") < 0, style);
  });
  check("HTML 中已无内联 width:80px",
        HTML.indexOf("width:80px") < 0 && HTML.indexOf("width: 80px") < 0);
  env.window.close();
}

/** 设置页保存往返：复选框参与、以服务端返回为准、保存后清草稿。 */
async function testSettingsSaveRoundTripV21() {
  console.log("\n[设置页保存往返与草稿清除]");
  const BASE_CFG = { localRoot: "", cloudRoot: "/", uploadConcurrency: 2, maxAttempts: 3,
    skipExisting: true, verifyAfterUpload: true, excludeNames: ["*.tmp"],
    localSourceMode: "fs", localSourceRoot: "" };
  const backend = makeBackend({
    "api/config": (q, method, init) => {
      if (method === "POST") {
        const sent = JSON.parse(init.body);
        // 服务端覆盖两个字段：验证「以服务端返回为准」
        const merged = Object.assign({}, BASE_CFG, sent,
          { uploadConcurrency: 6 });
        return { ok: true, config: merged };
      }
      return { ok: true, config: BASE_CFG };
    },
  });
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  doc.querySelector('[data-tab="settings"]').dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));

  const setVal = (id, v) => {
    const el = doc.getElementById(id);
    el.value = v;
    el.dispatchEvent(new env.window.Event("change", { bubbles: true }));
  };
  setVal("cfgConc", "5");
  setVal("cfgRetry", "7");
  setVal("cfgExcl", "*.tmp, *.log");
  setVal("cfgLocal", "/vol1/1000");
  setVal("cfgCloud", "/cl");
  doc.getElementById("cfgSkip").checked = false;
  doc.getElementById("cfgSkip").dispatchEvent(new env.window.Event("change", { bubbles: true }));
  doc.getElementById("cfgVerify").checked = true;
  doc.getElementById("cfgVerify").dispatchEvent(new env.window.Event("change", { bubbles: true }));
  await sleep(500);
  const draft = JSON.parse(env.window.localStorage.getItem("dd_ui_state") || "{}");
  eq("改值即生成未保存草稿", draft.cfgDraft && draft.cfgDraft.cfgConc, "5");

  doc.getElementById("btnSaveCfg").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  const sent = env.backend.state.calls.filter((c) => c.url.endsWith("/api/config") &&
    c.method === "POST").pop();
  check("发出了保存请求", !!sent);
  const body = sent && sent.body;
  eq("保存：并发数", body && body.uploadConcurrency, 5);
  eq("保存：重试次数", body && body.maxAttempts, 7);
  eq("保存：跳过已存在=false（复选框参与）", body && body.skipExisting, false);
  eq("保存：完成后校验=true（复选框参与）", body && body.verifyAfterUpload, true);
  eq("保存：排除规则解析为数组", body && JSON.stringify(body.excludeNames), JSON.stringify(["*.tmp", "*.log"]));
  eq("保存：本地目录", body && body.localRoot, "/vol1/1000");
  eq("保存：云端目录", body && body.cloudRoot, "/cl");

  eq("保存后回显以服务端为准（并发数=6）", doc.getElementById("cfgConc").value, "6");
  eq("保存提示已显示", doc.getElementById("cfgMsg").textContent, "已保存");
  const after = JSON.parse(env.window.localStorage.getItem("dd_ui_state") || "{}");
  check("保存成功后草稿被清除", after.cfgDraft === undefined, JSON.stringify(after.cfgDraft));

  // 第二趟：把「跳过已存在」改为勾选，保存体应随之变 true
  const before2 = env.backend.state.calls.filter((c) => c.url.endsWith("/api/config") &&
    c.method === "POST").length;
  doc.getElementById("cfgSkip").checked = true;
  doc.getElementById("cfgSkip").dispatchEvent(new env.window.Event("change", { bubbles: true }));
  doc.getElementById("btnSaveCfg").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  const sent2 = env.backend.state.calls.filter((c) => c.url.endsWith("/api/config") &&
    c.method === "POST").pop();
  check("第二趟确实发出了保存", env.backend.state.calls.filter((c) =>
    c.url.endsWith("/api/config") && c.method === "POST").length === before2 + 1);
  eq("勾选后保存 skipExisting=true", sent2 && sent2.body && sent2.body.skipExisting, true);
  env.window.close();
}

/** 记忆目录已不存在 → 回退默认根，不白屏。 */
async function testMemoryDirGoneFallback() {
  console.log("\n[记忆目录失效时降级不白屏]");
  const backend = makeBackend({
    "api/config": () => ({ ok: true, config: { localRoot: "/vol1/1000", cloudRoot: "/",
      uploadConcurrency: 2, maxAttempts: 3, skipExisting: true, verifyAfterUpload: true,
      scanDepth: 3, excludeNames: ["*.tmp"] } }),
    "api/local/list": (query) => {
      const raw = (/path=([^&]*)/.exec(query || "") || [])[1];
      const p = raw ? decodeURIComponent(raw) : "";
      if (p.indexOf("Gone") >= 0) return { ok: false, error: "目录不存在或不可读：" + p };
      if (!p) return { ok: true, aggregate: true, path: "", entries: [] };
      return { ok: true, path: p, aggregate: false, entries: [
        { name: "ok.mkv", path: p + "/ok.mkv", isDir: false, size: 1, mtime: 1 }] };
    },
    "api/cloud/list": (query) => {
      const raw = (/path=([^&]*)/.exec(query || "") || [])[1];
      const p = raw ? decodeURIComponent(raw) : "/";
      if (p.indexOf("goneC") >= 0) return { ok: false, error: "云端目录不存在：" + p };
      return { ok: true, path: p, write: true, entries: [
        { name: "c.mkv", path: p.replace(/\/$/, "") + "/c.mkv", isDir: false, size: 1, modified: "" }] };
    },
  });
  const env = makeWindow({ backend,
    storage: { dd_ui_state: JSON.stringify({ localPath: "/vol1/1000/Gone", cloudPath: "/goneC" }) } });
  await flush();
  const doc = env.document;
  const urls = env.backend.state.calls.map((c) => decodeURIComponent(c.url));
  check("先按记忆路径请求（记忆生效）",
        urls.some((u) => u.indexOf("/vol1/1000/Gone") >= 0));
  check("本地记忆失效 → 回退默认根 /vol1/1000",
        urls.some((u) => u.endsWith("/api/local/list?path=/vol1/1000")));
  check("云端记忆失效 → 回退默认根 /",
        urls.some((u) => u.endsWith("/api/cloud/list?path=/")));
  check("本地列表照常渲染（不白屏）",
        doc.querySelectorAll("#localList .flist-row").length >= 1,
        doc.querySelectorAll("#localList .flist-row").length + " 行");
  check("云端列表照常渲染（不白屏）",
        doc.querySelectorAll("#cloudList .flist-row").length >= 1);
  check("面包屑落在回退目录而非失效目录",
        doc.getElementById("localCrumb").textContent.indexOf("1000") >= 0 &&
        doc.getElementById("localCrumb").textContent.indexOf("Gone") < 0,
        doc.getElementById("localCrumb").textContent);
  check("降级过程没有抛异常", env.pageErrors.length === 0, env.pageErrors.join(" | "));
  env.window.close();
}

/** 密码 / OTP 绝不落盘：全量扫描 localStorage 的任何键与值。 */
async function testPasswordNeverPersistedV21() {
  console.log("\n[密码 / OTP 绝不持久化]");
  const backend = makeBackend({
    "api/connection": () => ({ ok: true, connection: {
      online: false, configured: false, baseUrl: "", username: "" } }),
  });
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  const SECRET = "TopSecret-P@ssw0rd-42";
  const OTP = "654321";
  doc.getElementById("inUrl").value = "http://192.168.0.10:5244";
  doc.getElementById("inUser").value = "admin";
  doc.getElementById("inPass").value = SECRET;
  doc.getElementById("inOtp").value = OTP;
  doc.getElementById("btnDoLogin").dispatchEvent(
    new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  const login = env.backend.state.calls.find((c) => c.url.endsWith("/api/connect"));
  check("登录请求确实携带密码（证明走了真实路径）",
        !!(login && login.body && login.body.password === SECRET));

  const store = env.window.localStorage;
  let leak = null;
  for (let i = 0; i < store.length; i++) {
    const k = store.key(i);
    const v = store.getItem(k);
    if ((v || "").indexOf(SECRET) >= 0 || (v || "").indexOf(OTP) >= 0) leak = k;
    if (k.indexOf(SECRET) >= 0 || k.indexOf(OTP) >= 0) leak = k;
  }
  check("localStorage 中任何键/值都不含密码或 OTP", leak === null, "疑似泄漏键：" + leak);
  check("地址/用户名允许记忆（对照组）",
        !!store.getItem("ol_url") && !!store.getItem("ol_user"));
  env.window.close();
}

/** 「全部重试」请求进行中禁用，防重复点。 */
async function testRetryAllNoDoubleClick() {
  console.log("\n[全部重试 · 请求中禁用防重复点]");
  const backend = makeBackend();
  backend.state.tasks = [
    { id: "t1", name: "bad.mkv", remotePath: "/bad.mkv", size: 10, uploaded: 0,
      status: "failed", error: "e" },
  ];
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  const btn = doc.getElementById("btnRetryAll");
  check("有失败项时按钮可见", !btn.classList.contains("hidden"));
  const before = env.backend.state.calls.filter(
    (c) => c.url.endsWith("/api/upload/retry")).length;
  btn.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  check("点击后立即禁用（防重复点）", btn.disabled === true);
  // 请求尚未返回时再点一次，应当被 disabled 守卫拦下
  btn.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  const after = env.backend.state.calls.filter(
    (c) => c.url.endsWith("/api/upload/retry")).length;
  eq("重复点击只发一次请求", after - before, 1);
  check("请求完成后恢复可点", btn.disabled === false);
  check("请求体恰为 {all:true}",
        env.backend.state.retryPayload && env.backend.state.retryPayload.all === true);
  env.window.close();
}

/** 全状态矩阵：徽标齐全 + 筛选归属（skipped/cancelled 不算进行中）。 */
async function testQueueStatusMatrixV21() {
  console.log("\n[状态矩阵 · 徽标与筛选归属]");
  const mk = (id, status, extra) => Object.assign({
    id: id, name: id + ".mkv", remotePath: "/" + id + ".mkv",
    size: 100, uploaded: status === "done" ? 100 : 0, status: status, error: extra || "" });
  const backend = makeBackend();
  backend.state.tasks = [
    mk("p", "pending"), mk("u", "uploading"), mk("v", "verifying"),
    mk("d", "done"), mk("f", "failed", "boom"), mk("s", "skipped"),
    mk("c", "cancelled"),
  ];
  const env = makeWindow({ backend });
  await flush();
  const doc = env.document;
  ["pending", "uploading", "verifying", "done", "failed", "skipped", "cancelled"].forEach((s) => {
    check("状态徽标 .qstat.st-" + s + " 存在",
          !!doc.querySelector("#queueBody .qstat.st-" + s));
  });
  eq("全部：7 行", doc.querySelectorAll("#queueBody .qrow").length, 7);
  const click = (f) => doc.querySelector(
    '#queueFilter .fbtn[data-filter="' + f + '"]').dispatchEvent(
      new env.window.MouseEvent("click", { bubbles: true }));
  click("active");
  eq("进行中仅 pending+uploading+verifying=3（skipped/cancelled 不算）",
     doc.querySelectorAll("#queueBody .qrow").length, 3);
  click("failed");
  eq("失败：1 行", doc.querySelectorAll("#queueBody .qrow").length, 1);
  click("done");
  eq("已完成：1 行", doc.querySelectorAll("#queueBody .qrow").length, 1);
  env.window.close();
}

/** 空队列：无任务文案 + 全部重试隐藏 + 统计为空。 */
async function testQueueEmptyStateV21() {
  console.log("\n[空队列态]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  eq("无任务显示「暂无任务」", doc.getElementById("queueBody").textContent, "暂无任务");
  check("无失败项时「全部重试」隐藏",
        doc.getElementById("btnRetryAll").classList.contains("hidden"));
  eq("无任务时头部统计为空", doc.getElementById("queueStat").textContent, "");
  eq("筛选四档齐全", doc.querySelectorAll("#queueFilter .fbtn").length, 4);
  env.window.close();
}

/* ---- v2.8.4 修复回归 ---- */

/**
 * 暂停按钮曾经拿「按钮文案」反推当前状态：
 *   const cur = tasks.length && btnPause.textContent === "全部暂停";
 * 任务列表为空时 cur 恒为 0（falsy），于是暂停后再也点不回恢复。
 */
async function testPauseUsesServerState() {
  console.log("\n[暂停按钮以服务端状态为准]");
  const backend = makeBackend();
  const env = makeWindow({ backend: backend });
  await flush();
  const doc = env.document;
  const btn = doc.getElementById("btnPause");
  eq("初始文案「全部暂停」", btn.textContent, "全部暂停");

  await btn.onclick();
  await flush();
  eq("空队列也能暂停", backend.state.paused, true);
  eq("暂停后文案「全部恢复」", btn.textContent, "全部恢复");

  await btn.onclick();
  await flush();
  eq("空队列下也能恢复（旧实现在这里永远失败）", backend.state.paused, false);
  eq("恢复后文案「全部暂停」", btn.textContent, "全部暂停");

  // 有任务时同样按服务端状态走
  backend.state.tasks = [
    { id: "p1", name: "p.bin", remotePath: "/p.bin", size: 10, uploaded: 0,
      status: "pending", error: "" },
  ];
  await btn.onclick();
  await flush();
  eq("有任务时也能暂停", backend.state.paused, true);
  let last = backend.state.calls.filter((c) => c.url.includes("/api/upload/pause")).pop();
  eq("暂停请求体 paused=true", last.body.paused, true);
  await btn.onclick();
  await flush();
  last = backend.state.calls.filter((c) => c.url.includes("/api/upload/pause")).pop();
  eq("恢复请求体 paused=false", last.body.paused, false);
  env.window.close();
}

/** 退避等待与取消中都要有明确文案，不能显示成"上传中 0%"。 */
async function testQueueBackoffAndCancelling() {
  console.log("\n[退避 / 取消中状态文案]");
  const backend = makeBackend();
  const now = Math.floor(Date.now() / 1000);
  backend.state.tasks = [
    { id: "t1", name: "backoff.bin", remotePath: "/backoff.bin", size: 100,
      uploaded: 0, status: "uploading", speed: 0, retryAt: now + 30,
      error: "第 1 次失败，2s 后自动重试：网络抖动" },
    { id: "t2", name: "cancelling.bin", remotePath: "/cancelling.bin", size: 100,
      uploaded: 40, status: "uploading", speed: 2048, cancelling: true, error: "" },
  ];
  const env = makeWindow({ backend: backend });
  await flush();
  const rows = Array.from(env.document.querySelectorAll(".qrow"));
  const pick = (n) => rows.find((r) => r.querySelector(".qn").textContent.includes(n));
  const backoff = pick("backoff.bin");
  check("退避中显示「等待重试 Ns」", /^等待重试 \d+s$/.test(
    backoff.querySelector(".qstat").textContent),
    "实际 " + backoff.querySelector(".qstat").textContent);
  const cancelling = pick("cancelling.bin");
  eq("取消中显示「取消中…」",
     cancelling.querySelector(".qstat").textContent, "取消中…");
  env.window.close();
}

/** 同步模式下拉不能再提供后端拒绝实现的两项（镜像同步 / 移动删除源）。 */
async function testSyncModeOnlyAdd() {
  console.log("\n[同步模式只保留「只新增」]");
  const env = makeWindow();
  await flush();
  const doc = env.document;
  const opts = Array.from(doc.querySelectorAll("#bdMode option")).map((o) => o.value);
  eq("下拉只剩 add", opts.join(","), "add");
  check("不再出现 full / move（后端已下线，选了也只会静默降级）",
        opts.indexOf("full") < 0 && opts.indexOf("move") < 0);
  env.window.close();
}

/** 对比深度留空时应省略字段（交给服务端用默认值），而不是算成 0。 */
async function testCompareDepthOptional() {
  console.log("\n[对比深度可留空]");
  const backend = makeBackend();
  const env = makeWindow({ backend: backend });
  await flush();
  const doc = env.document;
  doc.getElementById("cmpLocal").value = "/vol1/1000";
  doc.getElementById("cmpDepth").value = "";
  doc.getElementById("btnCompare").onclick();
  await flush();
  let call = backend.state.calls.filter((c) => c.url.includes("/api/compare")).pop();
  check("深度留空 → 请求体里没有 depth",
        call && call.body && !("depth" in call.body),
        JSON.stringify(call && call.body));

  doc.getElementById("cmpDepth").value = "5";
  doc.getElementById("btnCompare").onclick();
  await flush();
  call = backend.state.calls.filter((c) => c.url.includes("/api/compare")).pop();
  eq("填了深度就带上", call.body.depth, 5);
  env.window.close();
}

/* =========================================================================
 * v2.8.5 性能优化回归
 * ====================================================================== */

/**
 * 队列轮询自适应：有活跃任务时 2s 一问，全空闲时降到 8s。
 * 断言方式见 makeWindow 里 captureTimers 的注释。
 */
async function testAdaptiveTaskPollingV285() {
  console.log("\n[v2.8.5 · 队列轮询自适应降频]");
  const backend = makeBackend();
  backend.state.tasks = [];
  const env = makeWindow({ backend: backend, captureTimers: true });
  await flush();
  const win = env.window;
  const lastInterval = () => env.intervals[env.intervals.length - 1];

  check("顶层函数声明可外部调用（用于确定性推进轮询节奏）",
        typeof win.loadTasks === "function");
  eq("空闲队列 → 轮询间隔 8000ms", lastInterval(), 8000);

  backend.state.tasks = [{ id: "p1", name: "p1.mkv", remotePath: "/p1.mkv",
    size: 10, uploaded: 0, status: "pending", error: "" }];
  await win.loadTasks();
  eq("出现活跃任务 → 提速到 2000ms", lastInterval(), 2000);

  backend.state.tasks = [{ id: "p1", name: "p1.mkv", remotePath: "/p1.mkv",
    size: 10, uploaded: 10, status: "done", error: "" }];
  await win.loadTasks();
  eq("任务全部收尾 → 降回 8000ms", lastInterval(), 8000);

  win.close();
}

/**
 * 队列响应指纹：同一份数据重复拉到，不得重建 DOM。
 * 这是「任务上千时每次轮询都重排整屏」那条性能问题的回归闸门。
 */
async function testQueueRedrawFingerprintV285() {
  console.log("\n[v2.8.5 · 队列指纹防重绘]");
  const backend = makeBackend();
  backend.state.tasks = [
    { id: "d1", name: "d1.mkv", remotePath: "/d1.mkv",
      size: 100, uploaded: 100, status: "done", error: "" },
    { id: "d2", name: "d2.mkv", remotePath: "/d2.mkv",
      size: 200, uploaded: 0, status: "pending", error: "" },
  ];
  const env = makeWindow({ backend: backend });
  await flush();
  const win = env.window, doc = env.document;
  const body = doc.getElementById("queueBody");
  const first = body.firstElementChild;
  check("首屏渲染出队列行", !!first);
  eq("两行都渲染", body.querySelectorAll(".qrow").length, 2);

  const countBefore = backend.state.calls.filter((c) =>
    c.url.includes("/api/upload/list")).length;
  await win.loadTasks();
  check("重复拉取确实发生了", backend.state.calls.filter((c) =>
    c.url.includes("/api/upload/list")).length > countBefore);
  check("数据未变 → 不重建 DOM（首行仍是同一个节点）",
        body.firstElementChild === first);

  // 进度变化必须重绘，否则界面会「卡住不动」
  backend.state.tasks[1].uploaded = 50;
  backend.state.tasks[1].status = "uploading";
  await win.loadTasks();
  check("数据变化 → 重绘（首行换成新节点）",
        body.firstElementChild !== first);
  check("重绘后仍有两行", body.querySelectorAll(".qrow").length === 2);

  // paused 是队列「表头态」，也该进指纹
  backend.state.paused = true;
  await win.loadTasks();
  eq("暂停态切换也触发重绘（按钮文字改「全部恢复」）",
     doc.getElementById("btnPause").textContent, "全部恢复");

  win.close();
}

/**
 * 设置页新增「扫描并发数」：回填、保存、上下限夹取。
 * 夹取逻辑与后端 sanitize_config 保持一致（1..32，非法回落 8）。
 */
async function testScanConcurrencySettingV285() {
  console.log("\n[v2.8.5 · 扫描并发数设置项]");
  const CFG = { localRoot: "", cloudRoot: "/", uploadConcurrency: 2, maxAttempts: 3,
    skipExisting: true, verifyAfterUpload: true, scanDepth: 3, scanConcurrency: 12,
    excludeNames: ["*.tmp"], localSourceMode: "fs", localSourceRoot: "" };
  const backend = makeBackend({
    "api/config": (q, method, init) => {
      if (method === "POST") return { ok: true, config: JSON.parse(init.body) };
      return { ok: true, config: CFG };
    },
  });
  const env = makeWindow({ backend: backend });
  await flush();
  const win = env.window, doc = env.document;
  const el = doc.getElementById("cfgScanConc");
  check("设置页存在「扫描并发数」输入框", !!el);
  check("输入框是数字型且带 1..32 范围",
        el && el.getAttribute("type") === "number" &&
        el.getAttribute("min") === "1" && el.getAttribute("max") === "32",
        el && el.outerHTML);
  doc.querySelector('[data-tab="settings"]').dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();
  eq("按服务端配置回填（12）", el.value, "12");

  const setVal = (v) => {
    el.value = v;
    el.dispatchEvent(new win.Event("change", { bubbles: true }));
  };
  const savedBody = () => {
    const c = backend.state.calls.filter((c) => c.url.endsWith("/api/config") &&
      c.method === "POST").pop();
    return c && c.body;
  };
  const save = async () => {
    doc.getElementById("btnSaveCfg").dispatchEvent(
      new win.MouseEvent("click", { bubbles: true }));
    await flush();
  };

  setVal("20");
  await save();
  eq("保存体带上 scanConcurrency=20", savedBody() && savedBody().scanConcurrency, 20);

  setVal("99");
  await save();
  eq("超过上限 → 夹到 32", savedBody() && savedBody().scanConcurrency, 32);

  setVal("-5");
  await save();
  eq("低于下限 → 夹到 1", savedBody() && savedBody().scanConcurrency, 1);

  setVal("");
  await save();
  eq("留空 → 回落默认 8", savedBody() && savedBody().scanConcurrency, 8);

  // 改动参与草稿记忆（刷新后不丢）
  setVal("16");
  await sleep(500);
  const draft = JSON.parse(win.localStorage.getItem("dd_ui_state") || "{}");
  eq("改动即写入未保存草稿", draft.cfgDraft && draft.cfgDraft.cfgScanConc, "16");

  win.close();
}

async function main() {
  console.log("前端测试 v2.0（jsdom，严格网关桩：只放行 /app/" + APP + "/）");
  const cases = [
    testBaseDetection,
    testBaseFallback,
    testUnconnectedShowsConnectPanel,
    testConnectPanelExclusiveWithTabs,
    testSelectAndEnqueue,
    testQueueRenderingAndRetry,
    testRetryAllButton,
    testQueueFilter,
    testQueueStatusColors,
    testFormMemory,
    testFormMemoryStorageDisabled,
    testCompareFlow,
    testComparePrerequisite,
    // v2.9.0 目录比对并入文件传输页 + 双向差异操作
    testDiffBatchUpload,
    testDiffBatchDownload,
    testDiffClearNeedsConfirm,
    testDiffRowActions,
    testDiffStatusFilter,
    testDiffFilterEmptyGroup,
    testDiffSameGroupReadonly,
    testDiffSameTruncated,
    testDiffFilterKeepsSelection,
    testCompareFromPanes,
    testCompareTabMigration,
    // v2.9.2 两侧来源都可选（云端↔云端 / 本地↔本地）
    testCompareSideDefaultsAndSwitch,
    testCompareCloudToCloud,
    testCompareCloudToCloudCopy,
    // v2.9.3 两侧同源时不再互相改写路径
    testSameSourceCloudKeepsPeer,
    testSameSourceLocalSymmetric,
    testCrossSourcePanesDoNotCollide,
    testDefaultSidesStillFollowPanes,
    testSameSourcePickFillsOneSide,
    testSamePositionMarksBothInputs,
    testRestoreDedupesStoredSameSource,
    // v2.9.4 左栏自己也能是 OpenList 源（界面上有两个云端栏）
    testCloudLeftPaneFollowsItsOwnPane,
    testCloudLeftPaneNoLocalPaneHint,
    testLocalSourceModeSwitchRefollows,
    testTouchedSideNotOverriddenByModeSwitch,
    testCompareLocalToLocalCopy,
    testCompareEmptySideAndError,
    testCompareServerSideError,
    testCompareResultKeepsItsOwnSides,
    testNoNativeDialogs,
    testStorageDisabled,
    testXssEscaping,
    // v2.1.0 新增
    testLayoutShrinkChainV21,
    testSettingsLayoutV21,
    testSettingsSaveRoundTripV21,
    testMemoryDirGoneFallback,
    testPasswordNeverPersistedV21,
    testRetryAllNoDoubleClick,
    testQueueStatusMatrixV21,
    testQueueEmptyStateV21,
    // v2.8.4 修复回归
    testPauseUsesServerState,
    testQueueBackoffAndCancelling,
    testSyncModeOnlyAdd,
    testCompareDepthOptional,
    // v2.8.5 性能优化回归
    testAdaptiveTaskPollingV285,
    testQueueRedrawFingerprintV285,
    testScanConcurrencySettingV285,
  ];
  for (const fn of cases) {
    try {
      await fn();
    } catch (err) {
      failed++;
      failures.push(fn.name + " 抛异常：" + (err && err.stack || err));
      process.stdout.write("  FAIL " + fn.name + " 抛异常：" + err + "\n");
    }
  }

  console.log("\n通过 " + passed + "，失败 " + failed);
  if (failures.length) {
    console.log("\n失败清单：");
    failures.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
}

main();
