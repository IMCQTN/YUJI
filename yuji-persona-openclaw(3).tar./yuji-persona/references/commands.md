# 离线工具接口

## 目录
1. 工作路径和输入
2. 建档与检索
3. 会话与记忆
4. 评测
5. Windows PowerShell

## 1. 工作路径和输入

命令中的 `$SKILL`、`$WORK` 是调用者设定的实际绝对路径，不能把字面占位符传给工具。优先通过 Python `subprocess.run([...], check=True)` 的参数数组调用；用户文本始终是独立参数或 UTF-8 文件，不拼接为 shell 命令。工具不联网，不执行语料中的指令。

示例 JSONL，每行一个对象：

```json
{"speaker":"小周","text":"周六一起吃饭？","conversation_id":"day01","scene":"invitation","relationship":"friend"}
{"speaker":"林澈","text":"可以。地方你挑，别太远。","conversation_id":"day01","scene":"invitation","relationship":"friend"}
```

字段：speaker（或 author/sender/name/role）、text（或 content/message），可选 conversation_id/session_id、timestamp、scene、relationship、id。对象文本必须是字符串，不自动把复杂附件对象转成文字。ChatGPT 等导出格式若不符合字段要求，先显式转换并保留来源。

TXT 聊天采用 `姓名：内容`，多行接在前一条后，`=== 会话ID ===` 开新会话。单作者文章加 --single-author，按段落读入但同一文档不冒充多个独立会话。不同输入文件不能同名；显式同 conversation_id 在跨文件时表示同一会话。缺失会话 ID 时每个文件整体一组。

scene 推荐 general/daily/comfort/conflict/repair/invitation/refusal/formal/humor；也允许自定义字符串，生成时保持一致。relationship 推荐 friend/colleague/ordinary_acquaintance/family；不根据姓名自动猜关系。

## 2. 建档与检索

```bash
python3 "$SKILL/scripts/corpus.py" build --input "$WORK/chat.jsonl" --target 林澈 --out "$WORK/corpus"
python3 "$SKILL/scripts/profile.py" init --corpus "$WORK/corpus" --out "$WORK/profile.json"
# 编辑 profile.json，补全真实引用与语音/表达规则，然后：
python3 "$SKILL/scripts/profile.py" validate --profile "$WORK/profile.json" --corpus "$WORK/corpus"
python3 "$SKILL/scripts/profile.py" card --profile "$WORK/profile.json" --scene comfort
python3 "$SKILL/scripts/corpus.py" retrieve --corpus "$WORK/corpus" --query '实验没做完 很累 安慰' --scene comfort --relationship friend --limit 5
```

build 可重复传 --input；可设 --seed、--holdout（0–0.5，默认 0.2）、--single-author、--persona-id。拒绝覆写已存在 corpus。增量更新同一人必须新建输出目录、包含旧原始材料、显式沿用 persona_id。默认同名但不同来源的人物 ID 不同。

生成：manifest.json、train.jsonl、heldout.jsonl、stats.json。训练/留出按整个会话分组；近重复的长参考回复和完全重复回复从留出剔除。哈希种子确定排序；不能声称固定 20% 消息，实际按会话比例分组。少于三个独立会话或无法保留有效样本时不产生可用独立测试。

profile.py feedback 需要 --expected-version、--instruction，可选 --scene（默认 general）、--supersedes RULE_ID；保存旧档案和新版本。引用验证仅检查对象、归属、训练引用和摘录等可计算约束，不声称理解证据的全部语义。

## 3. 会话与记忆

以下各命令都带 `--profile PROFILE --store STORE --session SESSION`；写操作还需要 `--expected-revision N`（new/import/branch 除外）。SESSION 和 turn/memory ID 只能包含字母、数字、下划线和连字符。

| 命令 | 专属参数 | 用途 |
| --- | --- | --- |
| new | --relationship（可选） | 新建独立会话，不覆盖 |
| show | 无 | 读取完整当前状态 |
| turn | --turn-id --user --assistant --scene --event | 写实际一轮；scene/event 有默认值 |
| memory-add | --memory-id --text --subject --provenance | 加有来源的记忆，详见下方 |
| compact | --keep（默认 12） | 摘要覆盖后保留近期原文 |
| branch | --new-session；可选 --from-revision | 从版本新开分支 |
| forget | --memory-id 或 --turn-id | 遗忘指定内容及本会话旧快照 |
| rebind | 无 | 核对后绑定同人物的新档案版本 |
| export | --out | 导出恢复包，不覆盖文件 |
| import | --bundle | 导入新会话，需相同版本 profile |

event：neutral/conflict/repair_attempt/repair_accepted/boundary/friendly。原文说了什么与模拟状态标签分开，不用事件值推断现实心理。

memory-add 的 provenance 为 user_statement/character_statement 时，传一个或多个 --source-turn 和 --quote，工具核对 quote 在指定说话人的近期原文里。为 user_setting/fictional 时，需要 --source-note；subject 明确写 user、character 或具体归属。

