from __future__ import annotations
import argparse
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from src.http_api import make_handler
from src.repository import Repository
from src.service import Service
def start_reaper(service: Service, interval: float) -> None:
    def loop():
        while True:
            time.sleep(interval)
            try:
                service.sweep_expired_tasks()
            except Exception:
                pass
    threading.Thread(target=loop, name="task-lease-reaper", daemon=True).start()
def parse_args():
    parser=argparse.ArgumentParser(description='职业辐射剂量与异常事件')
    parser.add_argument("--db",default="./data.db",help="SQLite数据库路径")
    parser.add_argument("--port",type=int,default=8312,help="HTTP端口")
    parser.add_argument("--host",default="127.0.0.1",help="监听地址")
    parser.add_argument("--lease-seconds",type=int,default=1800,help="接办租约时长（秒）")
    return parser.parse_args()
def main():
    args=parse_args(); repository=Repository(args.db); service=Service(repository,lease_seconds=args.lease_seconds)
    start_reaper(service, max(30.0, service.lease_seconds/2))
    server=ThreadingHTTPServer((args.host,args.port),make_handler(service,str(Path(__file__).resolve().parent/"static")))
    print(f"listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); repository.close()
if __name__=="__main__": main()
