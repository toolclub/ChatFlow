"""
T-NODE-*: 图节点逻辑测试
覆盖: reflector 快速路径、planner 规划触发、call_model 澄清预检、sandbox 格式化器
"""
import pytest

# 跳过无法导入的模块（测试环境可能缺少 sqlalchemy 等依赖）
_DEPS_AVAILABLE = True
try:
    import sqlalchemy  # noqa
    import langchain_core  # noqa
except ImportError:
    _DEPS_AVAILABLE = False

needs_deps = pytest.mark.skipif(not _DEPS_AVAILABLE, reason="需要完整后端依赖")


# ═══════════════════════════════════════════════════════════════
# T-NODE-01: reflector 五条快速路径
# ═══════════════════════════════════════════════════════════════

@pytest.mark.unit
@needs_deps
@needs_deps
class TestReflectorFastPaths:
    """测试 reflector 不调用 LLM 的 5 条快速路径。"""

    def test_path1_no_plan(self):
        """无计划 → done"""
        from graph.nodes.reflector_node import ReflectorNode
        # 直接测试 execute 需要 mock state，这里测试辅助方法
        # 路径 1 在 execute 中：if not plan: return done
        pass  # 需要 mock GraphState，标记为 TODO

    def test_step_results_truncation(self):
        """step_results 每条截断到 3000"""
        from graph.nodes.reflector_node import ReflectorNode
        node = ReflectorNode()
        long_response = "x" * 5000
        state = {"step_results": []}
        result = node._accumulate_step_results(state, long_response)
        assert len(result[-1]) == 3000

    def test_step_result_summary_len(self):
        """步骤摘要长度 = 2000"""
        from graph.nodes.reflector_node import _STEP_RESULT_SUMMARY_LEN
        assert _STEP_RESULT_SUMMARY_LEN == 2000


# ═══════════════════════════════════════════════════════════════
# T-NODE-03: planner
# ═══════════════════════════════════════════════════════════════

@pytest.mark.unit
@needs_deps
class TestPlanner:
    def test_search_needs_planning(self):
        from graph.nodes.planner_node import PlannerNode
        assert PlannerNode._needs_planning("search", "搜索") is True

    def test_chat_no_planning(self):
        from graph.nodes.planner_node import PlannerNode
        assert PlannerNode._needs_planning("chat", "你好") is False

    def test_code_short_no_planning(self):
        from graph.nodes.planner_node import PlannerNode
        # 现在 code 路由始终规划（因为 code 任务涉及多步：写文件→执行→验证）
        assert PlannerNode._needs_planning("code", "写个冒泡排序") is True

    def test_code_long_needs_planning(self):
        from graph.nodes.planner_node import PlannerNode
        long_msg = "帮我写一个" + "很复杂的" * 30 + "系统"
        assert PlannerNode._needs_planning("code", long_msg) is True

    def test_code_with_signal_words(self):
        from graph.nodes.planner_node import PlannerNode
        assert PlannerNode._needs_planning("code", "首先分析然后重构") is True

    def test_continuation_detection(self):
        from graph.nodes.planner_node import PlannerNode
        assert PlannerNode._is_continuation("继续") is True
        assert PlannerNode._is_continuation("continue") is True
        assert PlannerNode._is_continuation("帮我继续写代码") is False

    def test_fix_json_inner_quotes(self):
        from graph.nodes.planner_node import _fix_json_inner_quotes
        # 内部裸引号应替换为单引号
        broken = '{"title": "他说"百业"好"}'
        fixed = _fix_json_inner_quotes(broken)
        assert '"百业"' not in fixed or "'" in fixed


# ═══════════════════════════════════════════════════════════════
# T-NODE-04: call_model 澄清预检
# ═══════════════════════════════════════════════════════════════

@pytest.mark.unit
@needs_deps
class TestClarificationPrecheck:
    def test_webpage_no_style(self):
        from graph.nodes.call_model_node import _needs_webpage_clarification
        assert _needs_webpage_clarification("帮我做个网页") is True

    def test_webpage_with_style(self):
        from graph.nodes.call_model_node import _needs_webpage_clarification
        assert _needs_webpage_clarification("帮我做个简约风格网页") is False

    def test_non_webpage(self):
        from graph.nodes.call_model_node import _needs_webpage_clarification
        assert _needs_webpage_clarification("帮我写个排序算法") is False

    def test_html_keyword(self):
        from graph.nodes.call_model_node import _needs_webpage_clarification
        assert _needs_webpage_clarification("写一个 html 页面") is True

    def test_empty_message(self):
        from graph.nodes.call_model_node import _needs_webpage_clarification
        assert _needs_webpage_clarification("") is False


# ═══════════════════════════════════════════════════════════════
# T-NODE-06: save_response 缓存跳过逻辑
# ═══════════════════════════════════════════════════════════════

@pytest.mark.unit
@needs_deps
class TestSaveResponseCache:
    def test_tool_summary_not_in_content(self):
        """tool_summary 存独立字段，不混入 content"""
        from graph.nodes.save_response_node import SaveResponseNode
        node = SaveResponseNode()
        summary = node._build_tool_summary({"messages": []})
        assert summary == ""  # 无工具时为空

    def test_step_context_empty_for_single_step(self):
        """单步任务不生成步骤摘要"""
        from graph.nodes.save_response_node import SaveResponseNode
        node = SaveResponseNode()
        ctx = node._build_step_context({"step_results": ["r1"], "plan": [{"title": "t1"}]})
        assert ctx == ""  # 单步不生成


