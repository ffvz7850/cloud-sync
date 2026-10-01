/**
 * 绑定功能前端测试（jsdom）：严格网关桩，只放行 /app/cloud-sync/。
 *
 * 覆盖：标签页切换、空状态、绑定列表渲染、新建表单、多目标编辑器、
 * 详情展开/缺失文件、单文件/批量上传、自动上传开关、目录选择器、删除。
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

async function flush(rounds) {
  for (let i = 0; i < (rounds || 10); i++) {
    await sleep(15);
  }
}

function makeResponse(status, payload) {
  const body = typeof payload === "string" ? payload : JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(body),
    json: () => Promise.resolve(JSON.parse(body)),
  };
}

/** 按文字找按钮（绑定卡片/详情里按钮多用文字区分，没有专用类名）。 */
function btnByText(container, text) {
  return [...container.querySelectorAll("button")].find(
    (b) => b.textContent.trim() === text);
}

/** 构造一个绑定对象（含缺失文件）。 */
function makeBinding(overrides) {
  const b = {
    id: "bd_test1",
    name: "文档备份",
    localPath: "/vol1/1000/docs",
    cloudTargets: [
      { id: "ct_a", path: "/backup", enabled: true },
      { id: "ct_b", path: "/mirror", enabled: true },
    ],
    scanDepth: 8,
    autoUpload: false,
    excludeNames: [],
    createdAt: 1700000000,
    updatedAt: 1700000000,
    lastScannedAt: 1700000000,
    scanStatus: "done",
    scanError: "",
    totalLocalFiles: 3,
    missingCount: 2,
    inFlightCount: 0,
    doneCount: 1,
    files: [
      {
        relPath: "a.txt", name: "a.txt", size: 100, mtime: 1700000000,
        targets: {
          ct_a: { status: "missing", cloudSize: 0, error: "", taskId: "" },
          ct_b: { status: "missing", cloudSize: 0, error: "", taskId: "" },
        },
      },
      {
        relPath: "b.txt", name: "b.txt", size: 200, mtime: 1700000000,
        targets: {
          ct_a: { status: "uploaded", cloudSize: 200, error: "", taskId: "" },
          ct_b: { status: "missing", cloudSize: 0, error: "", taskId: "" },
        },
      },
    ],
  };
  return Object.assign(b, overrides || {});
}

