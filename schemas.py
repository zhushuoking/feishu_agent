"""大模型输出的结构化 Schema（对应设计文档 8.3 任务识别输出 Schema）。

原则：模型输出一律不可信，进业务前必须过这里。
字段类型、枚举、长度、数组大小都在这一层卡死，卡不住就丢弃重试，
绝不能让「模型多返回一个字段」这种问题流到数据库里。
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

CandidateType = Literal["task", "inquiry", "notice", "unrelated", "unclear"]
MissingField = Literal[
    "assignee", "deliverable", "deadline", "acceptance_criteria", "task_content"
]
DeadlineStatus = Literal["resolved", "unresolved", "missing"]

MAX_TASKS_PER_MESSAGE = 10


class ExtractedTask(BaseModel):
    """单条任务候选。"""

    title: str = Field(..., description="任务标题，不超过 80 字")
    description: Optional[str] = Field(None, description="任务补充说明")
    assignee_mention_ids: List[str] = Field(
        default_factory=list, description="负责人，只能引用当前消息 mentions 里的标识"
    )
    deliverable: Optional[str] = Field(None, description="交付物")
    deadline_text: Optional[str] = Field(None, description="截止时间的原文表述，不要自己换算")
    acceptance_criteria: Optional[str] = Field(None, description="验收标准")
    priority: Literal["normal", "urgent"] = "normal"
    missing_fields: List[MissingField] = Field(default_factory=list)
    evidence: str = Field("", description="支撑该判断的原文片段")

    # ---- 以下字段模型不输出，由程序（deadline.py）回填 ----
    deadline_at: Optional[datetime] = Field(
        None, description="系统解析出的截止时间，带时区；解析失败为 None"
    )
    deadline_status: DeadlineStatus = Field(
        "missing", description="resolved 可用了 / unresolved 算不出需澄清 / missing 原话没给"
    )
    deadline_rule: Optional[str] = Field(None, description="命中的解析规则，排错用")

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("title 不能为空")
        if len(value) > 200:
            raise ValueError("title 过长")
        return value

    @field_validator("deadline_text")
    @classmethod
    def _blank_to_none(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


class TaskRecognition(BaseModel):
    """一次任务识别的完整结果。"""

    is_task_candidate: bool
    candidate_type: CandidateType
    confidence: float = Field(..., ge=0.0, le=1.0, description="0~1，模型自评置信度")
    tasks: List[ExtractedTask] = Field(default_factory=list)
    clarification_needed: bool = False
    clarification_question: Optional[str] = None

    @field_validator("tasks")
    @classmethod
    def _cap_tasks(cls, value: List[ExtractedTask]) -> List[ExtractedTask]:
        if len(value) > MAX_TASKS_PER_MESSAGE:
            raise ValueError(f"单条消息最多 {MAX_TASKS_PER_MESSAGE} 个候选，当前 {len(value)} 个")
        return value

    @model_validator(mode="after")
    def _consistency(self):
        # 不是任务候选就不该带任务列表，防止模型自相矛盾
        if not self.is_task_candidate:
            self.tasks = []
        # 说自己是任务却没有任务体，等于没识别出来
        if self.is_task_candidate and not self.tasks:
            self.is_task_candidate = False
            self.candidate_type = "unclear"
        return self

    def high_confidence(self, threshold: float = 0.90) -> bool:
        """置信度高且必备字段齐全。注意：首期即使满足也仍需人工确认。"""
        if not self.tasks:
            return False
        return self.confidence >= threshold and not self.tasks[0].missing_fields
