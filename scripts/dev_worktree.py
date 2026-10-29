"""三段式分支流程工具：dev 工作树 -> 测试区全量回归 -> master 合入。

用法::

    python scripts/dev_worktree.py new iv-cap-semantics --kind fix
    python scripts/dev_worktree.py status
    python scripts/dev_worktree.py verify --ref fix/iv-cap-semantics
    python scripts/dev_worktree.py merge fix/iv-cap-semantics
    python scripts/dev_worktree.py release fix/iv-cap-semantics

为什么要有这个脚本：流程是「trade-dev 里开发 → trade-test 全量回归绿 → 合入
trade 的 master」，三步全靠手工，少一步就漏一环（2026-10-29 的 fix/iv-percentile
就是这样跳过 test 直达 master 的）。本脚本把三件事变成命令，并且**用验证戳强制
顺序**：`verify` 通过后把被验证的 commit sha 写进 `trade-test/data/qa/verify_stamp.json`，
`merge` 发现 master 上没有这个 sha 的绿戳就拒绝合入。

硬约束：

* 本脚本**从不在生产根 checkout / 改文件**，只允许 `merge --ff-only` 与 `push`；
* `verify` 一定带 `TRADEYI_PROD_DB_PATH` 指向真生产库 —— 不带的话 `conftest` 的
  指纹守卫会拿测试库和自己比，跑出假绿（见 docs/test_environment_runtime.md §5）；
* 只做 ff 合入，历史保持线性；分支落后 master 时提示 rebase，不自动 merge commit。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PRODUCTION_ROOT = Path(os.environ.get("TRADEYI_PROD_ROOT", r"E:\Thisyi\trade")).resolve()
DEV_ROOT = Path(os.environ.get("TRADEYI_DEV_ROOT", r"E:\Thisyi\trade-dev")).resolve()
TEST_ROOT = Path(os.environ.get("TRADEYI_TEST_ROOT", r"E:\Thisyi\trade-test")).resolve()

PROD_DB = PRODUCTION_ROOT / "data" / "trading.db"
STAMP_PATH = TEST_ROOT / "data" / "qa" / "verify_stamp.json"
TEST_PYTHON = TEST_ROOT / ".venv" / "Scripts" / "python.exe"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
KINDS = ("fix", "feat")


# --------------------------------------------------------------------------- 纯函数（可单测）

def slug_valid(slug: str) -> bool:
    """工作树 slug：小写字母数字与连字符，不以连字符开头。"""
    return bool(SLUG_RE.match(slug or ""))


def branch_for(slug: str, kind: str) -> str:
    """`fix` + `iv-cap` -> `fix/iv-cap`；已带前缀时原样返回。"""
    if "/" in slug:
        return slug
    if kind not in KINDS:
        raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {kind!r}")
    return f"{kind}/{slug}"


def worktree_path(slug: str) -> Path:
    """分支 slug 到开发工作树路径；带前缀的分支取末段。"""
    name = slug.rstrip("/").split("/")[-1]
    return DEV_ROOT / f"wt-{name}"


def _writable_dir(path: Path) -> bool:
    """目录能否真正建文件：tempfile.gettempdir() 只认「能创建成功」的候选。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".dw_probe_{os.getpid()}"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def pick_tempdir(candidates: list[Path] | None = None) -> Path | None:
    """按优先级挑一个真正可写的临时目录，全不可用则返回 None（交给系统默认）。"""
    for path in candidates or []:
        if path and _writable_dir(path):
            return path
    return None


def verify_env(test_root: Path | None = None, prod_db: Path | None = None) -> dict[str, str]:
    """测试区跑全量回归的环境：标记 test、独立 DB/TEMP、显式指真生产库。"""
    base = (test_root or TEST_ROOT).resolve()
    db = (prod_db or PROD_DB).resolve()
    qa = base / "data" / "qa"
    env = dict(os.environ)
    env["TRADEYI_ENV"] = "test"
    env["TRADEYI_TEST_ROOT"] = str(base)
    env["TRADEYI_DB_PATH"] = str(base / "data" / "trading.db")
    # 关键：不带这一项，指纹守卫会拿测试库与自身比对，假绿。
    env["TRADEYI_PROD_DB_PATH"] = str(db)
    env["PYTHONPATH"] = str(base / "src")

    # TEMP/TMP：优先测试区自己的 qa 目录，但它必须真的可写（受限环境/杀软会拦），
    # 否则 pytest 的 capture 会在收集阶段直接崩在 tempfile.gettempdir()。
    tempdir = pick_tempdir([qa, Path(os.environ.get("TEMP", "")) if os.environ.get("TEMP") else None])
    if tempdir is not None:
        env["TEMP"] = str(tempdir)
        env["TMP"] = str(tempdir)
    return env


