from __future__ import annotations

import argparse
from pathlib import Path
import socket

from futu_quant.operational_readiness import audit_streamlit_pages


def main() -> None:
    parser = argparse.ArgumentParser(description="检查全部 Streamlit 页面兼容性")
    parser.add_argument("--runtime", action="store_true", help="通过主入口逐页实际渲染（只读模拟盘）")
    args = parser.parse_args()
    result = audit_streamlit_pages()
    print(f"静态巡检：{result['pages_checked']} 页，错误 {result['errors']}，警告 {result['warnings']}")
    for issue in result["issues"]:
        print(f"{issue['级别']} {issue['页面']}:{issue['行号']} {issue['问题']}")
    failures = result["errors"]
    if args.runtime:
        with socket.socket() as connection:
            connection.settimeout(1)
            if connection.connect_ex(("127.0.0.1", 11111)) != 0:
                print("运行巡检跳过：富途 OpenD 127.0.0.1:11111 当前不可连接；静态巡检已完成。")
                raise SystemExit(2)
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file("app.py").run(timeout=45)
        for exception in app.exception:
            print(f"运行错误 app.py: {exception.message}")
            failures += 1
        for page in sorted(Path("pages").glob("*.py")):
            app.switch_page(str(page)).run(timeout=45)
            if app.exception:
                for exception in app.exception:
                    print(f"运行错误 {page}: {exception.message}")
                    failures += 1
            else:
                print(f"运行通过 {page}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
