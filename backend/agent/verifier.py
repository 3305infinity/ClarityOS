"""
Moonwalk — Tool Verifier V2
============================
Verifies that tool executions succeeded by checking results and world state.
Each tool type has a specific verification strategy.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Any
from functools import partial

print = partial(print, flush=True)


# ═══════════════════════════════════════════════════════════════
#  Tool Classification — drives verification strategy
# ═══════════════════════════════════════════════════════════════

from agent.constants import UI_MUTATING_TOOLS as _UI_MUTATING_TOOLS
from agent.constants import READ_ONLY_TOOLS as _READ_ONLY_TOOLS


def _looks_like_ui_lookup_failure(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    failure_markers = (
        "no ui element matching",
        "no text field matching",
        "no close match for",
        "available elements:",
        "visible elements:",
        "not expose it via accessibility",
        "try read_screen",
        "error in click_ui",
        "error in type_in_field",
        "failed to type text",
        "failed to paste text",
    )
    return any(marker in normalized for marker in failure_markers)


# ═══════════════════════════════════════════════════════════════
#  Verification Result
# ═══════════════════════════════════════════════════════════════

@dataclass
class VerificationResult:
    """Result of verifying a tool execution."""
    success: bool
    confidence: float       # 0.0 - 1.0
    message: str
    should_retry: bool = False
    suggested_fix: Optional[str] = None
    
    def __repr__(self):
        status = "✓" if self.success else "✗"
        return f"VerificationResult({status} conf={self.confidence:.0%}: {self.message})"


# ═══════════════════════════════════════════════════════════════
#  Tool Verifier Class
# ═══════════════════════════════════════════════════════════════

class ToolVerifier:
    """
    Verifies that tool executions succeeded.
    Uses tool-specific strategies for accurate verification.
    """
    
    # Error patterns that indicate failure.
    # These are applied only to tool *error messages*, not to page content
    # returned by read-only tools.  Patterns are anchored or scoped to
    # reduce false positives from natural-language content.
    ERROR_PATTERNS = [
        r"^error\b",           # starts with "error"
        r"\bfailed to\b",     # "failed to …"
        r"\bexception\b",
        r"\bpermission denied\b",
        r"\bno such file\b",
        r"\bcommand not found\b",
        r"\bunable to\b",
        r"\btimeout\b",
    ]

    # Tools whose results are user/page *content* — never run
    # ERROR_PATTERNS against them because the words "error" or
    # "invalid" may appear naturally in the text.
    _CONTENT_TOOLS: frozenset[str] = frozenset({
        "read_file", "browser_read_page", "browser_read_text",
        "read_page_content", "get_page_summary", "web_scrape",
        "get_web_information", "gdocs_read", "gsheets_read",
        "gworkspace_analyze", "extract_structured_data",
        "run_shell",
    })

    def __init__(self):
        """Initialize with tool-specific verifiers."""
        # Map tool names to verification methods
        self._verifiers: Dict[str, Callable] = {
            "open_app": self._verify_open_app,
            "quit_app": self._verify_quit_app,
            "close_window": self._verify_close_window,
            "open_url": self._verify_open_url,
            "browser_snapshot": self._verify_browser_snapshot,
            "browser_find": self._verify_browser_find,
            "browser_click_ref": self._verify_browser_ref_action,
            "browser_type_ref": self._verify_browser_ref_action,
            "browser_select_ref": self._verify_browser_ref_action,
            "browser_click_match": self._verify_browser_click_match,
            "browser_read_page": self._verify_browser_read_page,
            "browser_read_text": self._verify_browser_read_text,
            "browser_scroll": self._verify_browser_scroll,
            "read_page_content": self._verify_read_page_content,
            "extract_structured_data": self._verify_extract_structured_data,
            "find_and_act": self._verify_find_and_act,
            "get_page_summary": self._verify_get_page_summary,
            "web_scrape": self._verify_web_scrape,
            "web_search": self._verify_web_search,
            "get_web_information": self._verify_get_web_information,
            "gdocs_create": self._verify_gdocs_create,
            "gdocs_append": self._verify_gdocs_append,
            "browser_assert": self._verify_browser_assert,
            "browser_wait_for": self._verify_browser_assert,
            "run_shell": self._verify_run_shell,
            "read_file": self._verify_read_file,
            "write_file": self._verify_write_file,
            "get_ui_tree": self._verify_get_ui_tree,
            "click_ui": self._verify_click_ui,
            "type_in_field": self._verify_type_in_field,
            "type_text": self._verify_type_text,
            "press_key": self._verify_press_key,
            "click_element": self._verify_click_element,
            "play_media": self._verify_play_media,
            "run_shortcut": self._verify_run_shortcut,
        }

    async def verify(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: str,
        success_criteria: str = "",
        get_current_state: Optional[Callable] = None
    ) -> VerificationResult:
        """
        Verify that a tool execution succeeded.
        
        Args:
            tool_name: Name of the tool that was executed
            tool_args: Arguments passed to the tool
            tool_result: Result string from the tool
            success_criteria: Expected success condition (from plan)
            get_current_state: Optional async function to get current desktop state
            
        Returns:
            VerificationResult with success status and details
        """
        # Use tool-specific verifier if available
        verifier = self._verifiers.get(tool_name)
        if verifier:
            try:
                return await verifier(tool_args, tool_result, success_criteria, get_current_state)
            except Exception as e:
                print(f"[Verifier] Error in {tool_name} verifier: {e}")
                # Fall back to default

        # For content-returning tools, skip generic error check —
        # file contents and command output may naturally contain words like
        # "timeout", "error", "invalid" that are NOT actual failures.
        if tool_name not in self._CONTENT_TOOLS:
            error_check = self._check_for_errors(tool_result)
            if error_check:
                return error_check
        
        # Default verification: trust the result if no errors
        return VerificationResult(
            success=True,
            confidence=0.7,
            message=f"Tool {tool_name} executed (no specific verification)"
        )

    async def verify_with_visual(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: str,
        success_criteria: str = "",
        get_current_state: Optional[Callable] = None,
        get_visual_state: Optional[Callable] = None,
    ) -> VerificationResult:
        """Verify tool execution with optional visual/DOM confirmation.

        - UI-mutating tools always trigger visual verification.
        - Read-only tools use string-based verification only.
        """
        # Step 1: standard string-based verification
        string_result = await self.verify(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            success_criteria=success_criteria,
            get_current_state=get_current_state,
        )

        # Step 2: if read-only tool, skip visual verification entirely
        if tool_name in _READ_ONLY_TOOLS:
            return string_result

        if tool_name == "gdocs_create" and string_result.success:
            return string_result

        # Step 3: confidence-gate — skip expensive visual LLM call when
        # string verification already has high confidence + clear success.
        # This saves ~1.5-3.5 s per UI-mutating tool call.
        if string_result.success and string_result.confidence >= 0.85:
            print(f"[Verifier] ⚡ High-confidence skip for {tool_name} "
                  f"(conf={string_result.confidence:.0%})")
            return string_result

        # Step 4: if UI-mutating tool, run visual verification
        if tool_name in _UI_MUTATING_TOOLS and get_visual_state is not None:
            try:
                visual_summary = await get_visual_state()
                if visual_summary:
                    visual_result = await self._evaluate_visual_evidence(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        string_result=string_result,
                        visual_summary=visual_summary,
                    )
                    if visual_result is not None:
                        return visual_result
            except Exception as e:
                print(f"[Verifier] ⚠ Visual verification failed for {tool_name}: {e}")

        return string_result

    async def verify_milestone(
        self,
        milestone_goal: str,
        success_signal: str,
        get_visual_state: Optional[Callable] = None,
    ) -> VerificationResult:
        """Verify milestone completion using visual/DOM state.

        Called when the LLM declares done=true to gate completion
        on real screen evidence.
        """
        if get_visual_state is None:
            return VerificationResult(
                success=True, confidence=0.6,
                message="No visual checker available — trusting LLM completion claim"
            )

        try:
            visual_summary = await get_visual_state()
            if not visual_summary:
                return VerificationResult(
                    success=True, confidence=0.6,
                    message="Visual state unavailable — trusting LLM completion claim"
                )

            return await self._evaluate_milestone_visual(
                milestone_goal=milestone_goal,
                success_signal=success_signal,
                visual_summary=visual_summary,
            )
        except Exception as e:
            print(f"[Verifier] ⚠ Milestone visual verification error: {e}")
            return VerificationResult(
                success=True, confidence=0.5,
                message=f"Milestone visual check failed ({e}) — trusting LLM"
            )

    async def _evaluate_visual_evidence(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        string_result: VerificationResult,
        visual_summary: str,
    ) -> Optional[VerificationResult]:
        """Use visual evidence to override string-based verdict for UI tools."""
        try:
            from providers.router import ModelRouter
            router = _get_shared_router()

            args_preview = str(tool_args)[:200]
            prompt = (
                f"Tool `{tool_name}` was called with args: {args_preview}\n"
                f"String result says: success={string_result.success}, "
                f"message='{string_result.message[:150]}'\n"
                f"Current screen/DOM state:\n{visual_summary[:1500]}\n\n"
                f"Based on the visual state, did the action actually succeed? "
                f"Reply with ONLY 'YES' or 'NO' followed by a single-sentence reason."
            )

            import re
            import os
            image_bytes = None
            match = re.search(r"Screenshot(?: captured)?: ([^\n]+)", visual_summary)
            if match:
                screenshot_path = match.group(1).strip()
                if os.path.exists(screenshot_path):
                    try:
                        with open(screenshot_path, "rb") as f:
                            image_bytes = f.read()
                    except Exception as e:
                        print(f"[Verifier] ⚠ Failed to read screenshot bytes: {e}")

            response = await router.route_and_call(
                user_message=prompt,
                system_prompt="You are a verification agent. Be concise.",
                force_tier="fast",
                image_data=image_bytes,
            )

            if response and response.text:
                answer = response.text.strip().upper()
                visual_says_yes = answer.startswith("YES")
                reason = response.text.strip()[3:].strip().lstrip(":.— ") or "visual check"

                # Visual disagrees with string — visual wins
                if visual_says_yes != string_result.success:
                    print(f"[Verifier] 👁 Visual override for {tool_name}: "
                          f"{string_result.success} → {visual_says_yes} ({reason[:80]})")
                    return VerificationResult(
                        success=visual_says_yes,
                        confidence=0.85,
                        message=f"Visual verification: {reason[:150]}",
                        should_retry=not visual_says_yes,
                    )
                # Visual agrees — boost confidence
                return VerificationResult(
                    success=string_result.success,
                    confidence=min(1.0, string_result.confidence + 0.1),
                    message=string_result.message,
                    should_retry=string_result.should_retry,
                    suggested_fix=string_result.suggested_fix,
                )
        except Exception as e:
            print(f"[Verifier] ⚠ Visual evaluation LLM call failed: {e}")
        return None

    async def _evaluate_milestone_visual(
        self,
        milestone_goal: str,
        success_signal: str,
        visual_summary: str,
    ) -> VerificationResult:
        """Ask Flash LLM if milestone is visually complete."""
        try:
            from providers.router import ModelRouter
            router = _get_shared_router()

            prompt = (
                f"Milestone goal: '{milestone_goal}'\n"
                f"Success signal: '{success_signal}'\n"
                f"Current screen/DOM state:\n{visual_summary[:2000]}\n\n"
                f"Is this milestone complete based on what the screen shows? "
                f"Reply with ONLY 'YES' or 'NO' followed by a single-sentence reason."
            )

            import re
            import os
            image_bytes = None
            match = re.search(r"Screenshot(?: captured)?: ([^\n]+)", visual_summary)
            if match:
                screenshot_path = match.group(1).strip()
                if os.path.exists(screenshot_path):
                    try:
                        with open(screenshot_path, "rb") as f:
                            image_bytes = f.read()
                    except Exception as e:
                        print(f"[Verifier] ⚠ Failed to read milestone screenshot: {e}")

            response = await router.route_and_call(
                user_message=prompt,
                system_prompt="You are a milestone verification agent. Be concise.",
                force_tier="fast",
                image_data=image_bytes,
            )

            if response and response.text:
                answer = response.text.strip().upper()
                confirmed = answer.startswith("YES")
                reason = response.text.strip()[3:].strip().lstrip(":.— ") or "visual check"

                return VerificationResult(
                    success=confirmed,
                    confidence=0.85 if confirmed else 0.4,
                    message=f"Milestone visual check: {reason[:200]}",
                    should_retry=not confirmed,
                )
        except Exception as e:
            print(f"[Verifier] ⚠ Milestone visual LLM call failed: {e}")

        return VerificationResult(
            success=True, confidence=0.5,
            message="Milestone visual LLM unavailable — trusting completion claim"
        )

    def _check_for_errors(self, result: str) -> Optional[VerificationResult]:
        """Check for error patterns in the result."""
        if not result:
            return None

        parsed: Optional[dict] = None
        try:
            candidate = json.loads(result)
            if isinstance(candidate, dict):
                parsed = candidate
        except (TypeError, ValueError):
            parsed = None

        if parsed is not None:
            ok = parsed.get("ok")
            error_code = str(parsed.get("error_code", "") or "").strip()
            message = str(parsed.get("message", "") or parsed.get("error", "") or "").strip()

            if ok is True and not error_code:
                return None

            if ok is False or error_code:
                error_text = " ".join(part for part in (error_code, message) if part).strip() or result[:100]
                return self._build_error_result(error_text)

            # For successful structured payloads without an explicit ok flag,
            # only inspect the dedicated message/error fields rather than the
            # entire JSON blob. This avoids false failures from internal
            # metadata like route_decision_reason="...timed out...".
            if message:
                return self._scan_error_text(message)
            return None

        return self._scan_error_text(result)

    def _scan_error_text(self, text: str) -> Optional[VerificationResult]:
        result_lower = text.lower()
        
        for pattern in self.ERROR_PATTERNS:
            if re.search(pattern, result_lower):
                return self._build_error_result(text)
        
        return None

    def _build_error_result(self, error_text: str) -> VerificationResult:
        error_lower = (error_text or "").lower()
        retryable = any(p in error_lower for p in [
            "timeout", "network",
            "econnreset", "unreachable", "connection", "retry",
            "temporarily", "unavailable", "econnrefused"
        ])
        if any(p in error_lower for p in ["no such file", "filenotfounderror", "does not exist"]):
            retryable = False

        return VerificationResult(
            success=False,
            confidence=0.9,
            message=f"Error detected: {str(error_text)[:100]}",
            should_retry=retryable,
            suggested_fix=self._suggest_fix(error_lower),
        )

    def _suggest_fix(self, error_text: str) -> Optional[str]:
        """Suggest a fix based on the error."""
        if "not found" in error_text:
            return "Check if the target exists or is installed"
        if "permission" in error_text:
            return "May need elevated permissions"
        if "timeout" in error_text:
            return "Try increasing wait time"
        return None

    # ═══════════════════════════════════════════════════════════════
    #  Tool-Specific Verifiers
    # ═══════════════════════════════════════════════════════════════

    async def _verify_open_app(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that an app was opened."""
        target_app = args.get("app_name", "").lower()
        result_lower = result.lower()

        if "couldn't find" in result_lower or "could not find" in result_lower or "error" in result_lower:
            return VerificationResult(
                success=False,
                confidence=0.9,
                message=f"Error detected: {result}",
                should_retry=False,
            )
        
        # Check if result indicates success
        if "successfully" in result_lower or "opened" in result_lower or "launched" in result_lower:
            # Double-check by getting current active app if possible
            if get_state:
                try:
                    state = await get_state()
                    current_app = state.get("active_app", "").lower()
                    if target_app in current_app or current_app in target_app:
                        return VerificationResult(
                            success=True,
                            confidence=0.95,
                            message=f"{target_app} is now the active app"
                        )
                    else:
                        # State check says a DIFFERENT app is active —
                        # lower confidence so visual verification triggers.
                        return VerificationResult(
                            success=True,
                            confidence=0.7,
                            message=f"open_app reported success but active app is '{current_app}', not '{target_app}'",
                            should_retry=True,
                        )
                except Exception:
                    pass
            
            # No state checker available — lower confidence to trigger visual verification
            return VerificationResult(
                success=True,
                confidence=0.75,
                message=f"open_app reported success for {target_app} (unverified)"
            )
        
        return VerificationResult(
            success=False,
            confidence=0.7,
            message=f"Could not confirm {target_app} opened",
            should_retry=True
        )

    async def _verify_quit_app(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that an app was quit."""
        target_app = args.get("app_name", "")
        
        if "quit" in result.lower() or "closed" in result.lower() or "success" in result.lower():
            return VerificationResult(
                success=True,
                confidence=0.85,
                message=f"{target_app} quit successfully"
            )
        
        return VerificationResult(
            success=True,  # Quitting is usually successful even with vague results
            confidence=0.7,
            message=f"Quit command sent to {target_app}"
        )

    async def _verify_close_window(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a window was closed."""
        if "closed" in result.lower() or "success" in result.lower():
            return VerificationResult(
                success=True,
                confidence=0.85,
                message="Window closed"
            )
        
        return VerificationResult(
            success=True,
            confidence=0.7,
            message="Close window command sent"
        )

    async def _verify_open_url(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a URL was opened."""
        target_url = args.get("url", "")
        
        if "opened" in result.lower() or "success" in result.lower():
            # Try to verify browser URL if possible
            if get_state:
                try:
                    state = await get_state()
                    current_url = state.get("browser_url", "")
                    # Check if domain matches
                    target_domain = self._extract_domain(target_url)
                    current_domain = self._extract_domain(current_url)
                    if target_domain and current_domain and target_domain in current_domain:
                        return VerificationResult(
                            success=True,
                            confidence=0.95,
                            message=f"Browser at {current_url}"
                        )
                except Exception:
                    pass
            
            return VerificationResult(
                success=True,
                confidence=0.85,
                message=f"URL opened: {target_url}"
            )
        
        return VerificationResult(
            success=False,
            confidence=0.6,
            message="Could not confirm URL opened",
            should_retry=True
        )

    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL."""
        import re
        match = re.search(r'(?:https?://)?(?:www\.)?([^/]+)', url)
        return match.group(1) if match else ""

    async def _verify_browser_snapshot(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        if '"generation"' in result and '"url"' in result:
            return VerificationResult(
                success=True,
                confidence=0.9,
                message="Browser snapshot is available"
            )
        return VerificationResult(
            success=False,
            confidence=0.85,
            message="Browser snapshot missing or malformed",
            should_retry=False
        )

    async def _verify_browser_find(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        if '"candidates"' in result:
            return VerificationResult(
                success=True,
                confidence=0.85,
                message="Browser candidate list returned"
            )
        return VerificationResult(
            success=False,
            confidence=0.8,
            message="Browser candidate list missing",
            should_retry=False
        )

    async def _verify_browser_ref_action(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        if '"ok": true' in result.lower() and '"verification"' in result.lower():
            return VerificationResult(
                success=True,
                confidence=0.8,
                message="Browser ref action accepted by bridge"
            )
        return VerificationResult(
            success=False,
            confidence=0.85,
            message="Browser ref action was not accepted",
            should_retry=False
        )

    async def _verify_browser_assert(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        if '"ok": true' in result.lower():
            return VerificationResult(
                success=True,
                confidence=0.9,
                message="Browser expectation satisfied"
            )
        return VerificationResult(
            success=False,
            confidence=0.85,
            message="Browser expectation not yet satisfied",
            should_retry=True
        )

    async def _verify_run_shell(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a shell command succeeded. Includes safety checks."""
        command = args.get("command", "")
        result_lower = result.lower()
        
        # Anti-hallucination: block dangerous commands that should have been caught upstream
        DANGEROUS_COMMANDS = [
            r'rm\s+-rf\s+/',               # rm -rf /
            r'mkfs\.',                       # Format filesystem
            r'dd\s+if=.*of=/dev/',           # Overwrite disk
            r':\(\)\s*\{\s*:\|:',            # Fork bomb
            r'chmod\s+-R\s+777\s+/',         # World-writable root
            r'> /dev/sd[a-z]',               # Overwrite raw disk
        ]
        for pattern in DANGEROUS_COMMANDS:
            if re.search(pattern, command):
                return VerificationResult(
                    success=False,
                    confidence=1.0,
                    message=f"Anti-hallucination: dangerous command blocked: {command[:50]}",
                    should_retry=False
                )
        
        # Check for error patterns specific to shell
        permanent_error_patterns = [
            "command not found",
            "no such file or directory",
            "permission denied",
            "operation not permitted",
            "exit code: 127",
        ]
        for pattern in permanent_error_patterns:
            if pattern in result_lower:
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message=f"Shell command failed: {pattern}",
                    should_retry=False,
                    suggested_fix="Check command syntax and permissions"
                )
        
        # Check for transient/network errors (should retry)
        transient_error_patterns = [
            "econnreset", "econnrefused", "network error",
            "unreachable", "connection refused", "temporarily unavailable",
            "timeout", "timed out",
        ]
        for pattern in transient_error_patterns:
            if pattern in result_lower:
                return VerificationResult(
                    success=False,
                    confidence=0.85,
                    message=f"Transient error: {pattern}",
                    should_retry=True,
                    suggested_fix="Retry the command"
                )
        
        # Check for explicit ERROR prefix
        if result_lower.startswith("error"):
            return VerificationResult(
                success=False,
                confidence=0.85,
                message=f"Command error: {result[:100]}",
                should_retry=True
            )
        
        return VerificationResult(
            success=True,
            confidence=0.8,
            message="Shell command completed"
        )

    async def _verify_read_file(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a file was read."""
        result_lower = result.lower()
        # Check for explicit file-not-found errors
        if result_lower.startswith("error") or "filenotfounderror" in result_lower or "no such file" in result_lower:
            return VerificationResult(
                success=False,
                confidence=0.9,
                message=f"File not found: {args.get('path', '')}",
                should_retry=False
            )
        
        # If we got content, it succeeded (even if content contains words like 'error')
        if len(result) > 0:
            return VerificationResult(
                success=True,
                confidence=0.95,
                message=f"Read {len(result)} characters"
            )
        
        return VerificationResult(
            success=True,
            confidence=0.7,
            message="File appears empty"
        )

    async def _verify_write_file(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a file was written. Includes anti-hallucination checks."""
        file_path = args.get("path", "")
        content = args.get("content", "")
        
        # Anti-hallucination: detect placeholder/template content that LLM forgot to fill
        PLACEHOLDER_PATTERNS = [
            r'\{step\d+_result\}',           # Unreplaced data flow placeholders
            r'\{prev_result\}',              # Unreplaced prev_result
            r'\[INSERT .+? HERE\]',          # Template markers
            r'<YOUR .+? HERE>',              # Template markers
            r'TODO:?\s*(?:replace|fill|add)', # TODO markers
            r'\.\.\.(?:\s*(?:add|insert|your))', # Ellipsis markers
        ]
        for pattern in PLACEHOLDER_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                return VerificationResult(
                    success=False,
                    confidence=0.95,
                    message=f"Anti-hallucination: placeholder content detected in write_file: {pattern}",
                    should_retry=False,
                    suggested_fix="Ensure all placeholders are replaced with actual content"
                )
        
        # Anti-hallucination: detect suspiciously short content for scripts/configs
        if file_path.endswith(('.py', '.js', '.ts', '.sh', '.yaml', '.yml', '.json')):
            if len(content.strip()) < 5 and content.strip() not in ('{}', '[]', '""', "''", ''):
                return VerificationResult(
                    success=False,
                    confidence=0.8,
                    message=f"Anti-hallucination: suspiciously short content ({len(content)} chars) for {file_path}",
                    should_retry=False
                )
        
        if "success" in result.lower() or "wrote" in result.lower() or "created" in result.lower():
            return VerificationResult(
                success=True,
                confidence=0.9,
                message=f"File written: {file_path}"
            )
        
        return VerificationResult(
            success=False,
            confidence=0.7,
            message="Could not confirm file write",
            should_retry=True
        )

    async def _verify_get_ui_tree(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify accessibility tree dumps return usable structure."""
        result_lower = str(result or "").strip().lower()
        if result_lower.startswith("error") or "timed out getting ui tree" in result_lower:
            return VerificationResult(
                success=False,
                confidence=0.95,
                message=str(result)[:180],
                should_retry=True,
                suggested_fix="Try a direct field/action tool or retry after refocusing the app",
            )
        if "no elements found matching search" in result_lower:
            return VerificationResult(
                success=False,
                confidence=0.85,
                message=str(result)[:180],
                should_retry=True,
                suggested_fix="Retry without a search filter or use a broader description",
            )
        if "[ax" in result_lower or " at " in result_lower:
            return VerificationResult(
                success=True,
                confidence=0.85,
                message="UI tree captured",
            )
        return VerificationResult(
            success=False,
            confidence=0.7,
            message="Could not confirm UI tree output",
            should_retry=True,
        )

    async def _verify_click_ui(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that an accessibility click actually found a target."""
        if _looks_like_ui_lookup_failure(result):
            return VerificationResult(
                success=False,
                confidence=0.95,
                message=str(result)[:180],
                should_retry=True,
                suggested_fix="Try type_in_field or inspect the UI tree before pressing keys",
            )

        if "clicked [" in result.lower() or result.lower().startswith("clicked "):
            return VerificationResult(
                success=True,
                confidence=0.85,
                message="UI element clicked",
            )

        return VerificationResult(
            success=False,
            confidence=0.7,
            message="Could not confirm UI click target",
            should_retry=True,
            suggested_fix="Use a different element description or inspect the UI tree",
        )

    async def _verify_type_in_field(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a field was focused and text entry succeeded."""
        if _looks_like_ui_lookup_failure(result):
            return VerificationResult(
                success=False,
                confidence=0.95,
                message=str(result)[:180],
                should_retry=True,
                suggested_fix="Try a different field description or inspect the UI tree",
            )

        result_lower = result.lower()
        if "typed " in result_lower or "pasted " in result_lower:
            return VerificationResult(
                success=True,
                confidence=0.85,
                message="Field focused and text entered",
            )

        return VerificationResult(
            success=False,
            confidence=0.75,
            message="Could not confirm field entry",
            should_retry=True,
            suggested_fix="Refocus the field before typing",
        )

    async def _verify_type_text(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that text was typed."""
        if _looks_like_ui_lookup_failure(result) or "no text provided" in result.lower():
            return VerificationResult(
                success=False,
                confidence=0.95,
                message=str(result)[:180],
                should_retry=False,
            )
        # Typing is hard to verify without screenshots
        return VerificationResult(
            success=True,
            confidence=0.75,
            message="Text typed"
        )

    async def _verify_press_key(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a key was pressed."""
        key = args.get("key", "")
        if result.lower().startswith("error"):
            return VerificationResult(
                success=False,
                confidence=0.95,
                message=str(result)[:180],
                should_retry=False,
            )
        return VerificationResult(
            success=True,
            confidence=0.8,
            message=f"Key '{key}' pressed"
        )

    async def _verify_click_element(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that an element was clicked."""
        if "clicked" in result.lower() or "success" in result.lower():
            return VerificationResult(
                success=True,
                confidence=0.8,
                message="Element clicked"
            )
        
        if "not found" in result.lower() or "no element" in result.lower():
            return VerificationResult(
                success=False,
                confidence=0.9,
                message="Element not found",
                should_retry=True,
                suggested_fix="Use get_ui_tree to find correct element"
            )
        
        return VerificationResult(
            success=True,
            confidence=0.6,
            message="Click attempted"
        )

    async def _verify_play_media(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that media playback was triggered."""
        return VerificationResult(
            success=True,
            confidence=0.8,
            message="Media play command sent"
        )

    async def _verify_run_shortcut(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a keyboard shortcut was executed."""
        result_lower = str(result or "").strip().lower()
        if result_lower.startswith("error"):
            return VerificationResult(
                success=False,
                confidence=0.95,
                message=str(result)[:180],
                should_retry=False,
            )

        keys = str((args or {}).get("keys", "")).strip().lower()
        if keys == "command+v":
            state = await get_state() if get_state else {}
            clipboard = str((state or {}).get("clipboard", "") or "").strip()
            if not clipboard:
                return VerificationResult(
                    success=False,
                    confidence=0.8,
                    message="Paste shortcut attempted with empty clipboard",
                    should_retry=True,
                    suggested_fix="Use type_text with explicit text instead of relying on the clipboard",
                )
            return VerificationResult(
                success=True,
                confidence=0.6,
                message="Paste shortcut sent",
            )

        return VerificationResult(
            success=True,
            confidence=0.8,
            message="Shortcut executed"
        )

    # ═══════════════════════════════════════════════════════════════
    #  Research-Critical Verifiers
    # ═══════════════════════════════════════════════════════════════

    async def _verify_web_search(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a web search was initiated."""
        query = args.get("query", "")
        if not query or len(query.strip()) < 2:
            return VerificationResult(
                success=False,
                confidence=0.9,
                message="web_search called with empty or trivial query",
                should_retry=False
            )
        if "opened web search" in result.lower() or "opened" in result.lower():
            return VerificationResult(
                success=True,
                confidence=0.85,
                message=f"Web search opened for: {query[:60]}"
            )
        return VerificationResult(
            success=False,
            confidence=0.7,
            message="Could not confirm web search opened",
            should_retry=True
        )

    async def _verify_browser_click_match(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that browser_click_match found and clicked an element."""
        import json as _json
        try:
            data = _json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            # Check the top-level ok field
            if data.get("ok") is False:
                error_msg = data.get("message", "Click match failed")
                error_code = data.get("error_code", "unknown")
                return VerificationResult(
                    success=False,
                    confidence=0.95,
                    message=f"browser_click_match failed: {error_msg}",
                    should_retry=error_code in ("stale_ref", "no_snapshot"),
                    suggested_fix="Try browser_refresh_refs then retry, or use a different query"
                )
            # Check nested action result
            action = data.get("action", {})
            if isinstance(action, dict) and action.get("ok") is False:
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message=f"Click action failed: {action.get('message', 'unknown')}",
                    should_retry=True,
                    suggested_fix="Element may have moved. Refresh and retry."
                )
            # Has a selected ref — success
            if data.get("selected_ref_id") or data.get("selected_candidate"):
                return VerificationResult(
                    success=True,
                    confidence=0.85,
                    message=f"Clicked element: {data.get('selection_reason', 'matched')}"
                )

        return VerificationResult(
            success=True,
            confidence=0.6,
            message="browser_click_match returned but result unclear"
        )

    async def _verify_browser_read_page(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that browser_read_page returned useful content."""
        import json as _json
        try:
            data = _json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            element_count = data.get("element_count", 0)
            content = data.get("content", "")
            url = data.get("url", "")

            if element_count == 0 and not content:
                return VerificationResult(
                    success=False,
                    confidence=0.85,
                    message=f"browser_read_page returned no content (url={url})",
                    should_retry=True,
                    suggested_fix="Page may not have loaded yet. Try browser_refresh_refs then retry."
                )
            if element_count > 0:
                return VerificationResult(
                    success=True,
                    confidence=0.85,
                    message=f"Read {element_count} elements from {url[:60]}"
                )

        if result.startswith("ERROR"):
            return VerificationResult(
                success=False,
                confidence=0.9,
                message=f"browser_read_page error: {result[:100]}",
                should_retry=True
            )

        return VerificationResult(
            success=True,
            confidence=0.7,
            message="browser_read_page completed"
        )

    async def _verify_browser_read_text(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that browser_read_text extracted meaningful text content."""
        import json as _json
        try:
            data = _json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            paragraph_count = data.get("paragraph_count", 0)
            content_length = data.get("content_length", 0)
            url = data.get("url", "")

            if paragraph_count == 0 or content_length < 50:
                return VerificationResult(
                    success=False,
                    confidence=0.85,
                    message=f"browser_read_text found no readable content (url={url})",
                    should_retry=True,
                    suggested_fix="Try scrolling down or navigating to the article page first."
                )
            return VerificationResult(
                success=True,
                confidence=0.9,
                message=f"Extracted {paragraph_count} paragraphs ({content_length} chars) from {url[:60]}"
            )

        return VerificationResult(
            success=True,
            confidence=0.6,
            message="browser_read_text completed"
        )

    async def _verify_browser_scroll(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that browser scroll succeeded."""
        import json as _json
        try:
            data = _json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            if data.get("ok") is True:
                direction = data.get("direction", "")
                at_bottom = data.get("at_bottom", False)
                msg = f"Scrolled {direction}"
                if at_bottom:
                    msg += " (reached bottom)"
                return VerificationResult(success=True, confidence=0.85, message=msg)
            if data.get("ok") is False:
                return VerificationResult(
                    success=False,
                    confidence=0.85,
                    message=f"Scroll failed: {data.get('message', 'unknown')}",
                    should_retry=True
                )

        return VerificationResult(success=True, confidence=0.7, message="Scroll command sent")

    async def _verify_read_page_content(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify ACI read_page_content returns substantive content."""
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            if data.get("ok") is False or data.get("error_code"):
                return VerificationResult(
                    success=False,
                    confidence=0.95,
                    message=f"read_page_content failed: {data.get('message', 'unknown')}",
                    should_retry=True,
                    suggested_fix="Open a content page first, then retry read_page_content."
                )

            content_length = int(data.get("content_length", 0) or 0)
            paragraph_count = int(data.get("paragraph_count", 0) or 0)
            url = data.get("url", "")
            if content_length < 50:
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message=f"read_page_content too shallow ({content_length} chars, {paragraph_count} paragraphs) at {url[:60]}",
                    should_retry=True,
                    suggested_fix="Navigate to a source page and read again."
                )
            return VerificationResult(
                success=True,
                confidence=0.9,
                message=f"Read {content_length} chars across {paragraph_count} paragraphs"
            )

        return VerificationResult(
            success=False,
            confidence=0.8,
            message="read_page_content returned non-JSON output",
            should_retry=True
        )

    async def _verify_extract_structured_data(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify ACI extract_structured_data returns at least one item."""
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            if data.get("ok") is False or data.get("error_code"):
                return VerificationResult(
                    success=False,
                    confidence=0.95,
                    message=f"extract_structured_data failed: {data.get('message', 'unknown')}",
                    should_retry=True,
                    suggested_fix="Refresh the page or change the item_type/query."
                )
            items = data.get("items", [])
            item_count = int(data.get("item_count", len(items) if isinstance(items, list) else 0) or 0)
            if item_count == 0:
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message="extract_structured_data returned zero items",
                    should_retry=True,
                    suggested_fix="Try a different page, broaden the query, or scroll before extraction."
                )
            return VerificationResult(
                success=True,
                confidence=0.9,
                message=f"Extracted {item_count} structured item(s)"
            )

        return VerificationResult(
            success=False,
            confidence=0.8,
            message="extract_structured_data returned non-JSON output",
            should_retry=True
        )

    async def _verify_find_and_act(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify ACI find_and_act actually performed a valid action."""
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            if data.get("ok") is False or data.get("error_code"):
                return VerificationResult(
                    success=False,
                    confidence=0.95,
                    message=f"find_and_act failed: {data.get('message', 'unknown')}",
                    should_retry=True,
                    suggested_fix="Refresh refs and retry with a more specific target."
                )

            action_result = data.get("action_result", {})
            if isinstance(action_result, dict) and action_result.get("ok") is False:
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message=f"find_and_act action failed: {action_result.get('message', 'unknown')}",
                    should_retry=True,
                    suggested_fix="The element may be stale; refresh and retry."
                )

            if data.get("ref_id"):
                return VerificationResult(
                    success=True,
                    confidence=0.88,
                    message=f"find_and_act completed on ref {data.get('ref_id')}"
                )

        return VerificationResult(
            success=False,
            confidence=0.8,
            message="find_and_act result missing actionable ref",
            should_retry=True
        )

    async def _verify_get_page_summary(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify ACI get_page_summary returns real page structure."""
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            if data.get("ok") is False or data.get("error_code"):
                return VerificationResult(
                    success=False,
                    confidence=0.95,
                    message=f"get_page_summary failed: {data.get('message', 'unknown')}",
                    should_retry=True,
                    suggested_fix="Refresh the browser snapshot and retry."
                )

            total_elements = int(data.get("total_elements", 0) or 0)
            page_type = data.get("page_type", "unknown")
            if total_elements == 0:
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message="get_page_summary returned zero elements",
                    should_retry=True,
                    suggested_fix="Wait for page load and retry."
                )
            return VerificationResult(
                success=True,
                confidence=0.88,
                message=f"Page summary captured ({page_type}, {total_elements} elements)"
            )

        return VerificationResult(
            success=False,
            confidence=0.8,
            message="get_page_summary returned non-JSON output",
            should_retry=True
        )

    async def _verify_web_scrape(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify structured web scrape output is substantive."""
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            if data.get("ok") is False or data.get("error_code"):
                return VerificationResult(
                    success=False,
                    confidence=0.95,
                    message=f"web_scrape failed: {data.get('message', 'unknown')}",
                    should_retry=True,
                    suggested_fix="Try a different URL or use browser tools for JS-heavy pages."
                )
            content_length = int(data.get("content_length", 0) or 0)
            if content_length < 80:
                return VerificationResult(
                    success=False,
                    confidence=0.88,
                    message=f"web_scrape returned too little content ({content_length} chars)",
                    should_retry=True,
                    suggested_fix="Retry with the canonical article URL."
                )
            return VerificationResult(
                success=True,
                confidence=0.9,
                message=f"web_scrape captured {content_length} chars"
            )

        return VerificationResult(
            success=False,
            confidence=0.8,
            message="web_scrape returned non-JSON output",
            should_retry=True
        )

    @staticmethod
    def _is_search_engine_serp(url: str) -> bool:
        """Return True if url is a search-engine results page (Google, Bing, etc.)."""
        try:
            from urllib.parse import urlparse
            host = urlparse(url or "").netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            _serp_hosts = {
                "google.com", "bing.com",
                "duckduckgo.com", "html.duckduckgo.com",
                "yahoo.com", "search.yahoo.com", "baidu.com",
            }
            return host in _serp_hosts and ("/search" in (url or "") or "q=" in (url or ""))
        except Exception:
            return False

    async def _verify_get_web_information(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify high-level web gateway output is substantive for the requested intent."""
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if not isinstance(data, dict):
            return VerificationResult(
                success=False,
                confidence=0.8,
                message="get_web_information returned non-JSON output",
                should_retry=True,
            )

        target_type = str(data.get("target_type") or args.get("target_type") or "").strip().lower()
        route = str(data.get("route", "")).strip()
        error_code = str(data.get("error_code", "")).strip().lower()
        if data.get("ok") is False or data.get("error_code"):
            should_retry = True
            suggested_fix = "Adjust the query or try opening a specific source URL before reading again."
            if target_type == "search_results" and error_code in {"browser_search_no_results", "empty_items"}:
                should_retry = False
                suggested_fix = "Open a specific source or choose a different strategy instead of repeating the same search."
            elif target_type == "search_results" and route == "background_fetch_fallback" and error_code == "request_failed":
                should_retry = False
                suggested_fix = "Live browser search should be used instead of retrying the same background fallback search."
            elif route == "browser_aci" and error_code in {"flash_timeout", "flash_error", "flash_unavailable"}:
                should_retry = False
                suggested_fix = "Flash browser extraction failed; choose a simpler read/search step instead of retrying the same call."
            return VerificationResult(
                success=False,
                confidence=0.95,
                message=f"get_web_information failed: {data.get('message', 'unknown')}",
                should_retry=should_retry,
                suggested_fix=suggested_fix,
            )

        item_count = int(data.get("item_count", len(data.get("items", [])) if isinstance(data.get("items"), list) else 0) or 0)
        content_length = int(data.get("content_length", len(str(data.get("content", "") or ""))) or 0)

        if target_type == "search_results":
            if item_count <= 0:
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message="get_web_information returned zero search results",
                    should_retry=True,
                    suggested_fix="Broaden the query or switch to a more specific search phrase.",
                )
            return VerificationResult(
                success=True,
                confidence=0.9,
                message=f"Captured {item_count} search result(s) via {route or 'gateway'}",
            )

        if target_type == "structured_data":
            if item_count <= 0:
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message="get_web_information returned no structured items",
                    should_retry=True,
                    suggested_fix="Use a clearer item hint or move to the listing/results page first.",
                )
            return VerificationResult(
                success=True,
                confidence=0.9,
                message=f"Captured {item_count} structured item(s) via {route or 'gateway'}",
            )

        if target_type == "page_summary":
            result_url = str(data.get("url", "") or "").strip()
            if self._is_search_engine_serp(result_url):
                return VerificationResult(
                    success=False,
                    confidence=0.92,
                    message=(
                        f"get_web_information summarized a search-results page ({result_url[:80]}) "
                        "instead of a content page — no real research data captured"
                    ),
                    should_retry=True,
                    suggested_fix="Provide a direct URL from the search results (e.g. Wikipedia or IMDb) rather than summarizing the SERP.",
                )
            summary_text = str(data.get("summary", "") or "").strip()
            headings = data.get("headings", [])
            page_type = str(data.get("page_type", "") or "").strip()
            if len(summary_text) < 40 and not headings and not page_type:
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message="get_web_information returned no real page summary",
                    should_retry=True,
                    suggested_fix="Retry summary generation or read the page content directly first.",
                )
            return VerificationResult(
                success=True,
                confidence=0.9,
                message=f"Captured page summary via {route or 'gateway'}",
            )

        if target_type == "page_content":
            if content_length < 60 and item_count <= 0 and not data.get("headings"):
                return VerificationResult(
                    success=False,
                    confidence=0.88,
                    message=f"get_web_information returned too little page information ({content_length} chars)",
                    should_retry=True,
                    suggested_fix="Open the source page first or retry with a specific URL.",
                )
            return VerificationResult(
                success=True,
                confidence=0.88,
                message=f"Captured page information via {route or 'gateway'}",
            )

        if content_length >= 60 or item_count > 0:
            return VerificationResult(
                success=True,
                confidence=0.85,
                message=f"get_web_information returned substantive data via {route or 'gateway'}",
            )

        return VerificationResult(
            success=False,
            confidence=0.82,
            message="get_web_information result was too thin to trust",
            should_retry=True,
        )

    async def _verify_gdocs_create(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        """Verify that a Google Doc was created."""
        import json as _json
        try:
            data = _json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            if data.get("ok") is True:
                url = data.get("url", "")
                return VerificationResult(
                    success=True,
                    confidence=0.9,
                    message=f"Google Doc created: {url}"
                )
            if data.get("ok") is False:
                if data.get("repairable") and data.get("url"):
                    return VerificationResult(
                        success=False,
                        confidence=0.92,
                        message=f"Google Doc content application failed: {data.get('note', 'unknown error')}",
                        should_retry=True,
                        suggested_fix="Retry the write on the same Google Doc instead of creating a new one.",
                    )
                return VerificationResult(
                    success=False,
                    confidence=0.9,
                    message=f"Google Doc creation failed: {data.get('note', 'unknown error')}",
                    should_retry=True
                )

        return VerificationResult(success=True, confidence=0.7, message="gdocs_create completed")

    async def _verify_gdocs_append(
        self,
        args: Dict[str, Any],
        result: str,
        criteria: str,
        get_state: Optional[Callable]
    ) -> VerificationResult:
        import json as _json
        try:
            data = _json.loads(result)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            if data.get("ok") is True and data.get("appended_chars", 0):
                return VerificationResult(
                    success=True,
                    confidence=0.9,
                    message=f"Google Doc updated: {data.get('url', data.get('doc_id', 'document'))}",
                )
            if data.get("ok") is False:
                return VerificationResult(
                    success=False,
                    confidence=0.92,
                    message=f"Google Doc append failed: {data.get('note', data.get('error_code', 'unknown error'))}",
                    should_retry=True,
                )

        return VerificationResult(
            success=False,
            confidence=0.8,
            message="gdocs_append result was unclear",
            should_retry=True,
        )


# ═══════════════════════════════════════════════════════════════
#  Shared Router for Visual Verification
# ═══════════════════════════════════════════════════════════════

_shared_router = None

def _get_shared_router():
    """Return a single ModelRouter instance shared across all visual verification calls."""
    global _shared_router
    if _shared_router is None:
        from providers.router import ModelRouter
        _shared_router = ModelRouter()
    return _shared_router


# ═══════════════════════════════════════════════════════════════
#  Singleton Instance
# ═══════════════════════════════════════════════════════════════

# Global verifier instance
_verifier: Optional[ToolVerifier] = None


def get_verifier() -> ToolVerifier:
    """Get the global ToolVerifier instance."""
    global _verifier
    if _verifier is None:
        _verifier = ToolVerifier()
    return _verifier