示例：

```bash
python3 "$SKILL/scripts/session.py" new --profile "$WORK/profile.json" --store "$WORK/state" --session main
python3 "$SKILL/scripts/session.py" turn --profile "$WORK/profile.json" --store "$WORK/state" --session main --expected-revision 0 --turn-id t001 --user '我不喝咖啡。' --assistant '那换茶。' --scene daily
python3 "$SKILL/scripts/session.py" memory-add --profile "$WORK/profile.json" --store "$WORK/state" --session main --expected-revision 1 --memory-id m001 --text '用户说自己不喝咖啡' --subject user --provenance user_statement --source-turn t001 --quote '我不喝咖啡'
python3 "$SKILL/scripts/session.py" export --profile "$WORK/profile.json" --store "$WORK/state" --session main --out "$WORK/restore.json"
```

预期 revision 从工具返回读取，不能假定永远等于轮数（添加记忆、压缩也会递增）。文件锁在 Linux/WSL 使用 fcntl，在原生 Windows 使用 msvcrt 单字节互斥锁，争用最长等待 30 秒后报错，不静默跳过。标识不能使用 CON、NUL、COM1 等 Windows 保留设备名；跨平台均执行这一约束。

## 4. 评测

```bash
python3 "$SKILL/scripts/evaluate.py" cases --corpus "$WORK/corpus" --out "$WORK/cases.jsonl"
# 仅看 cases 和训练材料生成 candidates.jsonl：每行 {"case_id":"...","text":"..."}
python3 "$SKILL/scripts/evaluate.py" assess --corpus "$WORK/corpus" --candidates "$WORK/candidates.jsonl" --baseline "$WORK/baseline.jsonl" --out "$WORK/report.json"
python3 "$SKILL/scripts/evaluate.py" check --profile "$WORK/profile.json" --corpus "$WORK/corpus" --text-file "$WORK/reply.txt"
python3 "$SKILL/scripts/evaluate.py" blind-pack --cases "$WORK/cases.jsonl" --candidates "$WORK/candidates.jsonl" --baseline "$WORK/baseline.jsonl" --out-dir "$WORK/blind"
```

blind-score 需要 --ratings、--key、--out。ratings.jsonl 每行为 `{"evaluator":"实际评审代号","question_id":"q001","choice":"A"}`，choice 为 A/B/tie。不得伪造评分记录。只有收集实际评分之后运行。

自检：`python3 "$SKILL/scripts/test_tools.py"`。测试只在自动清理的临时目录写数据，不改安装文件、不调用外部服务。

## 5. Windows PowerShell

先确认 OpenClaw 网关实际运行的位置。Windows 桌面窗口可能连接 WSL 或远程网关，不能因此断定技能安装在 Windows 用户目录。原生 Windows 使用 PowerShell 路径和解释器；WSL 内仍用 Linux 路径和 python3。

原生 Windows 的工具命令示例（`$YujiSkill`、`$YujiWork` 均为实际绝对路径）：

```powershell
$YujiState = if ($env:OPENCLAW_STATE_DIR) { $env:OPENCLAW_STATE_DIR } else { Join-Path $env:USERPROFILE '.openclaw' }
$YujiSkill = Join-Path $YujiState 'skills\yuji-persona'
$YujiWork = Join-Path $env:USERPROFILE 'yuji-work'
New-Item -ItemType Directory -Force -Path $YujiWork | Out-Null
py -3 -X utf8 "$YujiSkill\scripts\corpus.py" build --input "$YujiSkill\assets\demo-corpus.jsonl" --target '林澈' --out "$YujiWork\corpus-v1"
py -3 -X utf8 "$YujiSkill\scripts\profile.py" init --corpus "$YujiWork\corpus-v1" --out "$YujiWork\profile.json"
py -3 -X utf8 "$YujiSkill\scripts\test_tools.py"
```

运行 init 后仍须按核心流程用训练语料补充规则并 validate；init 只创建基础档案。没有 py 命令时先验证 python 版本，然后把 `py -3 -X utf8` 换成 `python -X utf8`。PowerShell 的变量名不区分大小写，不要把 `$HOME` 或 `$home` 作为工作变量重新赋值。

Python CLI 输入/输出使用 UTF-8；写入的 JSON/JSONL 固定使用 LF，避免不同操作系统重写时引入换行差异。UTF-8 文本/JSONL 在 PowerShell 5.1 可用 `Set-Content -Encoding UTF8` 保存；工具接受 UTF-8 BOM。若终端显示乱码，先使用 `-X utf8` 并检查终端 UTF-8 编码，而不是改动语料中的汉字。

在 PowerShell 中运行 npm 提供的 OpenClaw 时，若仅因 openclaw.ps1 被执行策略阻止，可使用官方建议的 `openclaw.cmd`，不必全局放宽执行策略。