def stamp_matches(stamp: dict | None, sha: str) -> bool:
    """绿戳是否覆盖指定提交（按较短一方做前缀比较，至少 7 位，容忍短 sha）。"""
    if not isinstance(stamp, dict) or not stamp.get("ok"):
        return False
    recorded = str(stamp.get("sha") or "").strip()
    target = str(sha or "").strip()
    width = min(len(recorded), len(target))
    return width >= 7 and recorded[:width] == target[:width]


def pytest_summary(output: str) -> str:
    """取 pytest 的收尾统计行（形如 `1710 passed in 312.40s`）。

    只认带耗时的收尾行：失败明细里也会出现 `2 failed` 这种孤立行，不能当统计用。
    """
    for line in reversed((output or "").splitlines()):
        text = line.strip()
        if re.search(r"\b\d+ (passed|failed|error|errors|skipped|xfailed|deselected)\b", text) and re.search(
            r"\bin \d+(\.\d+)?s\b", text
        ):
            return text
    return ""


# --------------------------------------------------------------------------- git 封装

def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"[git {' '.join(args)}] 失败于 {cwd}:\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def git_out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


def head_sha(cwd: Path, ref: str = "HEAD") -> str:
    return git_out(cwd, "rev-parse", ref)


def current_branch(cwd: Path) -> str:
    return git_out(cwd, "branch", "--show-current")


def dirty_tracked(cwd: Path) -> list[str]:
    """已跟踪文件的未提交改动（未跟踪的临时目录不算，例如 .pytest-tmp）。"""
    lines = git_out(cwd, "status", "--porcelain").splitlines()
    return [line for line in lines if line and not line.startswith("??")]