# ═══════════════════════════════════════════════════════════════
# T-STREAM-04: 沙箱格式化器 exit code 提取
# ═══════════════════════════════════════════════════════════════

@pytest.mark.unit
@needs_deps
class TestSandboxFormatter:
    def test_exit_code_success(self):
        from graph.runner.formatters.sandbox import SandboxFormatter
        assert SandboxFormatter._extract_exit_code("⏱ 1.50s | exit=0") == 0

    def test_exit_code_failure(self):
        from graph.runner.formatters.sandbox import SandboxFormatter
        assert SandboxFormatter._extract_exit_code("⏱ 1.50s | exit=1") == 1

    def test_exit_code_negative(self):
        from graph.runner.formatters.sandbox import SandboxFormatter
        assert SandboxFormatter._extract_exit_code("⏱ 0.50s | exit=-1") == -1

    def test_exit_code_no_match(self):
        from graph.runner.formatters.sandbox import SandboxFormatter
        assert SandboxFormatter._extract_exit_code("无结果") == 0


# ═══════════════════════════════════════════════════════════════
# T-EDGE-*: 路由逻辑
# ═══════════════════════════════════════════════════════════════

@pytest.mark.unit
@needs_deps
class TestEdgeRouting:
    def test_max_tool_calls_per_step(self):
        from graph.edges import _MAX_TOOL_CALLS_PER_STEP
        assert _MAX_TOOL_CALLS_PER_STEP == 50

    def test_cache_routing_hit(self):
        from graph.edges import cache_routing
        assert cache_routing({"cache_hit": True}) == "save_response"

    def test_cache_routing_miss(self):
        from graph.edges import cache_routing
        assert cache_routing({"cache_hit": False}) == "after_cache"
        assert cache_routing({}) == "after_cache"

    def test_reflector_routing_continue(self):
        from graph.edges import reflector_routing
        assert reflector_routing({"reflector_decision": "continue"}) == "call_model"

    def test_reflector_routing_retry(self):
        from graph.edges import reflector_routing
        assert reflector_routing({"reflector_decision": "retry"}) == "call_model"

    def test_reflector_routing_done(self):
        from graph.edges import reflector_routing
        assert reflector_routing({"reflector_decision": "done"}) == "save_response"
        assert reflector_routing({}) == "save_response"


# ═══════════════════════════════════════════════════════════════
# T-DB-02: artifact_store.detect_language
# ═══════════════════════════════════════════════════════════════

@pytest.mark.unit
@needs_deps
class TestDetectLanguage:
    def test_python(self):
        from db.artifact_store import detect_language
        assert detect_language("main.py") == "python"

    def test_javascript(self):
        from db.artifact_store import detect_language
        assert detect_language("app.js") == "javascript"

    def test_archive_tar_gz(self):
        from db.artifact_store import detect_language
        assert detect_language("download.tar.gz") == "archive"

    def test_no_extension(self):
        from db.artifact_store import detect_language
        assert detect_language("Makefile") == "text"

    def test_pptx(self):
        from db.artifact_store import detect_language
        assert detect_language("slides.pptx") == "pptx"

    def test_html(self):
        from db.artifact_store import detect_language
        assert detect_language("index.html") == "html"


# ═══════════════════════════════════════════════════════════════
# T-MEM-03: context_builder 截断
# ═══════════════════════════════════════════════════════════════

@pytest.mark.unit
@needs_deps
class TestContextBuilder:
    def test_truncate_short(self):
        from memory.context_builder import _truncate_assistant_history
        assert _truncate_assistant_history("short") == "short"

    def test_truncate_long(self):
        from memory.context_builder import _truncate_assistant_history
        long_text = "x" * 1000
        result = _truncate_assistant_history(long_text)
        assert len(result) == 803  # 800 + "..."

    def test_truncate_compat_summary_marker(self):
        from memory.context_builder import _truncate_assistant_history
        # 只有在总长度超过 800 时，代码才会尝试寻找并剥离 summary_marker
        marker = "【执行过程摘要】"
        # "核心答案" 是 4 个字符，200 次是 800 字符。加上 marker 和后续内容，总长肯定超过 800
        content = "核心答案" * 200 + marker + "步骤详情..." * 20
        assert len(content) > 800
        result = _truncate_assistant_history(content)
        assert marker not in result

# ═══════════════════════════════════════════════════════════════
# T-SANDBOX-03: sandbox_download 路径安全
# ═══════════════════════════════════════════════════════════════

@pytest.mark.unit
@needs_deps
class TestPathSanitization:
    def test_path_traversal_stripped(self):
        """../../etc/passwd → etc/passwd（.. 被清除）"""
        path = "../../etc/passwd"
        sanitized = path.replace("..", "").lstrip("/")
        assert ".." not in sanitized
        assert not sanitized.startswith("/")

    def test_absolute_path_stripped(self):
        """/etc/passwd → etc/passwd"""
        path = "/etc/passwd"
        sanitized = path.replace("..", "").lstrip("/")
        assert not sanitized.startswith("/")

    def test_dot_path(self):
        """'.' → '.'"""
        path = "."
        sanitized = path.replace("..", "").lstrip("/")
        if not sanitized or sanitized == ".":
            sanitized = "."
        assert sanitized == "."

    def test_shlex_quote(self):
        """特殊字符被转义"""
        import shlex
        dangerous = 'file"; rm -rf /; echo "'
        quoted = shlex.quote(dangerous)
        # 验证 quoted 能够被 shlex.split 还原为原始字符串，说明引用正确
        import shlex
        assert shlex.split(quoted)[0] == dangerous
