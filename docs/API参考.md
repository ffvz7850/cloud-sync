# OpenList API 参考（本应用唯一事实来源）

> 来源：OpenList **v4.2.6** server 端源码（`server/handles/*.go`、`server/router.go`、`server/middlewares/auth.go`、`internal/model/user.go`），并对用户 NAS（`192.168.0.10:5244`）的匿名端点做了实测。
> 所有业务接口都在 `{base}/api` 下。`base` 默认 `http://192.168.0.10:5244`。

实测记录：

| 端点 | 结果 | 说明 |
|---|---|---|
| `GET /api/public/settings` | 200 | `site_title=openlist`、`version=v4.2.6` |
| `POST /api/fs/list`（无 token） | 401 | `Guest user is disabled, login please` |
| `PROPFIND /dav`（无 token） | 401 | WebDAV 端点存在但需鉴权（本应用不用） |

---

## 1. 认证

### 1.1 密码哈希（StaticHash）

客户端**永远不发明文密码**。先用固定盐做一次 SHA-256：

```
staticHash = sha256_hex( password + "-" + "https://github.com/alist-org/alist" )
```

注意盐里是 `alist`（OpenList 为兼容沿用旧盐，**不是** `openlist`）。
`sha256_hex` 指小写十六进制字符串（`utils.HashData` 用 `hex.EncodeToString`）。

### 1.2 登录

`POST /api/auth/login/hash`

```json
{ "username": "alice", "password": "<staticHash>", "otp_code": "" }
```

成功 `200`：

```json
{ "code": 200, "message": "success", "data": { "token": "<jwt>" } }
```

失败（HTTP 状态与 `code` 一致）：

| code | 含义 |
|---|---|
| 401 | 用户名或密码错误 |
| 402 | 需要 2FA（`otp_code` 缺失或错误） |
| 429 | 失败次数过多，锁定 5 分钟（默认单 IP 最多 5 次） |

> 还有一个 `POST /api/auth/login`（已废弃），它会在服务端再套一层 `StaticHash`，等价于客户端传**明文**。**本应用统一用 `/auth/login/hash`**。

### 1.3 token 用法

后续每个请求带 header：

```
Authorization: <token>
```

**裸 token，没有 `Bearer ` 前缀**。中间件先用它和后台「其他设置」里的全局 admin token 比对，不一致再按 JWT 解析。

token 失效场景（都返回 401，客户端应自动重新登录）：

- JWT 过期；
- 用户在别处改了密码（token 里的 `PwdTS` 与服务端不一致）→ `Password has been changed`；
- 用户被禁用 → `Current user is disabled`。

### 1.4 当前用户

`GET /api/me` → `{ data: { id, username, base_path, role, permission, disabled, ... } }`

`permission` 位掩码里与本应用相关的位：

| bit | 含义 |
|---|---|
| 3 | 可建目录、可上传（**上传必需**） |
| 4 | 重命名 |
| 7 | 删除 |
| 8/9 | WebDAV 读 / 写（本应用不用） |

---

## 2. 目录与文件

### 2.1 列目录

`POST /api/fs/list`

```json
{ "path": "/movies", "password": "", "refresh": false, "page": 1, "per_page": 200 }
```

成功 `200` → `data`：

```json
{
  "content": [
    { "name": "a.mkv", "size": 123, "is_dir": false,
      "modified": "2026-09-01T10:00:00+08:00", "created": "...",
      "sign": "", "thumb": "", "type": 0,
      "hashinfo": "{}", "hash_info": {} }
  ],
  "total": 1, "readme": "", "header": "",
  "write": true, "provider": "local"
}
```

要点：

- **分页在服务端对「已列出的全部条目」切片**，不是驱动层分页。`per_page` 必须 `>0`，否则 `pagination()` 会切出空页。客户端应翻页直到拿够 `total`。
- `refresh: true` 会强制刷新缓存，但需要写权限（`CanWriteContent`），否则 403。
- 路径受 `base_path` 限制，越界 403。