def is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    return git(cwd, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def branch_exists(cwd: Path, branch: str) -> bool:
    return git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


# --------------------------------------------------------------------------- 绿戳

def read_stamp() -> dict | None:
    try:
        return json.loads(STAMP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_stamp(sha: str, ref: str, summary: str, duration_s: float) -> None:
    STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    STAMP_PATH.write_text(
        json.dumps(
            {
                "sha": sha,
                "ref": ref,
                "ok": True,
                "summary": summary,
                "duration_s": round(duration_s, 1),
                "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "test_root": str(TEST_ROOT),
                "prod_db": str(PROD_DB),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- 命令

def cmd_new(args: argparse.Namespace) -> int:
    branch = branch_for(args.slug, args.kind)
    path = worktree_path(branch)
    if branch_exists(PRODUCTION_ROOT, branch):
        print(f"拒绝：分支 {branch} 已存在。换个 slug，或直接用现有工作树继续。")
        return 2
    if path.exists():
        print(f"拒绝：目录已存在 {path}")
        return 2
    DEV_ROOT.mkdir(parents=True, exist_ok=True)
    git(PRODUCTION_ROOT, "worktree", "add", str(path), "-b", branch, args.base)
    print(f"已创建 {path}\n分支    {branch}（基于 {args.base} @ {head_sha(PRODUCTION_ROOT, args.base)[:8]}）")
    print("\n下一步：")
    print(f"  cd {path}")
    print("  # ... 开发与提交 ...")
    print(f"  python scripts/dev_worktree.py release {branch}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(f"生产 {PRODUCTION_ROOT}")
    print(f"  分支      {current_branch(PRODUCTION_ROOT)}")
    print(f"  未提交    {len(dirty_tracked(PRODUCTION_ROOT))} 个已跟踪文件")
    ahead_behind = git_out(PRODUCTION_ROOT, "rev-list", "--left-right", "--count", "origin/master...master")
    behind, ahead = (ahead_behind.split() + ["0", "0"])[:2]
    print(f"  远端      落后 {behind} / 领先 {ahead}")
    print(f"  HEAD      {head_sha(PRODUCTION_ROOT)[:8]}")

    print(f"\n测试区 {TEST_ROOT}")
    print(f"  分支      {current_branch(TEST_ROOT) or '(detached)'}")
    print(f"  HEAD      {head_sha(TEST_ROOT)[:8]}")
    print(f"  未提交    {len(dirty_tracked(TEST_ROOT))} 个已跟踪文件")
    stamp = read_stamp()
    if stamp:
        print(f"  绿戳      {stamp.get('sha', '')[:8]} @ {stamp.get('verified_at', '?')} | {stamp.get('summary', '')}")
    else:
        print("  绿戳      (无)")

    print("\n开发工作树")
    raw = git_out(PRODUCTION_ROOT, "worktree", "list", "--porcelain")
    entry: dict[str, str] = {}
    rows = []
    for line in raw.splitlines() + [""]:
        if not line.strip():
            if entry:
                rows.append(entry)
            entry = {}
            continue
        key, _, value = line.partition(" ")
        entry[key] = value
    for row in rows:
        path = Path(row.get("worktree", ""))
        if path.resolve() == PRODUCTION_ROOT or path.resolve() == TEST_ROOT:
            continue
        mark = "*" if path.exists() and dirty_tracked(path) else " "
        print(f" {mark} {path.name:<32} {row.get('branch', '').split('/')[-1] or '(detached)':<40} {row.get('HEAD', '')[:8]}")
    print("\n  * = 有未提交改动")
    return 0


def _sync_test(ref: str) -> str:
    """测试区切到 ref 对应的 verify 分支，返回目标 sha。失败即退出。"""
    sha = head_sha(PRODUCTION_ROOT, ref)
    if dirty_tracked(TEST_ROOT):
        raise SystemExit(
            f"拒绝：测试区 {TEST_ROOT} 有 {len(dirty_tracked(TEST_ROOT))} 个已跟踪文件未提交，"
            "先处理干净再验证（否则验的不是你想验的代码）"
        )
    name = ref.rstrip("/").split("/")[-1] or "head"
    verify_branch = f"verify/{name}"
    git(TEST_ROOT, "checkout", "-B", verify_branch, sha)

    # checkout 可能只更新 HEAD/index 而没把文件落到工作树（杀软/同步盘/受限权限下
    # 出现过：新增与被改文件全部显示为 D）。这里自愈一次，仍不干净就中止。
    if dirty_tracked(TEST_ROOT):
        stale = len(dirty_tracked(TEST_ROOT))
        git(TEST_ROOT, "checkout", "--", ".")
        remaining = dirty_tracked(TEST_ROOT)
        if remaining:
            raise SystemExit(
                f"拒绝：测试区切换后仍不干净（{remaining[:3]}...），工作树可能被占用或权限受限，"
                "请人工检查后再跑验证"
            )
        print(f"（已修复 {stale} 个未落地的文件）")
    print(f"测试区已切到 {verify_branch} -> {sha[:8]}")
    return sha


def _ensure_test_interpreter() -> None:
    """测试区对本解释器不可写时，改用测试区 venv 重跑自己。

    Windows「受控文件夹访问」/杀软可能只放行特定解释器：现象是生产 venv 的 python
    及其 git 子进程在 `trade-test` 里 `Permission denied`（连 checkout 都写不进去），
    而测试区自己的 venv 正常。与其让用户记住该用哪个 python，不如自动换过去。
    """
    if os.environ.get("DW_REEXEC") == "1":
        return
    if _writable_dir(TEST_ROOT / "data" / "qa"):
        return
    if not TEST_PYTHON.exists():
        return
    if Path(sys.executable).resolve() == TEST_PYTHON.resolve():
        return
    env = dict(os.environ)
    env["DW_REEXEC"] = "1"
    print(f"测试区对 {Path(sys.executable).name} 不可写，改用测试区解释器重跑：{TEST_PYTHON}")
    code = subprocess.run([str(TEST_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]], env=env).returncode
    raise SystemExit(code)


def cmd_sync_test(args: argparse.Namespace) -> int:
    _ensure_test_interpreter()
    _sync_test(args.ref)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    _ensure_test_interpreter()
    ref = args.ref
    sha = _sync_test(ref)
    python = TEST_PYTHON if TEST_PYTHON.exists() else Path(sys.executable)
    cmd = [str(python), "-m", "pytest", "-q", *(args.pytest_args or [])]
    print(f"跑全量回归：{' '.join(cmd)}")
    print(f"生产库指纹基准：{PROD_DB}")
    started = datetime.now()
    proc = subprocess.run(cmd, cwd=str(TEST_ROOT), env=verify_env(), text=True, encoding="utf-8", errors="replace",
                          capture_output=True)
    duration = (datetime.now() - started).total_seconds()
    output = (proc.stdout or "") + (proc.stderr or "")
    summary = pytest_summary(output)
    if proc.returncode == 0:
        write_stamp(sha, ref, summary, duration)
        print(f"\n全绿 {sha[:8]} | {summary} | 耗时 {duration / 60:.1f} 分钟")
        print(f"绿戳已写入 {STAMP_PATH}")
        return 0
    tail = "\n".join(output.splitlines()[-25:])
    print(f"\n回归失败（exit={proc.returncode}）{sha[:8]} | {summary or '无统计行'}")
    print(tail)
    return proc.returncode or 1


def cmd_merge(args: argparse.Namespace) -> int:
    branch = args.branch or current_branch(Path.cwd())
    if not branch or branch in ("master", "HEAD"):
        print("拒绝：请指定待合入的 fix/feat 分支（当前不在开发分支上）")
        return 2
    if current_branch(PRODUCTION_ROOT) != "master":
        print(f"拒绝：生产根不在 master（当前 {current_branch(PRODUCTION_ROOT)}）")
        return 2
    dirty = dirty_tracked(PRODUCTION_ROOT)
    if dirty:
        print(f"拒绝：生产根有 {len(dirty)} 个未提交改动，先清干净：")
        for line in dirty[:10]:
            print(f"  {line}")
        return 2
    if not branch_exists(PRODUCTION_ROOT, branch):
        print(f"拒绝：分支 {branch} 不存在")
        return 2
    sha = head_sha(PRODUCTION_ROOT, branch)
    if not is_ancestor(PRODUCTION_ROOT, "master", branch):
        print(f"拒绝：{branch} 落后于 master，无法 ff 合入。先在开发工作树 rebase master 再跑 verify。")
        return 2
    stamp = read_stamp()
    if not stamp_matches(stamp, sha) and not args.force:
        recorded = (stamp or {}).get("sha", "(无绿戳)")[:8]
        print(f"拒绝：{branch} 的 HEAD {sha[:8]} 没有测试区绿戳（现有戳 {recorded}）。")
        print(f"先跑：python scripts/dev_worktree.py verify --ref {branch}")
        print("（确认要跳过验证时加 --force，并在提交说明里写清原因）")
        return 2

    before = head_sha(PRODUCTION_ROOT)
    git(PRODUCTION_ROOT, "merge", "--ff-only", branch)
    after = head_sha(PRODUCTION_ROOT)
    print(f"已合入 master: {before[:8]} -> {after[:8]}（{branch}）")
    if args.no_push:
        print("按参数跳过推送")
        return 0
    push = git(PRODUCTION_ROOT, "push", "origin", "master", check=False)
    if push.returncode != 0:
        detail = (push.stderr or push.stdout).strip().splitlines()
        print(f"推送失败，本地已合入 {after[:8]}，未推送。原因：{detail[-1] if detail else '未知'}")
        print("可在能读凭据的终端里补一次：git -C E:\\Thisyi\\trade push origin master")
        return 3
    print("已推送 origin/master")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    verify_args = argparse.Namespace(ref=args.branch, pytest_args=args.pytest_args)
    code = cmd_verify(verify_args)
    if code != 0:
        print("\n验证未通过，未合入 master。")
        return code
    merge_args = argparse.Namespace(branch=args.branch, force=args.force, no_push=args.no_push)
    return cmd_merge(merge_args)


# --------------------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="三段式分支流程：dev 工作树 -> trade-test 全量回归 -> master 合入"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="在 trade-dev 下新建开发工作树")
    p_new.add_argument("slug", help="分支短名，例如 iv-cap（自动补 fix/ 前缀）")
    p_new.add_argument("--kind", default="fix", choices=list(KINDS), help="分支类型前缀")
    p_new.add_argument("--base", default="master", help="基线引用，默认 master")
    p_new.set_defaults(func=cmd_new)

    p_status = sub.add_parser("status", help="查看三个区的位置、分支、脏状态与绿戳")
    p_status.set_defaults(func=cmd_status)

    p_sync = sub.add_parser("sync-test", help="只把测试区切到指定提交（不跑测试）")
    p_sync.add_argument("--ref", default="master")
    p_sync.set_defaults(func=cmd_sync_test)

    p_verify = sub.add_parser("verify", help="在测试区跑全量回归并写绿戳")
    p_verify.add_argument("--ref", default="master", help="被验证的分支/提交")
    p_verify.add_argument("pytest_args", nargs="*", help="透传给 pytest 的额外参数，例如 tests/test_x.py -x")
    p_verify.set_defaults(func=cmd_verify)

    p_merge = sub.add_parser("merge", help="把已验证的分支 ff 合入 master 并推送")
    p_merge.add_argument("branch", nargs="?", help="分支名，默认取当前目录所在分支")
    p_merge.add_argument("--force", action="store_true", help="跳过绿戳校验（需自行承担风险）")
    p_merge.add_argument("--no-push", action="store_true", help="只本地合入不推送")
    p_merge.set_defaults(func=cmd_merge)

    p_release = sub.add_parser("release", help="verify + merge 一步到位")
    p_release.add_argument("branch", help="待发布分支")
    p_release.add_argument("--force", action="store_true", help="跳过绿戳校验")
    p_release.add_argument("--no-push", action="store_true", help="只本地合入不推送")
    p_release.add_argument("pytest_args", nargs="*", help="透传给 pytest 的额外参数")
    p_release.set_defaults(func=cmd_release)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