/** 假后端：已连接，可配置绑定数据。 */
function makeBackend(options) {
  options = options || {};
  const state = {
    bindings: options.bindings || [],
    uploadCalls: [],
    createCalls: [],
    scanCalls: [],
    // 需要挂住的请求：提交那一下必须「按暂停」才能断言提交进行中的界面
    // （按钮禁用、状态条、重复点击被拦）。不这么做就只能靠 sleep 猜时序。
    heldPaths: new Set(),
    held: {},
    // 覆盖 /api/bindings/progress 返回的 submit 段。
    submitProgress: null,
  };

  function respond(pathname, query, method, init) {
    switch (pathname) {
      case "api/info":
        return { ok: true, app: APP, version: "2.2.0" };
      case "api/connection":
        return { ok: true, connection: {
          online: true, configured: true,
          baseUrl: "http://192.168.0.10:5244", username: "admin",
          user: { username: "admin" } } };
      case "api/config":
        return { ok: true, config: {
          localRoot: "", cloudRoot: "/", uploadConcurrency: 2, maxAttempts: 3,
          skipExisting: true, verifyAfterUpload: true, scanDepth: 3,
          excludeNames: [] } };
      case "api/local/list":
        return { ok: true, path: "/vol1/1000", aggregate: false, entries: [] };
      case "api/cloud/list":
        return { ok: true, path: "/", entries: [] };
      case "api/upload/list":
        return { ok: true, tasks: [],
                 counts: { done: 0, failed: 0, pending: 0, uploading: 0 } };
      case "api/log":
        return { ok: true, lines: [] };
      case "api/bindings":
        return { ok: true, bindings: state.bindings };
      case "api/bindings/detail": {
        const id = (query.match(/id=([^&]+)/) || [])[1] || "";
        const found = state.bindings.find((b) => b.id === id);
        if (!found) return null;
        return { ok: true, binding: found };
      }
      case "api/bindings/progress": {
        // 提交阶段的进度来自后端内存（submit=1 的轻量查询）；
        // 云端复制阶段的进度来自 OpenList 任务列表。
        const submit = state.submitProgress || {
          active: false, phase: "done", done: 0, total: 0, files: 0,
          targets: 0, submitted: 0, failed: 0, percent: 100, message: "",
          startedAt: 0, finishedAt: 0, elapsed: 0 };
        return { ok: true, progress: {
          running: [], hasActive: false, error: "", submit: submit } };
      }
      default:
        return null;
    }
  }

  function respondPost(pathname, init) {
    const body = init.body ? JSON.parse(init.body) : {};
    switch (pathname) {
      case "api/bindings/create": {
        state.createCalls.push(body);
        const nb = {
          id: "bd_new",
          name: body.name || "新建",
          localPath: body.localPath,
          cloudTargets: (body.cloudTargets || []).map((p, i) =>
            typeof p === "string"
              ? { id: "ct_n" + i, path: p, enabled: true }
              : { id: "ct_n" + i, path: p.path, enabled: p.enabled !== false }),
          scanDepth: body.scanDepth || 8,
          autoUpload: !!body.autoUpload,
          excludeNames: body.excludeNames || [],
          files: [],
          scanStatus: "idle",
          missingCount: 0, inFlightCount: 0, doneCount: 0,
          totalLocalFiles: 0,
        };
        state.bindings.push(nb);
        return { ok: true, binding: nb };
      }
      case "api/bindings/update": {
        const found = state.bindings.find((b) => b.id === body.id);
        if (found) Object.assign(found, body);
        return { ok: true, binding: found };
      }
      case "api/bindings/delete": {
        state.bindings = state.bindings.filter((b) => b.id !== body.id);
        return { ok: true };
      }
      case "api/bindings/scan":
        state.scanCalls.push(body);
        return { ok: true };
      case "api/bindings/scan-all":
        return { ok: true, started: state.bindings.length };
      case "api/bindings/upload": {
        state.uploadCalls.push(body);
        const files = body.all ? 5 : (body.relPaths || []).length;
        // 真实后端是**阻塞**的：返回时就已提交完，并把结论一并带回来
        // （"提交进行中"的状态只能通过 progress 接口看到）。
        return { ok: true, queued: files,
                 submit: { active: false, phase: "done", done: 3, total: 3,
                           files: files, targets: 2, submitted: files, failed: 0,
                           percent: 100, message: "", startedAt: 1700000000,
                           finishedAt: 1700000002, elapsed: 2 } };
      }
      default:
        return null;
    }
  }

  const backend = function (url, init) {
    if (!url.startsWith(ALLOWED_PREFIX)) {
      return Promise.resolve(makeResponse(404, "not found"));
    }
    const rest = url.slice(ALLOWED_PREFIX.length);
    const split = rest.indexOf("?");
    const pathname = split < 0 ? rest : rest.slice(0, split);
    const query = split < 0 ? "" : rest.slice(split + 1);
    const method = (init && init.method) || "GET";
    let payload;
    if (options.overrides && options.overrides[pathname]) {
      payload = options.overrides[pathname](query, method, init);
    } else if (method === "POST") {
      payload = respondPost(pathname, init);
    } else {
      payload = respond(pathname, query, method, init);
    }
    if (!payload) return Promise.resolve(makeResponse(404, "not found"));
    // 被挂住的请求：返回一个等测试放行的 Promise。
    if (state.heldPaths.has(pathname)) {
      return new Promise((resolve) => {
        state.held[pathname] = () => {
          delete state.held[pathname];
          resolve(makeResponse(200, payload));
        };
      });
    }
    return Promise.resolve(makeResponse(200, payload));
  };

  backend.state = state;
  return backend;
}