### 2.2 获取单个文件/目录

`POST /api/fs/get` → body `{ "path": "/movies/a.mkv", "password": "" }`

`data` 在 2.1 条目结构基础上多：

```json
{ "raw_url": "http://.../p/movies/a.mkv?sign=...", "provider": "local", "related": [] }
```

`raw_url` 是可直接下载的地址（`GET /d<path>` 或代理 `/p<path>`）。

---

## 3. 上传

### 3.1 同步直传（中小文件）

`PUT /api/fs/put`

请求头：

| header | 说明 |
|---|---|
| `Authorization` | token |
| `File-Path` | **percent-encode 后的完整目标路径**（含文件名），服务端 `url.PathUnescape` 还原 |
| `Content-Length` | 文件字节数（或 `X-File-Size`） |
| `Overwrite` | 缺省/`true` 覆盖；传 `"false"` 且目标已存在 → 403 `file exists` |
| `Last-Modified` | 毫秒时间戳，写为云端 mtime（缺省取当前时间） |
| `Content-Type` | MIME，缺省按扩展名猜 |
| `X-File-Md5` / `X-File-Sha1` / `X-File-Sha256` | 可选，写入 `hash_info`，便于秒传/校验 |

body 为文件原始字节。成功 `200` → `{ "code":200, "data":null }`（同步等驱动写完才返回）。

### 3.2 分片上传（大文件 / 断点续传）

路由：`/api/fs/multipart/*`。需服务端 `MultipartEnabled` 开启（否则 init 返回 403 `multipart upload is disabled`，**客户端要能降级到 3.1**）。分片会话 TTL 默认 24h。

**① 初始化** `POST /api/fs/multipart/init`

请求头：`File-Path`、`X-File-Size`（必填，>0）、`X-Chunk-Size`（可选，≤服务端上限、≥1MB）、`Overwrite?`、`Last-Modified?`、`X-File-*` 哈希。

```json
{ "code":200, "data": { "upload_id": "...", "chunk_size": 10485760,
                        "uploaded": [0,1], "resumed": true, "...": "..." } }
```

`resumed: true` 表示这是**找回的旧会话**（同 path+size），`uploaded` 是已完成的分片序号——断点续传的关键。

**② 上传分片** `PUT /api/fs/multipart/chunk`

请求头：`X-Upload-Id`、`X-Chunk-Index`（从 0 开始）。body 为该分片字节。

- 分片**幂等**：同一片可重复传，服务端去重；
- 可并发、可乱序（在接收窗口内）；
- `429` = 接收窗口满（流控，稍候重试）；`409` = 该分片正在别的连接上传。

**③ 完成** `POST /api/fs/multipart/complete`（header `X-Upload-Id`）
等服务端把分片合并并写入驱动，返回最终 `SessionSnapshot`。

**④ 查询状态** `GET /api/fs/multipart/status?upload_id=<id>`
或 `GET /api/fs/multipart/status?path=<p>&size=<n>` —— 客户端崩溃重启后，用 **path+size** 找回可恢复的会话。

**⑤ 取消** `POST /api/fs/multipart/abort`（header `X-Upload-Id`）

### 3.3 服务端后台任务（可选）

`PUT /api/fs/put` 加 header `As-Task: true` → 立即返回 `{ "data": { "task": {TaskInfo} } }`，文件由服务端后台落盘。进度走任务接口轮询（见 §5）。

---

## 4. 目录 / 文件管理

| 接口 | body |
|---|---|
| `POST /api/fs/mkdir` | `{ "path": "/a/b" }`（逐级不存在会报错，客户端要逐层建） |
| `POST /api/fs/rename` | `{ "path": "/a/old", "name": "new" }` |
| `POST /api/fs/move` | `{ "src_dir": "/a", "dst_dir": "/b", "names": ["x"] }` |
| `POST /api/fs/copy` | 同 move |
| `POST /api/fs/remove` | `{ "dir": "/a", "names": ["x"] }`（是否进回收站取决于驱动） |

