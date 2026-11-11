# Compile Fast

用 **Python 3.10+** 实现的 Java 编译验证引擎，面向 Coding Agent。依据本目录的技术设计，优先落地真实 javac 验证与 agent skill。运行时没有第三方 Python 依赖，编译目标项目需要 JDK，Maven 模型解析需要 Maven。

## 已实现

- Git 变更识别：暂存、未暂存、未跟踪、删除和重命名；支持指定比较基准或直接传入 Agent Patch 文件列表。
- Maven effective POM / compile classpath 解析，包括模块、继承后的编译参数和 reactor 依赖。
- 模块级增量 javac：整模块批量编译；上游内容变化使下游失效；不使用旧 reactor class 文件掩盖删除/API 错误。
- SQLite 缓存：源文件、依赖内容、JDK、编译参数和构建模型参与 key；缓存输出校验；每次编译使用独立输出目录。
- JSON 反馈：`PASS / FAIL / UNKNOWN / INFRA_ERROR`，文件、行列、javac 错误码、实际范围、缓存证据和结果历史。
- 验证前后核对源码、模型、构建文件、依赖与输出；检测到修改返回 `UNKNOWN`，避免发布过期结果。
- 可独立安装的 agent skill，含 Python 引擎与有限次修复流程指引。

## 快速使用

在本仓库目录，不安装即可运行：

```powershell
python -m compile_fast --help
python -m compile_fast resolve-maven --project D:/path/to/java-project
python -m compile_fast verify --project D:/path/to/java-project
```

也可以 `python -m pip install .`，之后使用 `compile-fast` 命令。

解析 Maven 模型只运行帮助/依赖解析插件，不会自动执行 install、业务构建或测试。第一次解析可能下载固定版本的 Maven 插件和依赖。传递已有构建配置：

```powershell
python -m compile_fast resolve-maven --project D:/path/to/java-project --maven-arg=-Pdev --maven-arg=-Dcustom.property=value
python -m compile_fast verify --project D:/path/to/java-project --base main --change-id change-001 --agent coding-agent
```

依赖其他 reactor 模块时，Maven classpath 解析可能要求它们已存在于本地 Maven 仓库。解析失败返回 `UNKNOWN`；先执行项目原有的构建准备流程，或提供可靠的显式模型。验证时会移除解析出的 reactor JAR，以本次编译/内容匹配缓存的输出替代。

有效 POM 中 `--release` 模式目前支持 Maven Compiler Plugin 3.8+ 的 3.x 版本；不能明确复现的插件配置返回 `UNKNOWN`。请让 `--javac` 指向项目实际使用的 JDK，重新解析模型时保持 Maven profiles/settings 一致。

非 Git 项目或 Agent 已有精确变更列表：

```powershell
python -m compile_fast verify --project D:/path/to/java-project --changed module/src/main/java/demo/Api.java
# --changed 后不传文件：不依赖 Git，验证模型里的全部主源码。
python -m compile_fast verify --project D:/path/to/java-project --changed
```

显式变更列表不会缩小到仅编译这些文件。引擎核对所有模块内容，复用未变更模块，重编译变化模块及依赖它的模块。删除文件也应出现在变更列表中。

无 Maven 的普通源码工程：

```powershell
python -m compile_fast init --project D:/path/to/plain-project --release 17 --source-root src --confirm-context
python -m compile_fast verify --project D:/path/to/plain-project --changed
```

`--confirm-context` 表示你已确认源码目录、依赖、参数完整且不需要注解处理。依赖用重复的 `--classpath` 传入。不能确认时不要使用此参数；默认模板会保持 `UNKNOWN`。多模块模型格式见 [skill 模型说明](skills/compile-fast/references/model.md)。

## Agent 集成

```powershell
python scripts/install_skill.py
```

默认安装到 `$CODEX_HOME/skills/compile-fast`，未设置时为 `~/.codex/skills/compile-fast`。安装器打包当前 Python 引擎，并更新已有同名 skill。其他支持 `SKILL.md` 的 agent 可以通过 `--destination <agent-skills-directory>` 安装。新会话可使用 `$compile-fast`；实际入口为 `python <skill目录>/scripts/run.py`。

CLI stdout 是 JSON；退出码分别为 `0 / 1 / 2 / 3`。其他 agent 也可直接调用 CLI。`--output result.json` 可另存结果，历史记录保存在目标工作区 `.compile-fast/results/`。失败后按位置修复，再次验证；skill 要求同类错误连续三次后停止并上报。

## 验证与边界

```powershell
python -m unittest discover -s tests -v
# 可选真实 Maven 双模块联调：需要网络下载插件，使用工作区内独立 Maven 缓存。
python tests/maven_integration.py
```

测试使用实际 javac，覆盖跨模块方法删除、类型删除、接口签名错误、缓存复用与篡改、缺失依赖、处理器、Git 改名、并发修改检测、中文及空格路径。Maven 适配器另有可重复的模型测试。