function makeWindow(options) {
  options = options || {};
  const backend = options.backend || makeBackend(options);
  const virtualConsole = new VirtualConsole();
  const pageErrors = [];
  virtualConsole.on("jsdomError", (err) => pageErrors.push(String(err && err.message || err)));

  const dom = new JSDOM(HTML, {
    url: ORIGIN + "/app/" + APP,
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole,
    beforeParse(window) {
      window.fetch = function (url, init) {
        const absolute = /^https?:/i.test(url)
          ? url
          : new URL(url, window.location.href).href;
        return backend(absolute, init);
      };
    },
  });
  return { window: dom.window, document: dom.window.document, backend, pageErrors };
}

/** 切换到绑定页。 */
async function gotoBindings(doc, win) {
  doc.querySelector('#tabs button[data-tab="bindings"]').dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();
}

// ---------------------------------------------------------------------------
// 测试用例
// ---------------------------------------------------------------------------

/** 标签页：绑定按钮存在，可切换。 */
async function testBindingsTab() {
  console.log("\n[绑定标签页]");
  const env = makeWindow();
  await flush();
  const doc = env.document;

  const tabBtn = doc.querySelector('#tabs button[data-tab="bindings"]');
  check("绑定标签按钮存在", !!tabBtn);
  eq("同步按钮文字", tabBtn.textContent.trim(), "同步备份");

  tabBtn.dispatchEvent(new env.window.MouseEvent("click", { bubbles: true }));
  await flush();
  const page = doc.getElementById("page-bindings");
  check("切换后绑定页可见", !page.classList.contains("hidden"));
  check("传输页隐藏", doc.getElementById("page-transfer").classList.contains("hidden"));
  env.window.close();
}

/** 空状态：没有绑定时显示提示。 */
async function testBindingsEmptyState() {
  console.log("\n[绑定空状态]");
  const env = makeWindow({ bindings: [] });
  await flush();
  const doc = env.document;
  await gotoBindings(doc, env.window);

  const empty = doc.querySelector("#bindList .empty");
  check("显示空状态提示", !!empty);
  check("提示文字包含「新建同步」", empty.textContent.includes("新建同步"));
  env.window.close();
}

