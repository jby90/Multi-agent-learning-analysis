"""闭环二：画像领域路由 v4（三级优先级 + 拓扑 + 前置拉升 + 跨域钳制 + 按需抬前置）。

与 v3（diagnostic_router.DiagnosticRouter）的关系：
- v4 只服务于实况训练通道（interactive_session 生产入口显式开启）；
- v3 与冻结前测（pretest.json）保持原样，作为评测回归通道（蓝图 3.2/闭环二）。
- v4 不读取 profiles 的 gaps_prior / difficulty_start（预设盲区已删除，见蓝图 3.4）。

规则来源：《个性化培训完整蓝图》3.3/3.4/3.6（2026-08-17 定稿）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from agents.diagnosis_agent import load_profiles


PRETEST_PERSONA_PATH = (
    Path(__file__).resolve().parents[1] / "eval" / "cases" / "pretest_persona_v1.json"
)

PERSONA_PRETEST_VERSION = "persona-pretest-v1"
ROUTER_VERSION = "persona-router-v4"

_LEVELS = ("basic", "applied", "advanced")


def _step_up(level: str) -> str:
    if level not in _LEVELS:
        raise ValueError(f"unknown difficulty level: {level}")
    index = _LEVELS.index(level)
    if index + 1 >= len(_LEVELS):
        return level
    return _LEVELS[index + 1]


ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY_PATH = ROOT / "config" / "knowledge_dependencies_v3.json"
EXPERIENCE_TAG_PATH = ROOT / "config" / "diagnostic_experience_tags_v3.json"
PROBE_PATH = ROOT / "config" / "diagnostic_probes_v3.json"


def load_dependencies(path: Path = DEPENDENCY_PATH) -> dict[str, list[str]]:
    """知识点 → 前置知识点（knowledge_dependencies_v3.json）。"""
    data = _load_json(path)
    if not isinstance(data, Mapping) or "prerequisites" not in data:
        raise ValueError(f"{path}: dependency asset schema mismatch")
    return {
        str(point): [str(item) for item in prereqs]
        for point, prereqs in data["prerequisites"].items()
    }


def resolve_experience_tag_point(
    tag_id: str, path: Path = EXPERIENCE_TAG_PATH
) -> str | None:
    """训练关注点 tag_id → 知识点（找不到返回 None）。"""
    data = _load_json(path)
    items = data.get("tags") if isinstance(data, Mapping) else data
    if not isinstance(items, Sequence):
        return None
    for item in items:
        if isinstance(item, Mapping) and str(item.get("tag_id")) == tag_id:
            value = item.get("knowledge_point")
            return str(value) if isinstance(value, str) and value.strip() else None
    return None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid persona pretest asset {path}: {exc}") from exc


class PersonaPretest:
    """按画像加载的前测题集（每点一题、题序=知识目录序=依赖拓扑序）。"""

    def __init__(self, questions: tuple[dict[str, Any], ...]) -> None:
        self._questions = questions

    @property
    def questions(self) -> tuple[dict[str, Any], ...]:
        return self._questions

    def expected_ids(self) -> list[str]:
        return [str(question["question_id"]) for question in self._questions]

    def score(self, answers: Mapping[str, str]) -> list[dict[str, Any]]:
        expected = {str(q["question_id"]): q for q in self._questions}
        if set(answers) != set(expected):
            raise ValueError("answers must contain exactly this persona's pretest IDs")
        evidence: list[dict[str, Any]] = []
        for question in self._questions:
            question_id = str(question["question_id"])
            option = answers[question_id]
            if option not in question["options"]:
                raise ValueError(f"{question_id} answer must be an approved option")
            evidence.append(
                {
                    "evidence_id": question_id,
                    "evidence_source": "pretest",
                    "knowledge_point": str(question["knowledge_point"]),
                    "difficulty": "basic",
                    "is_correct": option == question["answer"],
                }
            )
        return evidence


def load_persona_pretest(
    profile_id: str, path: Path = PRETEST_PERSONA_PATH
) -> PersonaPretest:
    data = _load_json(path)
    if not isinstance(data, Mapping) or "questions" not in data or "sets" not in data:
        raise ValueError(f"{path}: persona pretest asset schema mismatch")
    questions_by_id = {
        str(item["question_id"]): item
        for item in data["questions"]
        if isinstance(item, Mapping) and "question_id" in item
    }
    ordered_ids = data["sets"].get(profile_id)
    if not isinstance(ordered_ids, list) or not ordered_ids:
        raise ValueError(f"{path}: no pretest set for profile {profile_id}")
    missing = [qid for qid in ordered_ids if str(qid) not in questions_by_id]
    if missing:
        raise ValueError(f"{path}: unknown question ids {missing} for {profile_id}")
    questions = tuple(
        questions_by_id[str(qid)]
        for qid in ordered_ids
    )
    return PersonaPretest(questions)


def _topological_order(points: Sequence[str], prerequisites: Mapping[str, Sequence[str]]) -> list[str]:
    """同层内按依赖拓扑排序（域内前置在前；环依赖按输入序稳定输出）。"""
    remaining = list(points)
    placed: list[str] = []
    placed_set: set[str] = set()
    guard = 0
    while remaining:
        guard += 1
        if guard > len(points) ** 2 + 8:  # 依赖环保护：按输入序放出
            placed.extend(remaining)
            break
        progressed = False
        for point in list(remaining):
            prereqs = [
                item
                for item in prerequisites.get(point, ())
                if item in set(points) and item not in placed_set
            ]
            if not prereqs:
                placed.append(point)
                placed_set.add(point)
                remaining.remove(point)
                progressed = True
        if not progressed:
            placed.extend(remaining)
            break
    return placed


class PersonaDiagnosticRouter:
    """三级优先级路由（蓝图 3.4）：①关注点 ②前测答错 ③前测答对。

    - 培养清单只含画像领域（knowledge_scope）内的知识点；
    - 同层拓扑排序；跨层前置拉升；跨域前置钳制（证据标注"画像背景已覆盖"）；
    - 点级档位：答对→应用档起步，答错→基础档起步（蓝图 3.3）；
    - 按需抬前置：依赖点的进阶档若要求前置点进阶（知识块 B 档前置），
      在首个依赖点前插入该前置点的"应用档重学"条目（过应用→进阶，
      复用引擎"初始+升一档"完成标准，不改 T17 语义）。
    """

    def __init__(
        self,
        profiles: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        pretest_path: Path = PRETEST_PERSONA_PATH,
    ) -> None:
        self._profiles = dict(profiles) if profiles is not None else load_profiles()
        self._pretest_path = pretest_path

    def pretest_for(self, profile_id: str) -> PersonaPretest:
        return load_persona_pretest(profile_id, self._pretest_path)

    def build_plan(
        self,
        profile_id: str,
        answers: Mapping[str, str],
        *,
        dependencies: Mapping[str, Sequence[str]],
        experience_tag_point: str | None = None,
        chunk_records: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"unsupported profile_id: {profile_id}")
        scope = [str(item) for item in profile.get("knowledge_scope", [])]
        if not scope:
            raise ValueError(f"profile {profile_id} has no knowledge_scope")
        scope_set = set(scope)

        pretest = self.pretest_for(profile_id)
        evidence = pretest.score(answers)

        wrong: list[str] = []
        correct: list[str] = []
        for item in evidence:
            point = str(item["knowledge_point"])
            if point not in scope_set:
                raise ValueError(
                    f"pretest knowledge point {point} outside {profile_id} scope"
                )
            (wrong if not item["is_correct"] else correct).append(point)

        # 层内拓扑 + 跨层前置拉升
        ordered_wrong = _topological_order(wrong, dependencies)
        ordered_correct = _topological_order(correct, dependencies)
        focus_point = (
            str(experience_tag_point)
            if experience_tag_point and str(experience_tag_point) in scope_set
            else None
        )

        sequence: list[tuple[str, str]] = []  # (knowledge_point, tier)
        tier_of: dict[str, str] = {}
        if focus_point:
            sequence.append((focus_point, "focus"))
            tier_of[focus_point] = "focus"
        for point in ordered_wrong:
            # A selected focus already carries the same pretest evidence and
            # chooses basic/applied from whether that point was answered
            # correctly.  Appending it again as a generic wrong-tier item
            # creates two visually identical learning-plan rows and can make
            # the same knowledge point run twice.
            if point in tier_of:
                continue
            sequence.append((point, "wrong"))
            tier_of.setdefault(point, "wrong")
        for point in ordered_correct:
            if point in tier_of:
                continue
            sequence.append((point, "correct"))
            tier_of[point] = "correct"

        sequence = self._pull_up_prerequisites(sequence, tier_of, dependencies, scope_set)

        plan: list[dict[str, Any]] = []
        for point, tier in sequence:
            if tier == "focus":
                status, source = "needs_training", "focus_tag"
                reason = "训练关注点指定，优先学习。"
                initial = "applied" if point in correct else "basic"
            elif tier == "wrong":
                status, source = "needs_training", "persona_pretest"
                reason = "前测答错，从基础档补足。"
                initial = "basic"
            else:
                status, source = "pending_training", "persona_pretest"
                reason = "前测答对，从应用档起步并冲击进阶。"
                initial = "applied"
            plan.append(
                {
                    "knowledge_point": point,
                    "mastery_status": status,
                    "evidence_source": source,
                    "evidence_ids": [
                        item["evidence_id"]
                        for item in evidence
                        if item["knowledge_point"] == point
                    ],
                    "initial_difficulty": initial,
                    "route_reason": reason,
                    "tier": tier,
                }
            )
        self._renumber(plan)

        # 按需抬前置（蓝图 3.4）：依赖点目标档（初始+1）所需的前置档不足时插入提升条目
        plan = self._insert_prerequisite_lifts(plan, chunk_records or ())

        # Blind spots describe the scored pretest, independently of whether a
        # point is represented by the focus tier in the executable plan.
        blind_spots = list(dict.fromkeys(ordered_wrong))
        selected = plan[0] if plan else None
        return {
            "router_version": ROUTER_VERSION,
            "pretest_version": PERSONA_PRETEST_VERSION,
            "knowledge_point_plan": plan,
            "blind_spots": blind_spots,
            "selected_plan_item_id": selected["plan_item_id"] if selected else None,
            "selected_knowledge_point": selected["knowledge_point"] if selected else None,
            "selected_difficulty": selected["initial_difficulty"] if selected else None,
            "route_evidence": evidence,
            "clamped_prerequisites": sorted(
                {
                    str(prereq)
                    for point in scope
                    for prereq in dependencies.get(point, ())
                    if prereq not in scope_set
                }
            ),
        }

    def _pull_up_prerequisites(
        self,
        sequence: list[tuple[str, str]],
        tier_of: Mapping[str, str],
        dependencies: Mapping[str, Sequence[str]],
        scope_set: set[str],
    ) -> list[tuple[str, str]]:
        """跨层前置拉升：低优先级层的前置点上拉到高层依赖点之前。"""
        position = {point: index for index, (point, _tier) in enumerate(sequence)}
        result = list(sequence)
        changed = True
        guard = 0
        while changed and guard < 64:
            changed = False
            guard += 1
            position = {point: index for index, (point, _tier) in enumerate(result)}
            for index, (point, tier) in enumerate(result):
                for prereq in dependencies.get(point, ()):
                    prereq = str(prereq)
                    if prereq not in scope_set or prereq not in position:
                        continue
                    if position[prereq] > index:
                        entry = result.pop(position[prereq])
                        result.insert(index, entry)
                        changed = True
                        break
                if changed:
                    break
        return result

    def _insert_prerequisite_lifts(
        self,
        plan: list[dict[str, Any]],
        chunk_records: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """按需抬前置：依赖点目标档所需前置档不足 → 插入前置点提升条目。

        chunk_records 来自知识块元数据（kb_loader），每条含 chunk_id /
        knowledge_point / prerequisites（chunk id 列表）；chunk_id 后缀
        -A/-B 表示应用/进阶档。提升条目从应用档起步，经"初始+升一档"完成
        进阶，不改 T17 完成语义。
        """
        def level_of(chunk_id: str) -> str:
            if chunk_id.endswith("-A"):
                return "applied"
            if chunk_id.endswith("-B"):
                return "advanced"
            return "basic"

        chunk_index = {
            str(record["chunk_id"]): (
                str(record["knowledge_point"]),
                level_of(str(record["chunk_id"])),
            )
            for record in chunk_records
        }
        # 知识点 → 其非基础档 chunk 所要求的（前置知识点 → 前置档位）
        required: dict[str, dict[str, str]] = {}
        for record in chunk_records:
            chunk_id = str(record["chunk_id"])
            dependent, level = chunk_index[chunk_id]
            if level == "basic":
                continue  # 基础块前置是点级的，域内拓扑排序已覆盖
            for prereq_chunk in record.get("prerequisites", ()):
                info = chunk_index.get(str(prereq_chunk))
                if info is None:
                    continue
                prereq_point, prereq_level = info
                if prereq_point == dependent or prereq_level == "basic":
                    continue
                slot = required.setdefault(dependent, {})
                keep = slot.get(prereq_point)
                if keep is None or _LEVELS.index(prereq_level) > _LEVELS.index(keep):
                    slot[prereq_point] = prereq_level

        point_target: dict[str, str] = {
            item["knowledge_point"]: _step_up(item["initial_difficulty"])
            for item in plan
        }

        lifts: dict[str, dict[str, Any]] = {}
        for item in plan:
            dependent = item["knowledge_point"]
            dependent_target = point_target.get(dependent)
            if dependent_target is None:
                continue
            needed = required.get(dependent, {})
            for prereq_point, prereq_level in sorted(needed.items()):
                if prereq_point == dependent or prereq_point not in point_target:
                    continue
                if _LEVELS.index(point_target[prereq_point]) < _LEVELS.index(prereq_level):
                    entry = {
                        "knowledge_point": prereq_point,
                        "mastery_status": "needs_training",
                        "evidence_source": "prerequisite_lift",
                        "evidence_ids": [f"LIFT:{dependent}:{prereq_level}"],
                        # 直接从所需档位起步（蓝图 3.4"补齐至所需档位，过该档门禁"）：
                        # 标准条目已通过的前置档位不重过，避免多打一档。
                        "initial_difficulty": prereq_level,
                        "route_reason": (
                            f"学习「{dependent}」的进阶档需要「{prereq_point}」达到"
                            f"{prereq_level}档，直接从{prereq_level}档补齐该前置。"
                        ),
                        "tier": "prerequisite_lift",
                    }
                    lifts[prereq_point] = entry
                    point_target[prereq_point] = prereq_level
                    item["route_reason"] = (
                        str(item.get("route_reason", ""))
                        + f"（按需抬前置：{prereq_point} 已提升至 {prereq_level} 档）"
                    )

        if not lifts:
            return plan

        result: list[dict[str, Any]] = []
        inserted: set[str] = set()
        for item in plan:
            dependent = item["knowledge_point"]
            for prereq_point, entry in lifts.items():
                if prereq_point in inserted or prereq_point == dependent:
                    continue
                if any(
                    f"LIFT:{dependent}:" in str(evidence)
                    for evidence in entry["evidence_ids"]
                ):
                    result.append(entry)
                    inserted.add(prereq_point)
            result.append(item)
        self._renumber(result)
        return result

    @staticmethod
    def _renumber(plan: list[dict[str, Any]]) -> None:
        for index, item in enumerate(plan, start=1):
            item["plan_item_id"] = f"PLAN-{index:03d}"

def calibration_probe_for(
    knowledge_point: str, path: Path = PROBE_PATH
) -> dict[str, Any] | None:
    """闭环三：为关注点选取 1 道应用档校准探针。

    优先 applied_calibration 型（AP-01~05），否则取应用档 diagnostic 型
    （DP-0x-A）；无应用档探针返回 None（调用方退化为不出探针）。
    """
    data = _load_json(path)
    if not isinstance(data, Sequence):
        raise ValueError(f"{path}: probe asset schema mismatch")
    candidates = [
        dict(item)
        for item in data
        if isinstance(item, Mapping)
        and str(item.get("knowledge_point")) == knowledge_point
        and str(item.get("difficulty")) == "applied"
    ]
    if not candidates:
        return None
    for item in candidates:
        if item.get("probe_type") == "applied_calibration":
            return item
    return candidates[0]


def apply_calibration(
    result: dict[str, Any],
    focus_point: str,
    probe_id: str,
    is_correct: bool,
) -> dict[str, Any]:
    """闭环三：校准探针双向回写（蓝图 3.5 四象限，原位更新并返回）。

    探针对 → 应用档起步（前测对=确认；前测错=跳过基础重学）；
    探针错 → 基础档起步（前测对=降回基础；前测错=维持）。
    回写落到培养清单条目的 initial_difficulty（后续微课/任务的实际档位来源）。
    """
    target_level = "applied" if is_correct else "basic"
    for item in result.get("knowledge_point_plan", []):
        if (
            item.get("knowledge_point") == focus_point
            and item.get("tier") == "focus"
        ):
            pretest_correct = item.get("initial_difficulty") == "applied"
            item["initial_difficulty"] = target_level
            item["evidence_ids"] = [
                *list(item.get("evidence_ids", [])),
                f"CALIB:{probe_id}:{'pass' if is_correct else 'fail'}",
            ]
            if is_correct and pretest_correct:
                item["route_reason"] = "校准探针答对：确认从应用档起步。"
            elif is_correct:
                item["route_reason"] = "校准探针答对：跳过基础重学，直接从应用档起步。"
            elif pretest_correct:
                item["route_reason"] = "校准探针答错：降回基础档起步，先夯实基础。"
            else:
                item["route_reason"] = "校准探针答错：维持基础档起步。"
            item["calibrated_by"] = str(probe_id)
            break
    result["route_evidence"] = [
        *list(result.get("route_evidence", [])),
        {
            "evidence_id": str(probe_id),
            "evidence_source": "calibration_probe",
            "knowledge_point": focus_point,
            "difficulty": "applied",
            "is_correct": bool(is_correct),
        },
    ]
    plan = result.get("knowledge_point_plan", [])
    if plan:
        first = plan[0]
        result["selected_plan_item_id"] = first.get("plan_item_id")
        result["selected_knowledge_point"] = first.get("knowledge_point")
        result["selected_difficulty"] = first.get("initial_difficulty")
    result["calibration"] = {
        "probe_id": str(probe_id),
        "knowledge_point": focus_point,
        "is_correct": bool(is_correct),
    }
    return result