本次在 Windows、Python 3.11、JDK 21、Maven 3.6.1 下完成了 20 项自动化测试及真实 Maven 双模块联调。联调验证 effective POM、reactor classpath、替换旧 reactor JAR、两模块缓存复用，以及删除上游方法后定位下游错误。联调报告写入 `.compile-fast/maven-integration-result.json`。这些结果来自小型夹具，不能据此推断大型业务项目的性能或全部插件兼容性。

当前版本已扩展 P1–P4 的执行链路：

| 能力 | 实现 |
| --- | --- |
| L1 语法与语义索引 | Tree-sitter 语法树；可配置 OpenRewrite LST，通过 Python/JPype 隔离进程获取类型、方法与引用位置 |
| ABI | Python 读取 class/JAR，覆盖签名、常量、继承、注解、record/sealed；未知属性保守失效 |
| 影响分析 | 候选引用传播与模块回退；实际 ABI 不变时复用下游缓存 |
| L3 | Maven clean verify / Gradle clean build，或自定义构建、UT/IT、启动检查命令 |
| 复杂编译上下文 | 注解处理、生成代码、JPMS 等自动升级真实构建；需要显式授权执行构建 |
| 服务 | HTTP 队列、工作区登记、Bearer 认证、持久任务、取消、旧任务取代、进程树终止 |
| Agent 门禁 | 变更归属、结果证据、三次同类失败停止建议、观测耗时与进程资源指标 |

## 新增命令

```powershell
python -m pip install ".[semantic,rewrite]"
python -m compile_fast resolve-gradle --project D:/project/java-app
python -m compile_fast index --project D:/project/java-app
python -m compile_fast impact --project D:/project/java-app
python -m compile_fast abi --path D:/project/java-app/target/classes --output abi.json
python -m compile_fast setup-rewrite --project D:/project/java-app
python -m compile_fast rewrite-index --project D:/project/java-app
python -m compile_fast verify --project D:/project/java-app --level auto --allow-build --changed
python -m compile_fast metrics --project D:/project/java-app
python -m compile_fast serve --workspace app=D:/project/java-app --allow-builds
```

`--level auto` 默认执行 L1/L2；加 `--allow-build` 后 PASS 或 UNKNOWN 继续进入 L3。`--level L3 --allow-build` 可直接执行完整门禁。`--patch change.diff` 读取补丁路径但不应用补丁；`--change-record changes.json` 接收 `changedFiles` 或 `changes[{beforePath,afterPath}]`。

HTTP 默认监听 127.0.0.1:8765，启动时输出令牌文件路径。请求头设置 `Authorization: Bearer <令牌>`。支持 `POST /api/verifications`、`GET /api/verifications/{id}`、`GET /api/verifications/{id}/diagnostics`、`POST /api/verifications/{id}/cancel`。提交示例：

```json
{"workspaceId":"app","changedFiles":[],"verificationLevel":"auto","allowBuild":true,"changeId":"change-1","agent":"agent-1"}
```

可传 `revision`（当前完整 commit hash）及 `projectModelVersion` 约束上下文。服务仅接受已登记工作区；同一工作区串行执行，新任务使旧任务过期，不同工作区可并行。重启后的未完成任务被标记中断，需要重新提交。

自定义 `.compile-fast/gates.json` 为命令数组，不经过 shell：

```json
[{"name":"build-and-tests","command":["mvn","-B","clean","verify"]}]
```

可追加项目自己的 Spring 启动、IT 或业务回归命令。PASS 只证明实际配置并执行的门禁；不会自动推断业务正确性。源码语义图仍可能缺边，因此不以图裁剪替代完整的模块验证。OpenRewrite 使用固定 8.63.0 运行时，解析缓存绑定源码、classpath 与 JDK。核心引擎不含自定义 Java 源码，Gradle 文件仅用于读取构建模型。

额外真实联调：`tests/maven_integration.py`、`tests/gradle_integration.py`、`tests/maven_processor_integration.py`、`tests/rewrite_integration.py --runtime-config <rewrite.json>`。Gradle 需在 PATH；OpenRewrite 需先 setup-rewrite。资源指标仅覆盖启动进程本身，未计入构建工具的全部后代进程，不应当作完整系统资源消耗。

Maven settings、外部父 POM、远程 SNAPSHOT、profiles 或工具链变更后应重新解析；本地模型不会自动探测远端变化。缓存按内容完整哈希，可靠性优先，未承诺毫秒级性能。缓存输出与结果记录目前保留；没有自动垃圾回收，长期使用可在无验证任务运行时清理目标项目 `.compile-fast/cache` 和历史 results。

实现参考：[javac 官方参数说明](https://docs.oracle.com/en/java/javase/21/docs/specs/man/javac.html)、[Maven effective-pom](https://maven.apache.org/plugins/maven-help-plugin/effective-pom-mojo.html)、[Maven build-classpath](https://maven.apache.org/plugins/maven-dependency-plugin/build-classpath-mojo.html)。解析器固定使用 help 3.3.0、dependency 3.6.1，以兼容本机 Maven 3.6.1；版本要求见 [Help 插件兼容表](https://maven.apache.org/plugins/maven-help-plugin/plugin-info.html) 和 [Dependency 插件兼容表](https://maven.apache.org/plugins/maven-dependency-plugin/plugin-info.html)。