/** 绑定列表渲染：卡片显示名称、路径、统计。 */
async function testBindingsListRender() {
  console.log("\n[绑定列表渲染]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  await gotoBindings(doc, env.window);

  const cards = doc.querySelectorAll("#bindList .bcard");
  eq("渲染 1 张绑定卡片", cards.length, 1);
  const card = cards[0];
  check("卡片显示绑定名称", card.textContent.includes("文档备份"));
  check("卡片显示本地路径", card.textContent.includes("/vol1/1000/docs"));
  check("卡片显示云端目标 /backup", card.textContent.includes("/backup"));
  check("卡片显示云端目标 /mirror", card.textContent.includes("/mirror"));
  check("卡片显示待处理数", card.textContent.includes("待处理 2"));
  check("卡片显示本地文件数", card.textContent.includes("本地文件 3"));
  env.window.close();
}

/** 新建绑定表单：打开、字段齐全、多目标编辑器。 */
async function testNewBindingForm() {
  console.log("\n[新建绑定表单]");
  const env = makeWindow({ bindings: [] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);

  doc.getElementById("btnNewBinding").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();

  const overlay = doc.getElementById("bindOverlay");
  check("表单弹层显示", !overlay.classList.contains("hidden"));
  check("名称输入框存在", !!doc.getElementById("bdName"));
  check("本地目录输入框存在", !!doc.getElementById("bdLocal"));
  check("扫描范围提示存在（已自动到底）", !!doc.querySelector("#bindDlg .frow .hint"));
  check("自动上传开关存在", !!doc.getElementById("bdAuto"));
  check("排除规则框存在", !!doc.getElementById("bdExcl"));

  let editors = doc.querySelectorAll("#bdTargets .tgt-editor");
  eq("初始 1 个云端目标", editors.length, 1);
  check("初始目标有浏览按钮", !!btnByText(editors[0], "浏览"));
  check("初始目标有删除按钮", !!btnByText(editors[0], "删除"));

  doc.getElementById("bdAddTarget").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();
  editors = doc.querySelectorAll("#bdTargets .tgt-editor");
  eq("添加后 2 个云端目标", editors.length, 2);
  check("第二个目标有删除按钮", !!btnByText(editors[1], "删除"));

  btnByText(editors[1], "删除").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();
  editors = doc.querySelectorAll("#bdTargets .tgt-editor");
  eq("删除后回到 1 个云端目标", editors.length, 1);
  env.window.close();
}

/** 表单提交：填写后创建，调用 create API。 */
async function testFormSubmit() {
  console.log("\n[表单提交]");
  const env = makeWindow({ bindings: [] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  doc.getElementById("btnNewBinding").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();

  doc.getElementById("bdName").value = "照片备份";
  doc.getElementById("bdLocal").value = "/vol1/1000/photos";
  let targetInputs = doc.querySelectorAll("#bdTargets .tgt-editor input[type=text]");
  targetInputs[0].value = "/photos";
  doc.getElementById("bdAddTarget").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();
  targetInputs = doc.querySelectorAll("#bdTargets .tgt-editor input[type=text]");
  targetInputs[1].value = "/photos-mirror";

  doc.getElementById("bdSaveBtn").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  eq("create API 被调用 1 次", env.backend.state.createCalls.length, 1);
  const payload = env.backend.state.createCalls[0];
  eq("提交名称", payload.name, "照片备份");
  eq("提交本地路径", payload.localPath, "/vol1/1000/photos");
  eq("提交 2 个云端目标", payload.cloudTargets.length, 2);
  check("表单弹层关闭",
        doc.getElementById("bindOverlay").classList.contains("hidden"));
  env.window.close();
}

/** 表单校验：本地路径为空时不提交。 */
async function testFormValidation() {
  console.log("\n[表单校验]");
  const env = makeWindow({ bindings: [] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  doc.getElementById("btnNewBinding").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();

  doc.getElementById("bdSaveBtn").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();
  eq("create API 未被调用", env.backend.state.createCalls.length, 0);
  check("弹层仍显示",
        !doc.getElementById("bindOverlay").classList.contains("hidden"));
  check("显示错误提示", doc.getElementById("bdError").textContent.length > 0);
  env.window.close();
}

/** 详情展开：点「文件」按钮显示缺失文件列表。 */
async function testDetailExpand() {
  console.log("\n[详情展开]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);

  let body = doc.querySelector("#bindList .bcard .bcard-body");
  check("初始详情区未展开", !body.classList.contains("open"));

  // 点「文件」按钮
  const fileBtn = btnByText(doc.querySelector("#bindList .bcard"), "文件");
  check("有「文件」按钮", !!fileBtn);
  fileBtn.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  body = doc.querySelector("#bindList .bcard .bcard-body");
  check("展开后详情区打开", body.classList.contains("open"));
  const rows = body.querySelectorAll(".bfrow");
  eq("显示 2 个文件行", rows.length, 2);
  check("包含 a.txt", body.textContent.includes("a.txt"));
  check("包含 b.txt", body.textContent.includes("b.txt"));
  // files 里只有待处理/进行中的文件，源目录总数得用 totalLocalFiles。
  // 旧实现写「共 2 个文件」，源目录明明有 3 个，看着像扫描漏了一个。
  const barHint = body.querySelector(".bc-files-bar .hint").textContent;
  check("详情栏用源目录文件总数", barHint.includes("本地共 3 个文件"), barHint);
  check("详情栏单独给出待处理数", barHint.includes("2 个待处理"), barHint);
  env.window.close();
}

/** 多目标状态：b.txt 在 backup 已传、mirror 缺失，标签正确。 */
async function testMultiTargetStatus() {
  console.log("\n[多目标状态]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  btnByText(doc.querySelector("#bindList .bcard"), "文件").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  const body = doc.querySelector("#bindList .bcard .bcard-body");
  const rows = body.querySelectorAll(".bfrow");
  const bRow = rows[1];
  check("b.txt 有「已存在」标签", bRow.textContent.includes("backup·已存在"));
  check("b.txt 有「缺失」标签", bRow.textContent.includes("mirror·缺失"));
  // 状态徽标类名
  check("有 .bf-tgt.uploaded", !!bRow.querySelector(".bf-tgt.uploaded"));
  check("有 .bf-tgt.missing", !!bRow.querySelector(".bf-tgt.missing"));
  env.window.close();
}

/** 单文件上传：点击行的「上传」按钮，调用 upload API。 */
async function testSingleFileUpload() {
  console.log("\n[单文件上传]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  btnByText(doc.querySelector("#bindList .bcard"), "文件").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  const aRow = doc.querySelectorAll("#bindList .bcard .bfrow")[0];
  const uploadBtn = btnByText(aRow, "上传");
  check("a.txt 有「上传」按钮", !!uploadBtn);
  uploadBtn.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  await flush(12);

  eq("upload API 调用 1 次", env.backend.state.uploadCalls.length, 1);
  const payload = env.backend.state.uploadCalls[0];
  check("包含 a.txt", payload.relPaths.includes("a.txt"));
  env.window.close();
}

/** 全部上传：点击详情操作栏的「全部上传」按钮。 */
async function testUploadAll() {
  console.log("\n[全部上传]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  btnByText(doc.querySelector("#bindList .bcard"), "文件").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  const bar = doc.querySelector("#bindList .bcard .bc-files-bar");
  const btn = btnByText(bar, "全部上传");
  check("有「全部上传」按钮", !!btn);
  btn.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  await flush(12);

  const payload = env.backend.state.uploadCalls[0];
  check("all=true", payload.all === true);
  env.window.close();
}

/** 上传选中：勾选后点「上传选中」。 */
async function testUploadSelected() {
  console.log("\n[上传选中]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  btnByText(doc.querySelector("#bindList .bcard"), "文件").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  // 勾选 a.txt
  const aRow = doc.querySelectorAll("#bindList .bcard .bfrow")[0];
  aRow.querySelector('input[type="checkbox"]').dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();

  const bar = doc.querySelector("#bindList .bcard .bc-files-bar");
  btnByText(bar, "上传选中").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(12);

  const payload = env.backend.state.uploadCalls[0];
  check("包含 a.txt", payload.relPaths.includes("a.txt"));
  check("不包含 b.txt", !payload.relPaths.includes("b.txt"));
  env.window.close();
}

/** 扫描按钮：点击触发 scan API。 */
async function testScanButton() {
  console.log("\n[扫描按钮]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);

  btnByText(doc.querySelector("#bindList .bcard"), "扫描").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(10);
  eq("scan API 调用 1 次", env.backend.state.scanCalls.length, 1);
  env.window.close();
}

/** 自动上传开关：卡片上的开关，切换后调用 update API。 */
async function testAutoUploadSwitch() {
  console.log("\n[自动上传开关]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);

  const sw = doc.querySelector("#bindList .bcard .switch input");
  check("自动上传初始未勾选", !sw.checked);
  sw.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  sw.checked = true;
  sw.dispatchEvent(new win.Event("change", { bubbles: true }));
  await flush(10);
  check("已调用 update 开启 autoUpload",
        env.backend.state.bindings[0].autoUpload === true);
  env.window.close();
}

/** 目录选择器：打开本地选择器、显示目录、选择后回填。 */
async function testPicker() {
  console.log("\n[目录选择器]");
  const env = makeWindow({
    bindings: [],
    overrides: {
      "api/cloud/list": (query) => ({
        ok: true,
        path: "/local",
        entries: [
          { name: "docs", path: "/local/docs", isDir: true, size: 0 },
          { name: "photos", path: "/local/photos", isDir: true, size: 0 },
        ],
      }),
    },
  });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  doc.getElementById("btnNewBinding").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();

  doc.getElementById("bdPickLocal").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  const picker = doc.getElementById("pickerOverlay");
  check("选择器弹层显示", !picker.classList.contains("hidden"));
  eq("显示两个目录行", doc.querySelectorAll("#pkBody .pkrow").length, 2);
  check("面包屑存在", !!doc.getElementById("pkCrumb").textContent);
  check("当前路径显示", doc.getElementById("pkCurrent").textContent.includes("/local"));

  doc.getElementById("pkSelectBtn").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush();
  check("选择器关闭", picker.classList.contains("hidden"));
  eq("OpenList 源目录回填", doc.getElementById("bdLocal").value, "/local");
  env.window.close();
}

/** 删除绑定：点「删除」，确认后调用 delete API。 */
async function testDeleteBinding() {
  console.log("\n[删除绑定]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);

  btnByText(doc.querySelector("#bindList .bcard"), "删除").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(10);

  check("出现确认弹层", !doc.getElementById("overlay").classList.contains("hidden"));
  doc.getElementById("dlgOk").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);
  eq("绑定列表清空", doc.querySelectorAll("#bindList .bcard").length, 0);
  env.window.close();
}

// ---------------------------------------------------------------------------
// 一键上传的交互反馈（v2.8.8）
//
// 反馈背景：点「一键上传」后要等十几秒才有反应（每个子目录一组
// ensure_dir + copy），期间界面毫无变化，用户以为没点上就反复点击 ——
// 每一次点击都是一份重复的复制请求。
// ---------------------------------------------------------------------------

/** 提交进行中：进度条实时更新、按钮立刻禁用、重复点击不再发请求。 */
async function testUploadProgressAndBlockDoubleClick() {
  console.log("\n[提交反馈：进度条与防重复点击]");
  const b = makeBinding();
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  btnByText(doc.querySelector("#bindList .bcard"), "文件").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  const card = doc.querySelector("#bindList .bcard");
  const bar = card.querySelector(".bc-files-bar");
  const allBtn = btnByText(bar, "全部上传");
  const selBtn = btnByText(bar, "上传选中");
  const scanBtn = btnByText(card.querySelector(".bc-ops"), "扫描");

  // 后端此刻正在提交：progress 接口返回"进行中"的真实进度。
  env.backend.state.submitProgress = {
    active: true, phase: "submit", done: 3, total: 10, files: 120, targets: 2,
    submitted: 30, failed: 0, percent: 30, message: "",
    startedAt: 1700000000, finishedAt: 0, elapsed: 0,
  };
  // 把提交请求按住，好断言"请求还没回来"那一刻的界面。
  env.backend.state.heldPaths.add("api/bindings/upload");

  allBtn.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  await flush(8);

  eq("提交请求已发出", env.backend.state.uploadCalls.length, 1);

  const subbar = card.querySelector(".subbar");
  check("请求返回前就出现了状态条", !!subbar);
  if (subbar) {
    const txt = subbar.querySelector(".subtxt").textContent;
    check("文案显示实时进度 " + JSON.stringify(txt), /4\/10/.test(txt));
    const fill = subbar.querySelector(".prog > i");
    eq("进度条宽度跟随后端进度", fill.style.width, "30%");
    check("进行中有转圈指示",
          subbar.querySelector(".spin").style.display !== "none");
  }

  // 三处入口都必须禁用，否则用户仍会顺着别的按钮再点一遍。
  check("「全部上传」已禁用", allBtn.disabled === true);
  check("「上传选中」已禁用", selBtn.disabled === true);
  check("「扫描」已禁用（提交期间改状态会打架）", scanBtn.disabled === true);

  // 再点一次：不该产生第二次提交
  const before = env.backend.state.uploadCalls.length;
  allBtn.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  await flush(8);
  eq("重复点击不再提交", env.backend.state.uploadCalls.length, before);

  // 放行：后端收尾（真实后端返回时提交已完成），状态条给出结论、按钮恢复。
  // finishedAt 用**当前时间**：状态条只在提交结束后的 10 分钟窗口内保留，
  // 这是刻意的产品行为，测试数据得跟着。
  const now = Math.floor(Date.now() / 1000);
  env.backend.state.submitProgress = {
    active: false, phase: "done", done: 10, total: 10, files: 120, targets: 2,
    submitted: 120, failed: 0, percent: 100, message: "",
    startedAt: now - 9, finishedAt: now, elapsed: 9,
  };
  env.backend.state.held["api/bindings/upload"]();
  await flush(16);

  const after = doc.querySelector("#bindList .bcard .subbar");
  check("完成后状态条给出已提交数量",
        !!after && /已提交/.test(after.querySelector(".subtxt").textContent));
  const restored = btnByText(doc.querySelector("#bindList .bcard"), "全部上传");
  check("按钮恢复可用", !!restored && restored.disabled === false);
  eq("确认没有第二次提交", env.backend.state.uploadCalls.length, before);
  env.window.close();
}

/** 文件夹视图的「一键上传」：点下去按钮自己先动起来。 */
async function testFolderUploadButtonLoading() {
  console.log("\n[文件夹视图：一键上传按钮 loading]");
  const target = { status: "missing", cloudSize: 0, error: "", taskId: "" };
  const b = makeBinding({
    cloudTargets: [{ id: "ct_a", path: "/backup", enabled: true }],
    files: [
      { relPath: "sub/a.txt", name: "a.txt", size: 100, mtime: 1700000000,
        targets: { ct_a: Object.assign({}, target) } },
      { relPath: "sub/b.txt", name: "b.txt", size: 200, mtime: 1700000000,
        targets: { ct_a: Object.assign({}, target) } },
    ],
  });
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  btnByText(doc.querySelector("#bindList .bcard"), "文件").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  const upBtn = btnByText(doc.querySelector("#bindList .bcard"), "一键上传");
  check("文件夹行有「一键上传」按钮", !!upBtn);

  env.backend.state.heldPaths.add("api/bindings/upload");
  upBtn.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  await flush(8);

  eq("点击后发出提交请求", env.backend.state.uploadCalls.length, 1);
  check("按钮已禁用", upBtn.disabled === true);
  check("按钮文案变为提交中", /提交中/.test(upBtn.textContent));
  check("按钮带转圈指示", !!upBtn.querySelector(".spin"));
  const payload = env.backend.state.uploadCalls[0];
  check("只提交该文件夹下的文件",
        payload.relPaths.length === 2 &&
        payload.relPaths.every((p) => p.indexOf("sub/") === 0));

  env.backend.state.submitProgress = {
    active: false, phase: "done", done: 1, total: 1, files: 2, targets: 1,
    submitted: 2, failed: 0, percent: 100, message: "",
    startedAt: Math.floor(Date.now() / 1000) - 2,
    finishedAt: Math.floor(Date.now() / 1000), elapsed: 2,
  };
  env.backend.state.held["api/bindings/upload"]();
  await flush(16);

  const restored = btnByText(doc.querySelector("#bindList .bcard"), "一键上传");
  check("提交结束后按钮恢复为「一键上传」",
        !!restored && restored.disabled === false &&
        restored.textContent.trim() === "一键上传");
  env.window.close();
}

/** 后端说「正在提交」时，进来就该是禁用态 —— 刷新不能把防护刷掉。 */
async function testSubmittingStateRestoresFromBackend() {
  console.log("\n[刷新恢复：提交中的禁用态]");
  const b = makeBinding({ submitting: true });
  const env = makeWindow({ bindings: [b] });
  await flush();
  const doc = env.document;
  const win = env.window;
  await gotoBindings(doc, win);
  btnByText(doc.querySelector("#bindList .bcard"), "文件").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  await flush(14);

  const card = doc.querySelector("#bindList .bcard");
  const bar = card.querySelector(".bc-files-bar");
  const allBtn = btnByText(bar, "全部上传");
  const selBtn = btnByText(bar, "上传选中");
  const scanBtn = btnByText(card.querySelector(".bc-ops"), "扫描");

  check("「全部上传」进来就是禁用态", !!allBtn && allBtn.disabled === true);
  check("「上传选中」进来就是禁用态", !!selBtn && selBtn.disabled === true);
  check("「扫描」进来就是禁用态", !!scanBtn && scanBtn.disabled === true);

  allBtn.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  await flush(6);
  eq("禁用态下点击不发请求", env.backend.state.uploadCalls.length, 0);
  env.window.close();
}

async function main() {
  console.log("绑定前端测试（jsdom，严格网关桩：只放行 /app/" + APP + "/）");
  const cases = [
    testBindingsTab,
    testBindingsEmptyState,
    testBindingsListRender,
    testNewBindingForm,
    testFormSubmit,
    testFormValidation,
    testDetailExpand,
    testMultiTargetStatus,
    testSingleFileUpload,
    testUploadAll,
    testUploadSelected,
    testUploadProgressAndBlockDoubleClick,
    testFolderUploadButtonLoading,
    testSubmittingStateRestoresFromBackend,
    testScanButton,
    testAutoUploadSwitch,
    testPicker,
    testDeleteBinding,
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
