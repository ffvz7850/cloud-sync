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
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def __call__(self, message):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s\n" % (stamp, message)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
            except Exception:
                pass
        sys.stderr.write(line)
        sys.stderr.flush()


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
