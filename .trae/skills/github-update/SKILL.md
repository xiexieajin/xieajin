---
name: "github-update"
description: "将本地代码变更推送到GitHub仓库（xiexieajin/xieajin）。当用户说'更新GitHub'、'推送代码'、'同步代码'、'上传代码'、'提交代码到GitHub'时触发。自动完成检查变更、提交、推送全流程。"
---

# GitHub 仓库更新技能

## 用途
帮助用户将本地代码变更快速推送到 GitHub 仓库 `xiexieajin/xieajin`，无需手动输入 Git 命令。

## 触发条件
当用户表达以下意图时激活本技能：
- "更新GitHub" / "更新仓库"
- "推送代码" / "push代码"
- "同步代码" / "同步到GitHub"
- "上传代码" / "提交代码"
- "帮我更新到GitHub"
- 任何涉及将本地变更推送到远程GitHub仓库的请求

## 执行流程

### 第1步：安全检查（必须执行）
在推送前，必须确认敏感文件不会被上传：

```powershell
# 检查 .gitignore 是否仍然保护敏感文件
git -C 'd:\pycharm\供应商寻源系统' status --short
```

**必须确认以下文件不在提交列表中（即没有被 git 跟踪）：**
- `config.py`（包含真实API密钥和数据库密码）
- `instance/` 目录（数据库文件）
- `workspace/` 目录（认证token等运行时数据）
- `__pycache__/` 目录（编译缓存）

如果发现上述文件出现在 `git status` 的待提交列表中，**立即停止**并提醒用户，不要继续推送。

### 第2步：查看变更内容
```powershell
# 查看具体改了哪些文件
git -C 'd:\pycharm\供应商寻源系统' status --short
git -C 'd:\pycharm\供应商寻源系统' diff --stat
```

向用户简要展示改动了哪些文件，让用户确认是否要推送。

### 第3步：添加变更并提交
```powershell
# 添加所有变更（.gitignore 会自动过滤敏感文件）
git -C 'd:\pycharm\供应商寻源系统' add -A

# 提交变更，commit 信息根据实际改动自动生成
# 如果用户说明了改了什么，用用户的描述作为 commit 信息
# 如果用户没说，根据 diff 内容自动总结
git -C 'd:\pycharm\供应商寻源系统' commit -m "提交信息"
```

**commit 信息规则：**
- 如果用户说了改了什么 → 用用户的话作为 commit 信息
- 如果用户没说 → 根据 diff 内容自动总结，格式如："更新：xxx模块，修复xxx问题"
- 始终用中文写 commit 信息

### 第4步：推送到GitHub
```powershell
# 推送到远程 main 分支
git -C 'd:\pycharm\供应商寻源系统' push origin master:main
```

如果推送失败，根据错误信息处理：
- **认证过期**：运行 `& 'C:\Program Files\GitHub CLI\gh.exe' auth login --web` 重新认证
- **远程有新提交**：先 `git pull --rebase origin main` 再 push
- **其他错误**：展示错误信息给用户，给出解决建议

### 第5步：确认结果
推送成功后，告诉用户：
- 推送了哪些文件
- 仓库地址：https://github.com/xiexieajin/xieajin
- 可以点击链接查看在线版本

## 仓库信息
- **仓库地址**：https://github.com/xiexieajin/xieajin
- **GitHub用户名**：xiexieajin
- **本地分支**：master
- **远程分支**：main
- **项目路径**：d:\pycharm\供应商寻源系统

## 认证信息
- GitHub CLI 已安装在 `C:\Program Files\GitHub CLI\gh.exe`
- 凭据已通过 `gh auth setup-git` 配置
- 如果认证过期，运行：`& 'C:\Program Files\GitHub CLI\gh.exe' auth login --web`

## 安全红线
1. **config.py 绝对不能上传**（包含智谱、DeepSeek、天眼查、1688的API密钥和MySQL密码）
2. **instance/ 目录绝对不能上传**（包含数据库文件）
3. **workspace/ 目录绝对不能上传**（包含认证token）
4. 每次推送前必须执行安全检查，确认上述文件未被跟踪

## 小白说明
这个技能就是帮你把改好的代码"快递"到GitHub上。流程是：
1. 先检查一下要寄的东西里有没有不该寄的（密钥、数据库等敏感信息）
2. 把要寄的东西打包（git add + commit）
3. 寄出去（git push）
4. 确认对方收到了

你只需要说"更新GitHub"，剩下的交给我自动完成！
