"""全局测试隔离与沙箱临时目录兼容。

生产 runtime_config（data/trading.db）里的动态设置会被
`Settings.load(refresh=True)` 折进 os.environ（见 config.refresh_runtime_environment），
monkeypatch 只还原它自己设置的键，于是 EXECUTION_MODE=REAL、REAL_ACCOUNT_ID 等
生产配置会泄漏给同进程后续测试，造成跨用例污染。

这里在每条用例前后对整个 os.environ 做快照/还原，同时把 config.ENV_FILE 指向
一个空文件，彻底切断测试进程与生产 .env / 生产 SQLite 的联系。
"""

from __future__ import annotations

import logging.handlers
import os
import pathlib
import tempfile

# 统一重定向 tempfile 根目录至工作区内部，杜绝在系统 Temp 目录下创建 sqlite 数据库导致的 WinError 5 沙箱拦截
_qa_dir = pathlib.Path("data/qa").resolve()
_qa_dir.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_qa_dir)
os.environ["TMP"] = str(_qa_dir)
os.environ["TEMP"] = str(_qa_dir)
os.environ["TMPDIR"] = str(_qa_dir)

# Windows 下若在 mkdir 显式传入 0o700/0o600 等 UNIX 权限，Python 底层会生成受限的 DACL 安全描述符，
# 剥离沙箱令牌访问权，导致后续 sqlite3 无法创建数据库文件。在此去除显式 mode，统一继承父目录完整 ACL。
_orig_path_mkdir = pathlib.Path.mkdir
def _safe_path_mkdir(self, mode=None, parents=False, exist_ok=False):
    return _orig_path_mkdir(self, parents=parents, exist_ok=exist_ok)
pathlib.Path.mkdir = _safe_path_mkdir

_orig_os_mkdir = os.mkdir
def _safe_os_mkdir(path, mode=None, *args, **kwargs):
    return _orig_os_mkdir(path, *args, **kwargs)
os.mkdir = _safe_os_mkdir

# tempfile 内部使用 `import os as _os`，直接修补其内部私有引用
_orig_tempfile_os_mkdir = getattr(tempfile, "_os", os).mkdir
def _safe_tempfile_os_mkdir(path, mode=None, *args, **kwargs):
    return _orig_tempfile_os_mkdir(path, *args, **kwargs)
if hasattr(tempfile, "_os"):
    tempfile._os.mkdir = _safe_tempfile_os_mkdir
if hasattr(tempfile, "_resetperms"):
    tempfile._resetperms = lambda path: None

# Windows 默认以 GBK 解码文件，测试读取带有中文注释的代码时会抛 UnicodeDecodeError，统一默认 UTF-8
_orig_read_text = pathlib.Path.read_text
def _safe_read_text(self, encoding=None, errors=None):
    if encoding is None:
        encoding = "utf-8"
    return _orig_read_text(self, encoding=encoding, errors=errors)
pathlib.Path.read_text = _safe_read_text

# futu 库 import 时默认写 C:\Users\Administrator\AppData\Roaming\com.futunn.FutuOpenD\Log，
# 在 workspace-write 沙箱下会因越权写抛出 PermissionError，在此安全重定向至工作区内。
_orig_trfh_init = logging.handlers.TimedRotatingFileHandler.__init__
def _safe_trfh_init(self, filename, *args, **kwargs):
    s_fn = str(filename)
    if "com.futunn.FutuOpenD" in s_fn or ("AppData" in s_fn and "Log" in s_fn):
        filename = str(_qa_dir / "futu_test.log")
    return _orig_trfh_init(self, filename, *args, **kwargs)
logging.handlers.TimedRotatingFileHandler.__init__ = _safe_trfh_init

import pytest  # noqa: E402  # 必须在上述 monkeypatch 之后导入：先装好兼容补丁再进入 pytest 装载流程

_SNAPSHOT_KEYS: tuple[str, ...] = tuple(sorted(os.environ.keys()))
_SNAPSHOT_VALUES: dict[str, str] = dict(os.environ)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch, tmp_path):
    # 每条用例前：ENV_FILE 指向空临时文件，生产 .env / 生产 runtime_config 不再进入进程
    env_file = tmp_path / "isolation.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("futu_quant.config.ENV_FILE", env_file)

    yield

    # 每条用例后：恢复完整 os.environ 快照，保留 pytest 自身控制变量
    for key in list(os.environ.keys()):
        if key not in _SNAPSHOT_KEYS and not key.startswith("PYTEST_"):
            del os.environ[key]
    for key, value in _SNAPSHOT_VALUES.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