---

## 5. 后台任务查询

> **重要：端点必须带任务类型段。** 正确的路径形状是 `/api/task/<type>/<action>`，
> `<type>` 为 `upload`（上传）或 `copy`（复制，目录绑定备份用到）。
> **不存在 `GET /api/task`，也不存在 `GET /api/task/<id>`** —— 这两个是 v2.8.4 修过的真实缺陷：
> 当年 `bindings.py` 按这两个路径去查复制任务，请求一律 404，而代码把"查不到"当成了
> "任务已完成"，于是备份功能会**谎报成功**。写这块代码时务必先核对本节的端点。

### 5.1 上传任务

`GET  /api/task/upload/undone` → `{ data: [TaskInfo] }`（pending/running/重试中）
`GET  /api/task/upload/done` → `{ data: [TaskInfo] }`（succeeded/failed/canceled）
`POST /api/task/upload/info?tid=<id>`
`POST /api/task/upload/cancel?tid=<id>` / `delete` / `retry`
`POST /api/task/upload/retry_failed` / `clear_done`

### 5.2 复制任务（`/api/fs/copy` 跨存储时产生）

`GET  /api/task/copy/undone` → `{ data: [TaskInfo] }`
`GET  /api/task/copy/done`   → `{ data: [TaskInfo] }`
`POST /api/task/copy/info?tid=<id>`（注意是 **POST + 查询串**，不是 `GET /api/task/<id>`）

- `data` 是 **TaskInfo 数组**，不是字典；`done` 里同时混有成功与失败任务，要靠 `state` 区分。
- 跨存储时 `POST /api/fs/copy` 的响应里会带 `tasks` 数组（提交即返回，服务端异步执行）。
  应用只在 `len(data["tasks"]) == len(names)` 时才认领这些 `tid` —— 数量对不上就说明这批
  任务不全是本次提交产生的，硬认领会把别人的任务当成自己的。

`TaskInfo`：`{ id, name, creator, state, status, progress(0-100), total_bytes, error, start_time, end_time }`

`state` 取值（`pkg/tache/state.go`）：`0=pending 1=running 2=succeeded 3=canceling
4=canceled 5=errored 6=failing 7=failed(重试次数耗尽) 8=waiting_retry`。
判定"成功"只认 **2**；`4` 与 `7` 都算失败（4 是取消，7 是重试耗尽）。

> 本应用**自己实现客户端侧任务队列**（进度、自动重试、手动重传、断点续传都在本地做），
> 服务端任务接口只用于：①目录绑定备份的复制任务；②`As-Task` 模式的上传。
> 查询失败（网络/鉴权问题）必须与"确实没有任务"区分开：前者返回 `None` 由调用方保持原状态并重试，
> **绝不能当成任务已完成**。

---

## 6. 错误约定

统一响应壳：

```json
{ "code": <int>, "message": "<str>", "data": <any|null> }
```

- `code == 200` 为成功；其余为失败，`message` 是原因。
- HTTP 状态码与 `code` 一致（401/403/404/429/500…）。
- 客户端据此分类：`401` → 重新登录；`429` → 退避重试；`403 file exists` → 业务冲突；其余 → 业务错误。

---

## 7. 本应用的选型结论

1. **登录**：`/auth/login/hash` + StaticHash；token 存服务端，401 自动重登。
2. **小文件**（< 分片阈值）：`fs/put` 同步直传，进度靠客户端分块发送统计。
3. **大文件**：优先 `multipart` 分片（断点续传），init 报 403 自动降级到 `fs/put`。
4. **完整性校验**：上传后 `fs/get` 比对 `size`（+ 若驱动回 `hash_info` 则比对哈希）。
5. **不做**：WebDAV（用 OpenList 自带的）、客户端不需要的任务接口（自己实现队列）。
