# -*- coding: utf-8 -*-
"""应用入口。

只起一类监听：统一网关的 Unix Socket（飞牛桌面把 `/app/cloud-sync/**` 转发进来），
本机开发时也可退到 `--tcp` 端口直连。上传队列随应用启动，并自动恢复上次未完成的任务。
"""

import argparse
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config          # noqa: E402
import http_api        # noqa: E402


class Logger(object):
    """按大小轮转的日志写入器。

    早先是"每行 open/append/flush"，既不轮转也不设上限：NAS 上常驻几周，
    info.log 会涨到几十上百 MB（扫描/上传重试都在写），而日志面板还要反复读它。
    这里加 5 MB × 3 份的轮转，超限时把 .1→.2、.2→.3 后换新文件。
    """

    MAX_BYTES = 5 * 1024 * 1024
    KEEP = 3

    def __init__(self, path, max_bytes=None, keep=None):
        self.path = path
        self._lock = threading.Lock()
        self.max_bytes = int(max_bytes or self.MAX_BYTES)
        self.keep = max(1, int(keep or self.KEEP))

    def _rotate(self):
        for i in range(self.keep - 1, 0, -1):
            src = "%s.%d" % (self.path, i)
            dst = "%s.%d" % (self.path, i + 1)
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def __call__(self, message):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s\n" % (stamp, message)
        payload = line.encode("utf-8")
        with self._lock:
            try:
                if os.path.exists(self.path) and \
                        os.path.getsize(self.path) + len(payload) > self.max_bytes:
                    self._rotate()
                with open(self.path, "ab") as fh:
                    fh.write(payload)       # 直接写 UTF-8 字节，避免编码回落
                    fh.flush()
            except Exception:
                pass
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass


def build_context():
    ctx = http_api.AppContext(config.APP_NAME)
    log = Logger(config.log_path())
    ctx.log = log
    http_api.set_app_context(ctx, log)
    return ctx, log


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cloud-sync")
    parser.add_argument("action", nargs="?", default="run",
                        choices=["run", "selftest", "show-config"])
    parser.add_argument("--socket", default="", help="统一网关 socket 路径")
    parser.add_argument("--tcp", default="", help="本机调试用，形如 127.0.0.1:8899")
    args = parser.parse_args(argv)

    ctx, log = build_context()
    log("=" * 60)
    log("%s v%s 启动（data=%s, inside_trim=%s）"
        % (config.APP_NAME, config.APP_VERSION, config.data_dir(), config.is_inside_trim()))

    if args.action == "show-config":
        import json
        print(json.dumps(config.load_config(), ensure_ascii=False, indent=2))
        print("settings.conf:", config.settings_path())
        return 0

    if args.action == "selftest":
        return run_selftest(ctx, log)

    servers = []

    socket_path = args.socket
    if not socket_path and config.is_inside_trim():
        socket_path = os.path.join(config.app_dest(), "app.sock")

    if args.tcp:
        host, _, port = args.tcp.partition(":")
        server = http_api.create_tcp_server(host or "127.0.0.1", int(port or 8899), ctx)
        servers.append(server)
        log("调试端口监听 http://%s:%s/" % (host or "127.0.0.1", port or 8899))

    if socket_path:
        server = http_api.create_unix_server(socket_path, ctx)
        if server:
            servers.append(server)
            log("统一网关 socket 已监听：%s" % socket_path)
        elif getattr(ctx, "single_instance_conflict", False):
            # 另一个实例已经在跑。此时**绝不能**继续起队列：两个进程会用同一份
            # queue.json / bindings.json 互相覆盖。
            log("已有实例在运行，本进程退出")
            return 3
        else:
            log("当前平台不支持 Unix Socket，跳过网关监听")

    if not servers:
        log("没有任何监听成功，退出")
        return 1

    # 启动上传 / 下载队列（都会自动恢复上次未完成的任务）
    try:
        ctx.queue()
        ctx.downloads()
        log("上传与下载队列已启动")
    except Exception as exc:
        log("队列启动失败：%s" % exc)

    stopping = threading.Event()

    def handle_signal(signum, _frame):
        log("收到信号 %s，准备退出" % signum)
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle_signal)
        except Exception:
            pass

    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    log("启动完成，等待请求")
    try:
        while not stopping.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    log("正在关闭…")
    for attr in ("_queue", "_downloads"):
        try:
            queue = getattr(ctx, attr, None)
            if queue is not None:
                queue.stop()
        except Exception:
            pass
    for server in servers:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
    log("已退出")
    return 0


def run_selftest(ctx, log):
    """`main selftest` —— 一键体检，输出人可读结论。"""
    import hasher
    import httpclient
    ok = True
    print("数据目录：", config.data_dir())

    backend, note = httpclient.detect_backend()
    print("网络传输：", backend, "-", note)

    digest = hasher.sha256_hex("abc")
    hash_ok = digest.startswith("ba7816bf")
    print("SHA-256 自检（%s）：%s" % (hasher.backend(), "通过" if hash_ok else "失败"))
    ok = ok and hash_ok

    store = ctx.store()
    status = store.status()
    print("OpenList 连接：", "已配置（%s）" % status.get("baseUrl") if status["configured"] else "未配置")
    if status["configured"]:
        try:
            result = ctx.client().diagnose()
            for step in result["steps"]:
                print("  - %s：%s %s" % (step["name"], "OK" if step["ok"] else "失败", step["detail"]))
            ok = ok and result["ok"]
        except Exception as exc:
            print("  连接测试失败：", exc)
            ok = False

    print("结论：", "全部通过" if ok else "存在问题，请按上面提示处理")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
