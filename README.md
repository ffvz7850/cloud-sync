# 云端互传 · 飞牛 fnOS 应用

> **要改代码？** 先看 **`docs/接手指南.md`**（代码地图、路由表、坑清单），
> 再看 **`docs/交接说明.md`**（工程导览、开发铁律、打包流程）。
> OpenList 服务端接口契约见 **`docs/API参考.md`**。本文只讲**怎么用**。

通过 OpenList 在 NAS 本地目录与各云端网盘（豆包、阿里、百度、天翼等）之间做
**增量同步备份**：调用 OpenList 的服务端复制能力，让 OpenList 自己在两个存储之间
搬运文件，不占用 NAS 的 CPU 和带宽。

- **应用名 / id**：云端互传 / `cloud-sync`
- **当前版本**：2.15.48
- **平台**：`all`（x86 与 ARM 通用，纯 Python 标准库，无二进制依赖）
- **依赖**：`python312`（manifest 已声明，应用中心自动装）
- **要求**：fnOS ≥ 1.2.0401（统一网关 micro_app 模式）

---

## 一、功能概览

| 页面 | 功能 |
|---|---|
| **文件对比** | 左栏选本地目录、右栏选云端目录，双向比对差异（仅左侧/仅右侧/大小不一致），一键复制到对侧 |
| **同步备份** | 建立"本地源目录 → 多个云端目标目录"的绑定关系。自动扫描本地有而云端没有的文件，一键或自动通过 OpenList 服务端复制过去。支持跳过已存在/覆盖/中止三种冲突策略 |
| **传输列表** | 上传、下载、OpenList 复制任务统一展示，状态直接引用 OpenList 服务端任务。支持暂停、取消、清除已结束、全部重试，分页展示（默认 100 条/页） |
| **设置** | OpenList 连接配置、默认路径、自动扫描（每隔 N 天在指定时间点全量扫描，默认 7 天凌晨 3 点）、扫描并发限制、日志查看、赞助入口 |

核心设计：
- **服务端复制**：同步备份的上传走 `POST /api/fs/copy`，OpenList 在两个存储之间直接复制，NAS 不中转文件流。
- **增量扫描**：只找本地有、云端没有的文件，不重复传已存在的。
- **安全**：删除操作必须二次确认；应用以 root 运行但本地浏览有路径白名单；所有删除写审计日志。

---

## 二、安装

1. 下载 `cloud-sync-<版本>.fpk`。
2. 飞牛 **应用中心 → 手动安装** → 上传该文件（旧版本直接覆盖升级）。
3. 桌面出现「云端互传」图标，点开即用。

首次使用：在设置页填 OpenList 地址（如 `http://192.168.0.10:5244`）、用户名、密码，点连接。
密码只用于换 token，应用不存明文。

---

## 三、日常使用

### 同步备份（核心功能）

1. 在「同步备份」页点「新建同步」。
2. 选本地源目录（OpenList 上的本地存储路径），选一个或多个云端目标目录。
3. 冲突策略默认「跳过已存在」（云端有同名文件就跳过，不重复传）。
4. 保存后点「扫描」：扫描本地与云端的差异，列出缺失文件。
5. 点「一键上传」把缺失文件复制到所有目标；或开「自动上传」让扫描后自动传。
6. 也可设置「自动扫描」：每隔 N 天（默认 7 天）在指定时间点自动跑一次全量扫描。

> **上传进行中扫描会被禁止**：扫描会把正在复制的文件误判成缺失导致重复上传。
> 上传/提交期间扫描按钮自动禁用，等待上传完成后恢复。

### 文件对比

在左右两栏各选一个目录，点「对比」查看差异。点「上传」把左侧独有文件复制到右侧。
上传前有两步确认。

### 传输列表

直接展示 OpenList 服务端正在运行的任务（复制中、排队中、成功、失败）。
- 「全部暂停」「全部取消」「清除已结束」会转发到 OpenList 原生任务接口。
- 已完成任务保留最多 3000 条，可切换每页 50/100/200/500 条。

---

## 四、开发者

```
cloud-sync/
├── manifest                 飞牛应用清单（appname / version / platform）
├── ICON.PNG / ICON_256.PNG  桌面图标
├── config/                  privilege（run-as: root）、resource
├── cmd/                     生命周期脚本（必须 LF、无 BOM、755 可执行位）
├── wizard/uninstall        卸载向导（JSON 数组，无后缀）
├── app/
│   ├── ui/                  桌面入口 config 与图标（icon_64.png / icon_256.png）
│   ├── www/index.html       前端单文件（HTML+CSS+JS 全内联，无构建步骤）
│   └── server/              后端（纯 Python 标准库）
│       ├── main.py          入口（Unix Socket 网关，开发时 --tcp 直连）
│       ├── http_api.py      所有 HTTP 路由与 handler
│       ├── openlist.py       OpenList API 客户端（所有网络调用收敛于此）
│       ├── httpclient.py     底层 HTTP 传输（urllib）
│       ├── config.py        配置读写、版本号 APP_VERSION
│       ├── bindings.py      同步备份核心（绑定 CRUD、扫描、上传、定时器）
│       ├── verify.py          目录双向比对
│       ├── upload_queue.py   本地文件上传队列
│       ├── download_queue.py 下载队列
│       ├── transfer_records.py 传输记录（本地复制 / 同存储云端复制的回执）
│       └── hasher.py         SHA-256（能力探测，飞牛 Python 无 hashlib 时降级）
├── docs/
│   ├── 接手指南.md           ← 开发者必读：代码地图、路由表、坑清单
│   ├── 交接说明.md           工程导览、开发铁律、测试与打包
│   ├── API参考.md           OpenList v4.2.6 服务端接口契约
│   └── 需求与设计.md         原始需求与设计决策
├── tests/                   单元测试 + jsdom 前端测试
└── tools/
    ├── build_fpk.py         打包 fpk（含 52 项自检）
    ├── pack_src.py           打源码交接包
    ├── mutcheck.py           变异测试（反向验证测试真能变红）
    └── fnpack.exe            飞牛官方打包器（Windows 版）
```

### 常用命令

```bash
# 全部测试（Python 单测 + jsdom 前端）
python tests/run_all.py

# 本地起服务调试（不装 NAS）
python app/server/main.py --tcp 127.0.0.1:8380
# 浏览器打开 http://127.0.0.1:8380/

# 打包 fpk
python tools/build_fpk.py --project . --fnpack tools/fnpack.exe

# 打源码交接包
python tools/pack_src.py --out dist/cloud-sync-<版本>-src.zip
```

**改代码后必须做**：
1. 跑 `python tests/run_all.py` 全绿
2. 版本号迭代两处：`app/server/config.py` 的 `APP_VERSION` + `manifest` 的 `version`
3. 重新打包 fpk

### 关键约束

- 飞牛 Python 是裁剪构建，**不能顶层硬导入依赖 C 扩展的标准库**（`lzma`/`bz2`/`gzip`/`zlib`/`zipfile`/`sqlite3`/`ssl`/`ctypes` 等），需要时做能力探测 + 降级。
- `cmd/*` 脚本必须 LF 换行、无 BOM、有可执行位（用 build_fpk.py 打包会自动补权限）。
- OpenList copy 任务不返回进度字段（progress/size 全为 0），前端显示无限循环动画，不要猜百分比。
- 应用仓库：https://github.com/ffvz7850/cloud-sync

---

## 五、许可

MIT。开发者 / 发布者：**ffvz**。
