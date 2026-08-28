# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""benchmark 目录 CRUD、case 校验、scoreboard 原子追加、leak check、导入导出。

布局(见 ``paths.py``)::

    benchmarks/<id>/
    ├── benchmark_config.toml      # title/description/test_agent/runs=1/frozen
    ├── scoreboard.yaml            # evaluations: [];frozen 后才允许追加
    ├── CASE-<nnn>-<semantic>/{statement,rubric}/README.md
    └── evaluations/*.yaml         # 每 cell 证据归档(被拒候选也留,不入 scoreboard)

scoreboard 追加采用 fcntl 文件锁 + 临时文件原子替换 + 写后完整 re-parse 校验
(Penguin "写后重 parse" 规则的后端固化)。frozen 门槛与版本单调由
``schemas.validate_evaluation_append`` 强制。
"""

from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
import tomllib
import zipfile
from pathlib import Path

from jiuwenswarm.harness_evolve import paths
from jiuwenswarm.harness_evolve.errors import (
    BenchmarkError,
    BenchmarkValidationError,
    ScoreboardError,
)
from jiuwenswarm.harness_evolve.schemas import (
    BENCHMARK_ID_RE,
    CASE_ID_RE,
    BenchmarkConfig,
    Evaluation,
    Scoreboard,
    parse_rubric_points,
    require_rubric_sum_100,
    scoreboard_from_yaml,
    scoreboard_to_yaml,
    validate_evaluation_append,
)

__all__ = [
    "benchmark_exists",
    "list_benchmarks",
    "init_benchmark",
    "read_config",
    "write_case",
    "list_cases",
    "read_statement",
    "read_rubric",
    "validate_benchmark",
    "leak_check",
    "freeze_benchmark",
    "read_scoreboard",
    "append_evaluation",
    "export_benchmark",
    "import_benchmark",
]


def _escape_toml(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


def _write_config(path: Path, cfg: BenchmarkConfig) -> None:
    lines = [
        f'title = "{_escape_toml(cfg.title)}"',
        f'description = "{_escape_toml(cfg.description)}"',
        f'test_agent = "{_escape_toml(cfg.test_agent)}"',
        f"runs = {cfg.runs}",
        f"frozen = {str(cfg.frozen).lower()}",
        f"version = {cfg.version}",
    ]
    if cfg.parent:
        lines.append(f'parent = "{_escape_toml(cfg.parent)}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def benchmark_exists(benchmark_id: str) -> bool:
    return paths.benchmark_dir(benchmark_id).is_dir()


def list_benchmarks() -> list[str]:
    root = paths.benchmarks_dir()
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and BENCHMARK_ID_RE.match(p.name)
    )


# ── 创建与读取 ────────────────────────────────────────────────────────────


def init_benchmark(
    benchmark_id: str, title: str, description: str, test_agent: str
) -> Path:
    """创建 benchmark 目录树(benchmark_config.toml + 空 scoreboard)。

    id 必须匹配 ``[a-z0-9-]{3,64}``;已存在 → 拒绝。
    """
    if not BENCHMARK_ID_RE.match(benchmark_id):
        raise BenchmarkValidationError(
            f"benchmark id 必须匹配 {BENCHMARK_ID_RE.pattern!r},得到 {benchmark_id!r}"
        )
    if not title.strip() or not test_agent.strip():
        raise BenchmarkValidationError("title 与 test_agent 不能为空")
    if benchmark_exists(benchmark_id):
        raise BenchmarkValidationError(f"benchmark 已存在: {benchmark_id}")

    bid_dir = paths.benchmark_dir(benchmark_id)
    bid_dir.mkdir(parents=True, exist_ok=True)
    try:
        _write_config(
            paths.benchmark_config_file(benchmark_id),
            BenchmarkConfig(
                title=title.strip(),
                description=description.strip(),
                test_agent=test_agent.strip(),
            ),
        )
        paths.scoreboard_file(benchmark_id).write_text(
            scoreboard_to_yaml(Scoreboard()), encoding="utf-8"
        )
    except Exception:
        shutil.rmtree(bid_dir, ignore_errors=True)
        raise
    return bid_dir


def read_config(benchmark_id: str) -> BenchmarkConfig:
    path = paths.benchmark_config_file(benchmark_id)
    if not path.is_file():
        raise BenchmarkError(f"benchmark 不存在或无 config: {benchmark_id}")
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    try:
        return BenchmarkConfig(
            title=str(raw.get("title", "")),
            description=str(raw.get("description", "")),
            test_agent=str(raw.get("test_agent", "")),
            runs=int(raw.get("runs", 1)),
            frozen=bool(raw.get("frozen", False)),
            version=int(raw.get("version", 1)),
            parent=str(raw["parent"]) if raw.get("parent") else None,
        )
    except (TypeError, ValueError) as exc:
        raise BenchmarkError(f"benchmark_config.toml 解析失败: {exc}") from exc


def is_frozen(benchmark_id: str) -> bool:
    return read_config(benchmark_id).frozen


# ── case ──────────────────────────────────────────────────────────────────


def write_case(
    benchmark_id: str, case_id: str, statement_md: str, rubric_md: str
) -> Path:
    """写入一个 case(statement 公开 / rubric 私有,总和恰 100)。

    frozen 之后拒绝写。
    """
    if not CASE_ID_RE.match(case_id):
        raise BenchmarkValidationError(
            f"case id 必须匹配 {CASE_ID_RE.pattern!r},得到 {case_id!r}"
        )
    if not benchmark_exists(benchmark_id):
        raise BenchmarkError(f"benchmark 不存在: {benchmark_id}")
    if is_frozen(benchmark_id):
        raise BenchmarkValidationError(
            f"benchmark 已冻结,禁止写 case: {benchmark_id}"
        )
    if not statement_md.strip():
        raise BenchmarkValidationError(f"{case_id}: statement 不能为空")
    points = parse_rubric_points(rubric_md)
    require_rubric_sum_100(points)

    case_dir = paths.benchmark_case_dir(benchmark_id, case_id)
    (case_dir / "statement").mkdir(parents=True, exist_ok=True)
    (case_dir / "rubric").mkdir(parents=True, exist_ok=True)
    (case_dir / "statement" / "README.md").write_text(
        statement_md, encoding="utf-8"
    )
    (case_dir / "rubric" / "README.md").write_text(rubric_md, encoding="utf-8")
    return case_dir


def list_cases(benchmark_id: str) -> list[str]:
    root = paths.benchmark_cases_dir(benchmark_id)
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and CASE_ID_RE.match(p.name)
    )


def read_statement(benchmark_id: str, case_id: str) -> str:
    path = (
        paths.benchmark_case_dir(benchmark_id, case_id) / "statement" / "README.md"
    )
    if not path.is_file():
        raise BenchmarkError(f"statement 不存在: {benchmark_id}/{case_id}")
    return path.read_text(encoding="utf-8")


def read_rubric(benchmark_id: str, case_id: str) -> str:
    path = paths.benchmark_case_dir(benchmark_id, case_id) / "rubric" / "README.md"
    if not path.is_file():
        raise BenchmarkError(f"rubric 不存在: {benchmark_id}/{case_id}")
    return path.read_text(encoding="utf-8")


# ── case 删除与 benchmark 版本化演化 ────────────────────────────────────────


def delete_case(benchmark_id: str, case_id: str) -> bool:
    """删除一个 case(仅限未冻结 benchmark)。"""
    if not benchmark_exists(benchmark_id):
        raise BenchmarkError(f"benchmark 不存在: {benchmark_id}")
    if is_frozen(benchmark_id):
        raise BenchmarkValidationError(
            f"benchmark 已冻结,禁止删 case: {benchmark_id}"
        )
    case_dir = paths.benchmark_case_dir(benchmark_id, case_id)
    if not case_dir.is_dir():
        raise BenchmarkValidationError(f"case 不存在: {benchmark_id}/{case_id}")
    shutil.rmtree(case_dir)
    return True


def evolve_benchmark(
    benchmark_id: str,
    *,
    new_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> dict:
    """版本化复制:冻结的 benchmark → 新一版(继承 cases,空 scoreboard)。

    旧 benchmark 与旧 scoreboard 原样保留(分数可比性守护);新版
    unfrozen,可自由增删改 case,重新 pilot → freeze → baseline。
    """
    cfg = read_config(benchmark_id)
    if not cfg.frozen:
        raise BenchmarkValidationError(
            f"benchmark 未冻结: {benchmark_id}。尺子未定稿时直接修改原 benchmark 即可;"
            "定稿后先 benchmark-freeze 再 evolve"
        )
    target_id = new_id or f"{benchmark_id}-v{cfg.version + 1}"
    if not BENCHMARK_ID_RE.match(target_id):
        raise BenchmarkValidationError(
            f"benchmark id 必须匹配 {BENCHMARK_ID_RE.pattern!r},得到 {target_id!r}"
        )
    if benchmark_exists(target_id):
        raise BenchmarkValidationError(f"benchmark 已存在: {target_id}")

    bid_dir = paths.benchmark_dir(target_id)
    bid_dir.mkdir(parents=True, exist_ok=True)
    try:
        _write_config(
            paths.benchmark_config_file(target_id),
            BenchmarkConfig(
                title=(title or cfg.title).strip(),
                description=(description or cfg.description).strip(),
                test_agent=cfg.test_agent,
                runs=cfg.runs,
                frozen=False,
                version=cfg.version + 1,
                parent=benchmark_id,
            ),
        )
        paths.scoreboard_file(target_id).write_text(
            scoreboard_to_yaml(Scoreboard()), encoding="utf-8"
        )
        # 全部 case 拷贝(statement + rubric);scoreboard 不继承 = 新周期
        for case_id in list_cases(benchmark_id):
            shutil.copytree(
                paths.benchmark_case_dir(benchmark_id, case_id),
                paths.benchmark_case_dir(target_id, case_id),
            )
    except Exception:
        shutil.rmtree(bid_dir, ignore_errors=True)
        raise
    return {
        "benchmark": target_id,
        "parent": benchmark_id,
        "version": cfg.version + 1,
        "cases": list_cases(target_id),
    }


# ── 校验与 leak check ─────────────────────────────────────────────────────


def validate_benchmark(benchmark_id: str) -> list[str]:
    """返回缺陷清单;空列表 = 可冻结。"""
    defects: list[str] = []
    try:
        cfg = read_config(benchmark_id)
    except BenchmarkError as exc:
        return [f"config: {exc}"]
    if not cfg.title:
        defects.append("config: title 为空")
    if not cfg.test_agent:
        defects.append("config: test_agent 为空")

    cases = list_cases(benchmark_id)
    if not cases:
        defects.append("无任何 case")
    for case_id in cases:
        if not (
            paths.benchmark_case_dir(benchmark_id, case_id) / "statement"
            / "README.md"
        ).is_file():
            defects.append(f"{case_id}: 缺少 statement/README.md")
        rubric_path = (
            paths.benchmark_case_dir(benchmark_id, case_id) / "rubric" / "README.md"
        )
        if not rubric_path.is_file():
            defects.append(f"{case_id}: 缺少 rubric/README.md")
            continue
        try:
            points = parse_rubric_points(rubric_path.read_text(encoding="utf-8"))
            require_rubric_sum_100(points)
        except BenchmarkValidationError as exc:
            defects.append(f"{case_id}: rubric 校验失败: {exc}")
    return defects


def _normalize_line(line: str) -> str:
    return " ".join(line.split()).lower()


def _rubric_fragments(rubric_md: str) -> list[str]:
    """提取 rubric 中有实质内容的片段(正文行 + 表格 description 列)。

    供 leak-check 做子串匹配;表格行本身不会逐字出现在 statement 里,
    但其描述列文本是真实的泄露向量。
    """
    fragments: list[str] = []
    for line in rubric_md.splitlines():
        norm = _normalize_line(line)
        if not norm or len(norm) < 12 or set(norm) <= {"-", "|", ":"}:
            continue
        if norm.startswith("|"):
            cells = [c.strip() for c in line.split("|")]
            if len(cells) >= 4:
                desc = _normalize_line(cells[3])
                if desc and desc != "description" and len(desc) >= 12:
                    fragments.append(desc)
            continue
        fragments.append(norm)
    return fragments


def leak_check(benchmark_id: str) -> list[str]:
    """静态扫描:statement 中不得出现 rubric 内容片段或 'rubric' 字样。"""
    issues: list[str] = []
    for case_id in list_cases(benchmark_id):
        statement = read_statement(benchmark_id, case_id)
        rubric = read_rubric(benchmark_id, case_id)
        stmt_joined = "\n".join(
            _normalize_line(l) for l in statement.splitlines()
        )
        if "rubric" in stmt_joined:
            issues.append(f"{case_id}: statement 引用了 'rubric'(泄露风险)")
        for frag in _rubric_fragments(rubric):
            if frag in stmt_joined:
                issues.append(f"{case_id}: statement 泄露 rubric 片段: {frag[:80]}")
    return issues


def freeze_benchmark(benchmark_id: str) -> None:
    """冻结 benchmark:先校验无缺陷,再写 frozen=true。"""
    defects = validate_benchmark(benchmark_id)
    if defects:
        raise BenchmarkValidationError(
            f"benchmark 未通过校验,不能冻结:\n- " + "\n- ".join(defects)
        )
    cfg = read_config(benchmark_id)
    if cfg.frozen:
        raise BenchmarkValidationError(f"benchmark 已冻结: {benchmark_id}")
    cfg.frozen = True
    _write_config(paths.benchmark_config_file(benchmark_id), cfg)


# ── scoreboard ────────────────────────────────────────────────────────────


def read_scoreboard(benchmark_id: str) -> Scoreboard:
    path = paths.scoreboard_file(benchmark_id)
    if not path.is_file():
        raise BenchmarkError(f"benchmark 不存在或无 scoreboard: {benchmark_id}")
    return scoreboard_from_yaml(path.read_text(encoding="utf-8"))


def append_evaluation(benchmark_id: str, evaluation: Evaluation) -> Scoreboard:
    """原子追加一条已接受评估(fcntl 锁 + tmp 替换 + 写后重 parse 校验)。"""
    path = paths.scoreboard_file(benchmark_id)
    if not path.is_file():
        raise BenchmarkError(f"benchmark 不存在或无 scoreboard: {benchmark_id}")

    frozen = is_frozen(benchmark_id)
    with open(path, "r", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            board = scoreboard_from_yaml(fh.read())
            validate_evaluation_append(board, evaluation, frozen=frozen)
            board.evaluations.append(evaluation)

            tmp_path = path.with_name(path.name + ".tmp")
            tmp_path.write_text(scoreboard_to_yaml(board), encoding="utf-8")
            with open(tmp_path, "rb") as tmp_fh:
                os.fsync(tmp_fh.fileno())
            os.replace(tmp_path, path)

            # 写后完整 re-parse 校验(Penguin 规则的固化)
            rewritten = scoreboard_from_yaml(path.read_text(encoding="utf-8"))
            if len(rewritten.evaluations) != len(board.evaluations):
                raise ScoreboardError("写后重 parse 校验失败: 条目数不一致")
            return rewritten
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ── 导入 / 导出 ───────────────────────────────────────────────────────────


def export_benchmark(benchmark_id: str, out_path: str | Path) -> Path:
    """导出 benchmark 为 zip(zip 顶层为 <benchmark_id>/ 目录)。"""
    if not benchmark_exists(benchmark_id):
        raise BenchmarkError(f"benchmark 不存在: {benchmark_id}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    archive = shutil.make_archive(
        str(out_path.with_suffix("")),
        "zip",
        root_dir=paths.benchmarks_dir(),
        base_dir=benchmark_id,
    )
    return Path(archive)


def import_benchmark(
    zip_path: str | Path, new_id: str | None = None
) -> str:
    """从 zip 导入 benchmark。返回导入后的 benchmark id。

    zip 顶层应为单个 <benchmark_id>/ 目录;``new_id`` 可改名导入
    (id 冲突时强制要求)。
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise BenchmarkError(f"zip 不存在: {zip_path}")

    with tempfile.TemporaryDirectory() as tmp:
        extract_dir = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
        top_dirs = [
            p for p in extract_dir.iterdir() if p.is_dir()
        ]
        if len(top_dirs) != 1:
            raise BenchmarkValidationError(
                f"zip 顶层必须恰好一个目录,得到 {len(top_dirs)} 个"
            )
        source_id = top_dirs[0].name
        if not BENCHMARK_ID_RE.match(source_id):
            raise BenchmarkValidationError(
                f"zip 内 benchmark id 非法: {source_id!r}"
            )
        target_id = new_id or source_id
        if not BENCHMARK_ID_RE.match(target_id):
            raise BenchmarkValidationError(
                f"目标 benchmark id 必须匹配 {BENCHMARK_ID_RE.pattern!r}"
            )
        if target_id != source_id and new_id:
            # 改名导入:把顶层目录重命名为新 id
            renamed = extract_dir / target_id
            top_dirs[0].rename(renamed)
        if benchmark_exists(target_id):
            raise BenchmarkValidationError(
                f"benchmark 已存在: {target_id}(可用 --as 改名导入)"
            )
        # 结构校验(config 可解析 + scoreboard 可解析)
        target_dir = extract_dir / target_id
        config_path = target_dir / "benchmark_config.toml"
        scoreboard_path = target_dir / "scoreboard.yaml"
        if not config_path.is_file() or not scoreboard_path.is_file():
            raise BenchmarkValidationError(
                "zip 缺少 benchmark_config.toml 或 scoreboard.yaml"
            )
        try:
            with open(config_path, "rb") as fh:
                tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise BenchmarkValidationError(f"config 解析失败: {exc}") from exc
        try:
            scoreboard_from_yaml(scoreboard_path.read_text(encoding="utf-8"))
        except ScoreboardError as exc:
            raise BenchmarkValidationError(f"scoreboard 解析失败: {exc}") from exc
        final = paths.benchmarks_dir() / target_id
        final.parent.mkdir(parents=True, exist_ok=True)
        (extract_dir / target_id).rename(final)
    return target_id
