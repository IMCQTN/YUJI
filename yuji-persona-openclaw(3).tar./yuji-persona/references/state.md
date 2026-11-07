# 状态与可恢复记忆

## 目录
1. 分层和归属
2. 事务与人物隔离
3. 长对话、分支和遗忘
4. 导出与持久保存

## 1. 分层和归属

| 数据 | 来源 | 允许如何使用 |
| --- | --- | --- |
| profile rules | 原始训练文本/明确纠正 | 控制表达风格 |
| profile claims | 目标人物语料自述 | 有时间和来源限制的自述，不保证现实真伪 |
| user_statement | 用户在当前会话说的话 | 保留 subject=user，不改成角色经历 |
| character_statement | 模拟回复 | 维持这段剧情连贯，不当真人事实 |
| user_setting | 用户明确人物/关系设定 | 在该场景中使用，保留设定来源 |
| fictional | 明确虚构世界信息 | 只在虚构分支内使用 |

每条记忆包含唯一 id、text、subject、provenance、sources 和 quote/source_note。工具核对 quote 确实来自指定说话人的原文，但不自动证明摘要含义正确；生成者仍须审查否定、时间、对象和不确定性。

profile.representation 固定为 style_simulation，标签为“姓名（模拟）”，禁止真实身份声明和对外发送。session 保留 simulation_label，载入与导入均校验；turn 返回可直接显示的带标签 display_reply。存储中的 assistant 原文不含标签，便于风格统计；面向用户展示必须使用带标签版本。

`simulation_state.tension` 的 neutral/raised/unresolved/easing 是剧情控制标签。conflict 将其设为 raised，repair_attempt 在有冲突时设为 unresolved；仅明确接受修复时用 repair_accepted，设为 easing。friendly 不自动恢复信任。标签不应作为“真人心理分析”向用户解释。

## 2. 事务与人物隔离

存储路径：`STORE/<persona_id>/<session_id>.json`，快照放 `snapshots/<session_id>/`。人物标识不只依赖名字，禁止读取同名他人的会话。分支仍属于同一人物。

所有修改使用锁、原子替换和 expected-revision。相同 turn ID、相同内容重复提交是幂等重试；相同 ID 不同内容拒绝。过期 revision 拒绝，不能自动取新版本然后强行覆盖。读取后检查用户实际发出的回复，不把未发送的备选写入。

文件锁按实际执行系统选择：Linux/WSL 使用 fcntl；原生 Windows 使用 msvcrt 的第零字节锁，并在释放时定位到相同字节。Windows 争用超过 30 秒会报错，不能因此绕开锁。锁只用于同一执行环境中的本地文件；不要让 Windows 与 WSL 同时写同一份会话。需要切换运行环境时使用 export/import，并保留各自独立状态目录。

profile_hash 变化会暂停会话写入。明确核对人物和改动后执行 rebind；不把新语料或用户纠正静默应用为“此前就知道”。从旧快照分支时必须匹配当时 profile_hash；要保留旧版可用 profile.py feedback 保存的档案历史。

## 3. 长对话、分支和遗忘

超过 24 轮时工具提示摘要，不自动截断。先为要压缩的每一轮建立带来源的记忆（可归纳但不能把矛盾揉平），再 compact 保留最近 12 轮。未被摘要覆盖的旧轮次不会删除。摘要应优先保留称呼、明确承诺、边界、偏好、未解决问题以及其归属。

branch 从指定 revision 建新 session，并从 0 开始版本，不修改主线。模拟多个可能回答时分别使用新分支；不能把 A 分支发生的事带进 B 分支。

分支名不能与主线仅大小写不同，以避免 Windows 上同一锁文件被重复取得。导出同时锁定来源会话和目的文件；多个会话同时导出到同一路径时，只允许一个成功，其余报文件已存在，不能静默覆盖恢复包。

forget 的确切范围：

- 指定 memory-id：删除该记忆、它引用的原始轮次、引用这些轮次的其他记忆，以及该 session 全部旧快照。
- 指定 turn-id：删除该轮原文和派生记忆，并清理该 session 全部旧快照。
- 清理后保留已遗忘 turn ID 的墓碑，避免重试重新恢复内容；重置派生场景标签。
- 不自动清理其他分支、profile 原始语料、外部导出包或已保存副本；要处理这些应逐一定位并按用户要求操作。不能宣称已全局彻底删除。
- 只遗忘无 source-turn 的 user_setting/fictional 条目时，不删除无关原文，但仍清除当前 session 旧快照。

删除整个工作会话时，定位当前文件和 snapshots 子目录，只删除明确指定人物的会话；不要清空 STORE 根目录。没有外部删除权限时如实说明。

## 4. 导出与持久保存

export 包含 profile、当前 state、校验和，不含完整训练集、已清理历史快照。若需要完整继续检索，另存 corpus 工作数据和原始来源映射。import 要求匹配 profile、有效校验和和未占用 session 名，不覆盖已有会话。校验和用于意外损坏检测，不是防恶意篡改的数字签名。

本地存档在当前工作环境中可复用；不能假定任意新聊天都能自动访问。跨会话可靠方案是保存恢复包及所需语料文件，之后重新载入。用户可访问持久存储可用时，在明确检查点按环境工作流保存文件，成功后提供可打开的链接。

恢复包有聊天内容；提供给第三方、课堂或公开分享时优先用虚构样例或脱敏版本。不要把用户原始聊天记录打包进安装 Skill。
