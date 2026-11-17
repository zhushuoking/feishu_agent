---
name: 新功能 / 待办
about: 想加一个能力，或者拆一件要做的事
title: "[feat] "
labels: enhancement
assignees: ""
---

## 要解决什么

<!-- 先说场景，别先说方案。
     反例：「加一个 deadline 变更功能」
     正例：「群里说『延到周五』，现在系统当普通聊天，任务截止时间还是老的，
            结果日报一直提醒已经延期的旧时间」 -->

## 完成标准

<!-- 怎么算做完了。写成能验证的句子，最好能对应一条测试 -->

- [ ]
- [ ]

## 涉及哪里

<!-- 拿不准就留空。知道的话勾一下，方便分工 -->

- [ ] 消息接入 / 去重（main.py models.py store.py）
- [ ] 任务识别（detector.py prompts.py schemas.py）
- [ ] 时间解析（deadline.py）
- [ ] 数据层（db.py models_db.py repo.py）
- [ ] 卡片（cards.py card_action.py）
- [ ] 状态与权限（state_machine.py permissions.py）
- [ ] 通知（notify.py worker.py events.py）
- [ ] 反馈（feedback.py）
- [ ] 文档

## 有什么设计约束

<!-- 下面几条是这个项目的底线，碰了要单独说明理由 -->

- [ ] 不需要模型直接写库 / 发消息 / 改状态（模型只出候选）
- [ ] 不需要绕过 `repo.transition_task()` 改状态
- [ ] 需要新增飞书权限（写明哪个 scope、是否要发布新版本）

## 备注
