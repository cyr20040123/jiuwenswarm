# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""harness_evolve CLI(``jiuwenswarm-harness-evolve``)。

命令按流水线分组(实现随 P2-P4 逐步注册):

- agent 管理:       agent-register / agent-list / agent-inspect /
                    agent-export / agent-import / agent-delete(跨机迁移)
- benchmark-design: benchmark-init / case-write / benchmark-validate / leak-check /
                    benchmark-freeze / scoreboard-append / scoreboard-read /
                    benchmark-export / benchmark-import
- agent-evaluation: eval-run / eval-matrix / report
- agent-optimization: agent-version / snapshot-create / agent-edit / agent-restore /
                      baseline-record / optimize

全局约定:
- ``--data-dir`` 覆盖数据根目录(默认 ``get_user_workspace_dir()/"harness-evolve"``);
- ``--json`` 输出机器可解析 JSON;
- 退出码:0=成功,2=用户输入错误,3=执行失败。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Callable

from jiuwenswarm.harness_evolve import __version__
from jiuwenswarm.harness_evolve.errors import HarnessError, HarnessUsageError
from jiuwenswarm.harness_evolve.paths import set_base_dir

# 子命令注册表:名称 -> (parser 装配函数, 执行函数)
# 各模块在 _register_commands 中惰性 import 并注册,避免 CLI 加载时引入
# openjiuwen 等重依赖。
_COMMANDS: dict[str, tuple[Callable[[argparse.ArgumentParser], None], Callable]] = {}


def _register_commands() -> None:
    from jiuwenswarm.harness_evolve.commands import register_commands

    register_commands(_COMMANDS)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="harness_evolve 数据根目录(默认 ~/.jiuwenswarm/harness-evolve)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiuwenswarm-harness-evolve",
        description="Penguin 式 agent harness 自演进闭环(agent-creation / "
        "benchmark-design / agent-evaluation / agent-optimization)。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    _register_commands()
    for name, (add_parser, _run) in sorted(_COMMANDS.items()):
        sub = subparsers.add_parser(name, help=add_parser.__doc__ or "")
        _add_common_args(sub)
        add_parser(sub)

    return parser


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    if args.data_dir:
        set_base_dir(args.data_dir)

    _run = _COMMANDS[args.command][1]
    try:
        result = _run(args)
        if args.json and result is not None:
            _print_json(result)
        return 0
    except HarnessUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — CLI 顶层兜底
        print(f"unexpected error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
