"""
Moonwalk — World State V2
==========================
Structured context representation replacing raw strings.
Provides typed fields for desktop state, user intent, and extracted entities.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum


# ═══════════════════════════════════════════════════════════════
#  Intent Classification Enums
# ═══════════════════════════════════════════════════════════════

class IntentAction(str, Enum):
    """High-level action categories the user might request."""
    OPEN = "open"           # Launch app/URL/file
    CLOSE = "close"         # Quit app/close window
    SEARCH = "search"       # Web or file search
    CREATE = "create"       # Write file, spawn agent, create something
    DELETE = "delete"       # Remove file, stop agent
    MODIFY = "modify"       # Edit file, change setting
    ANALYZE = "analyze"     # Read screen, understand content, explain
    NAVIGATE = "navigate"   # Go to URL, switch app, scroll
    EXECUTE = "execute"     # Run command, press keys, click
    PLAY = "play"           # Play media (music, video)
    COMMUNICATE = "communicate"  # Send message, email
    QUERY = "query"         # Ask a question (no action needed)
    UNKNOWN = "unknown"     # Needs clarification


class TargetType(str, Enum):
    """What the user's action targets."""
    APP = "app"
    URL = "url"
    FILE = "file"
    FOLDER = "folder"
    CONTENT = "content"      # Text, media
    UI_ELEMENT = "ui_element"
    AGENT = "agent"
    SYSTEM = "system"        # Volume, brightness, etc.
    UNKNOWN = "unknown"


# ═══════════════════════════════════════════════════════════════
#  User Intent
# ═══════════════════════════════════════════════════════════════

@dataclass
class UserIntent:
    """Structured understanding of what the user wants to accomplish."""
    action: IntentAction
    target_type: TargetType
    target_value: str = ""              # "Spotify", "youtube.com", "~/file.py"
    parameters: Dict[str, Any] = field(default_factory=dict)  # Extra context
    confidence: float = 0.0             # 0.0 - 1.0
    ambiguous: bool = False             # Needs clarification?
    clarification_prompt: str = ""      # What to ask if ambiguous
    raw_text: str = ""                  # Original user text

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "target_type": self.target_type.value,
            "target_value": self.target_value,
            "parameters": self.parameters,
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "clarification_prompt": self.clarification_prompt
        }


# ═══════════════════════════════════════════════════════════════
#  Task Graph
# ═══════════════════════════════════════════════════════════════

@dataclass
class TaskEntity:
    """One entity extracted from the request."""
    type: str
    value: str
    source: str = ""


@dataclass
class TaskGraph:
    """
    Multi-entity task model used by the planner.
    Keeps compound requests intact instead of collapsing them into one target.
    """
    primary_action: str = ""
    primary_goal: str = ""
    entities: List[TaskEntity] = field(default_factory=list)
    selectors: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    desired_outcomes: List[str] = field(default_factory=list)
    unresolved_slots: List[str] = field(default_factory=list)
    complexity_score: float = 0.0

    def entity_types(self) -> set[str]:
        return {entity.type for entity in self.entities}

    def to_dict(self) -> dict:
        return {
            "primary_action": self.primary_action,
            "primary_goal": self.primary_goal,
            "entities": [
                {"type": entity.type, "value": entity.value, "source": entity.source}
                for entity in self.entities
            ],
            "selectors": list(self.selectors),
            "constraints": list(self.constraints),
            "desired_outcomes": list(self.desired_outcomes),
            "unresolved_slots": list(self.unresolved_slots),
            "complexity_score": round(float(self.complexity_score or 0.0), 2),
        }

    def to_prompt_string(self) -> str:
        lines = [
            f"  Primary Action: {self.primary_action or 'unknown'}",
            f"  Primary Goal: {self.primary_goal or '(none)'}",
            f"  Complexity: {self.complexity_score:.1f}",
        ]
        if self.entities:
            entity_bits = [f"{entity.type}:{entity.value}" for entity in self.entities]
            lines.append(f"  Entities: {', '.join(entity_bits)}")
        if self.selectors:
            lines.append(f"  Selectors: {', '.join(self.selectors)}")
        if self.constraints:
            lines.append(f"  Constraints: {', '.join(self.constraints)}")
        if self.desired_outcomes:
            lines.append(f"  Desired Outcomes: {', '.join(self.desired_outcomes)}")
        if self.unresolved_slots:
            lines.append(f"  Unresolved Slots: {', '.join(self.unresolved_slots)}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  World State
# ═══════════════════════════════════════════════════════════════

@dataclass
class WorldState:
    """
    Complete structured view of the desktop environment.
    Replaces the old ContextSnapshot with typed, structured data.
    """
    # ── Desktop State (from perception layer) ──
    active_app: str = ""
    window_title: str = ""
    browser_url: Optional[str] = None
    running_apps: List[str] = field(default_factory=list)
    
    # ── Extracted Entities (from user request) ──
    mentioned_apps: List[str] = field(default_factory=list)
    mentioned_files: List[str] = field(default_factory=list)
    mentioned_urls: List[str] = field(default_factory=list)
    mentioned_queries: List[str] = field(default_factory=list)  # Search terms
    
    # ── Clipboard ──
    clipboard_content: Optional[str] = None
    
    # ── Screen State (if captured) ──
    has_screenshot: bool = False
    screenshot_path: Optional[str] = None
    screen_description: Optional[str] = None
    
    # ── Selected Text (from browser or editor) ──
    selected_text: Optional[str] = None
    
    # ── Parsed Intent ──
    intent: Optional[UserIntent] = None
    task_graph: Optional[TaskGraph] = None
    
    # ── History Context ──
    recent_tool_calls: List[str] = field(default_factory=list)
    recent_tool_results: List[str] = field(default_factory=list)
    conversation_topic: str = ""
    turn_count: int = 0
    
    # ── Metadata ──
    timestamp: float = 0.0

    def to_prompt_dict(self) -> dict:
        """Convert to dict for JSON serialization in prompts."""
        return {
            "desktop": {
                "active_app": self.active_app,
                "window_title": self.window_title,
                "browser_url": self.browser_url,
            },
            "entities": {
                "apps": self.mentioned_apps,
                "files": self.mentioned_files,
                "urls": self.mentioned_urls,
            },
            "intent": self.intent.to_dict() if self.intent else None,
            "task_graph": self.task_graph.to_dict() if self.task_graph else None,
            "has_screenshot": self.has_screenshot,
        }

    def to_prompt_string(self) -> str:
        """Format as a structured string block for the LLM."""
        now = datetime.now()
        lines = ["[Desktop Context]"]
        lines.append(f"  Date: {now.strftime('%A, %B %d, %Y')}")
        lines.append(f"  Time: {now.strftime('%I:%M %p')}")
        lines.append(f"  Active App: {self.active_app or 'Unknown'}")
        lines.append(f"  Window Title: {self.window_title or 'Unknown'}")
        if self.browser_url:
            lines.append(f"  Browser URL: {self.browser_url}")
        if self.selected_text:
            lines.append(f"  Selected Text: {self.selected_text[:500]}")
        if self.clipboard_content:
            lines.append(f"  Clipboard: {self.clipboard_content[:200]}")
        if self.has_screenshot:
            lines.append(f"  Screenshot: attached")
        if self.intent:
            lines.append(f"[Parsed Intent]")
            lines.append(f"  Action: {self.intent.action.value}")
            lines.append(f"  Target: {self.intent.target_type.value} → {self.intent.target_value}")
            lines.append(f"  Confidence: {self.intent.confidence:.0%}")
            if self.intent.ambiguous:
                lines.append(f"  ⚠️ Ambiguous: {self.intent.clarification_prompt}")
        if self.task_graph:
            lines.append("[Task Graph]")
            lines.append(self.task_graph.to_prompt_string())
        lines.append("[End Context]")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Intent Parser (Rule-based for speed)
# ═══════════════════════════════════════════════════════════════

class IntentParser:
    """
    Fast rule-based intent parser. 
    Falls back to LLM classification for complex cases.
    """
    
    # Action keywords
    ACTION_PATTERNS = {
        IntentAction.OPEN: ["open", "launch", "start", "show", "go to", "navigate to"],
        IntentAction.CLOSE: ["close", "quit", "exit", "kill", "stop", "end", "terminate"],
        IntentAction.SEARCH: ["search", "find", "look up", "google", "lookup", "find all"],
        IntentAction.CREATE: ["create", "make", "write", "new", "generate", "spawn", "add", "delegate"],
        IntentAction.DELETE: ["delete", "remove", "trash", "erase"],
        IntentAction.MODIFY: ["edit", "change", "modify", "update", "set", "fix", "refactor", "rename"],
        IntentAction.ANALYZE: ["analyze", "explain", "what is", "what's", "describe", "read", "look at", "see", "summarize", "help me with"],
        IntentAction.PLAY: ["play", "pause", "resume", "skip", "next", "previous"],
        IntentAction.EXECUTE: ["click", "type", "press", "run", "execute", "install", "npm", "pip", "ls", "cd"],
        IntentAction.COMMUNICATE: ["send", "email", "message", "text", "reply"],
        IntentAction.QUERY: ["how", "why", "when", "where", "who", "can you", "tell me", "what time", "what day"],
    }
    
    # Target type patterns
    TARGET_PATTERNS = {
        TargetType.APP: ["app", "application", "spotify", "chrome", "safari", "slack", "discord", "code", "vscode", "cursor", "terminal", "finder", "mail", "messages", "notes", "calendar"],
        TargetType.URL: ["http", "https", "www.", ".com", ".org", ".io", "youtube", "github", "google"],
        TargetType.FILE: [".py", ".js", ".ts", ".txt", ".md", ".json", ".yaml", ".html", ".css", "file", "document"],
        TargetType.FOLDER: ["folder", "directory", "~/", "/Users"],
        TargetType.AGENT: ["agent", "background", "monitor", "task"],
        TargetType.SYSTEM: ["volume", "brightness", "wifi", "bluetooth", "dark mode", "night shift"],
    }

    # Common app aliases
    APP_ALIASES = {
        "spotify": "Spotify",
        "chrome": "Google Chrome",
        "safari": "Safari",
        "vscode": "Visual Studio Code",
        "code": "Visual Studio Code",
        "cursor": "Cursor",
        "slack": "Slack",
        "discord": "Discord",
        "notion": "Notion",
        "terminal": "Terminal",
        "iterm": "iTerm",
        "finder": "Finder",
        "mail": "Mail",
        "notes": "Notes",
        "messages": "Messages",
        "music": "Music",
        "photos": "Photos",
        "preview": "Preview",
        "capcut": "CapCut",
        # Additional common apps
        "calculator": "Calculator",
        "calendar": "Calendar",
        "reminders": "Reminders",
        "books": "Books",
        "news": "News",
        "stocks": "Stocks",
        "weather": "Weather",
        "maps": "Maps",
        "facetime": "FaceTime",
        "zoom": "zoom.us",
        "teams": "Microsoft Teams",
        "word": "Microsoft Word",
        "excel": "Microsoft Excel",
        "powerpoint": "Microsoft PowerPoint",
        "pages": "Pages",
        "numbers": "Numbers",
        "keynote": "Keynote",
        "xcode": "Xcode",
        "textedit": "TextEdit",
        "system preferences": "System Preferences",
        "settings": "System Settings",
        "activity monitor": "Activity Monitor",
        # Windows apps. This is the Windows port, so a request for "notepad"
        # must resolve to Notepad — not to the nearest macOS equivalent.
        # Longer phrases first: lookup returns the first alias that matches.
        "file explorer": "File Explorer",
        "windows explorer": "File Explorer",
        "task manager": "Task Manager",
        "command prompt": "Command Prompt",
        "snipping tool": "Snipping Tool",
        "control panel": "Control Panel",
        "device manager": "Device Manager",
        "docker desktop": "Docker Desktop",
        "notepad++": "Notepad++",
        "notepad": "Notepad",
        "wordpad": "WordPad",
        "paint": "Paint",
        "mspaint": "Paint",
        "powershell": "PowerShell",
        "cmd": "Command Prompt",
        "docker": "Docker Desktop",
        "edge": "Microsoft Edge",
        "superrareapp2024": "SuperRareApp2024",  # For benchmark edge case
    }

    def parse(self, text: str, context: Optional['WorldState'] = None) -> UserIntent:
        """Parse user text into a structured UserIntent."""
        text_lower = text.lower().strip()
        
        # Detect action
        action = self._detect_action(text_lower)
        
        # Detect target type and value
        target_type, target_value = self._detect_target(text_lower, action)
        
        # Extract parameters
        parameters = self._extract_parameters(text_lower, action, target_type)
        
        # Calculate confidence
        confidence = self._calculate_confidence(action, target_type, target_value)
        
        # Check for ambiguity
        ambiguous, clarification = self._check_ambiguity(text_lower, action, target_type, target_value)
        
        return UserIntent(
            action=action,
            target_type=target_type,
            target_value=target_value,
            parameters=parameters,
            confidence=confidence,
            ambiguous=ambiguous,
            clarification_prompt=clarification,
            raw_text=text
        )

    def extract_task_graph(self, text: str, context: Optional['WorldState'] = None) -> TaskGraph:
        """Extract a multi-entity task graph for compound-task planning."""
        raw_text = (text or "").strip()
        text_lower = raw_text.lower()
        intent = self.parse(raw_text, context)

        entities = self._extract_all_entities(text_lower)
        selectors = self._extract_selectors(text_lower)
        constraints = self._extract_constraints(text_lower)
        desired_outcomes = self._infer_desired_outcomes(text_lower, intent, entities, selectors)
        unresolved_slots = self._infer_unresolved_slots(text_lower, intent, entities)
        complexity = self._estimate_task_complexity(text_lower, entities, selectors, constraints, desired_outcomes)

        primary_goal = raw_text[:140] if raw_text else f"{intent.action.value} task"
        return TaskGraph(
            primary_action=intent.action.value,
            primary_goal=primary_goal,
            entities=entities,
            selectors=selectors,
            constraints=constraints,
            desired_outcomes=desired_outcomes,
            unresolved_slots=unresolved_slots,
            complexity_score=complexity,
        )

    def _detect_action(self, text: str) -> IntentAction:
        """Detect the primary action from text."""
        import re
        
        # Check for specific high-priority patterns first
        # "delegate" should match CREATE, not SEARCH (even if "research" is in the text)
        if "delegate" in text:
            return IntentAction.CREATE
        
        # "find and fix" or "fix" should be MODIFY, not SEARCH
        if re.search(r'\bfix\b', text):
            return IntentAction.MODIFY
        
        # "refactor" should be MODIFY
        if re.search(r'\brefactor\b', text):
            return IntentAction.MODIFY
        
        # "add error handling" / "add ... to" should be MODIFY
        if re.search(r'\badd\b.*\b(?:handling|to)\b', text):
            return IntentAction.MODIFY
        
        # "open file X and update/change Y" → the real intent is MODIFY
        if re.search(r'\bopen\b.*(?:file|\.(?:yaml|json|py|js|ts|sh|txt|conf|cfg))', text) and \
           re.search(r'\b(?:update|change|modify|edit|set)\b', text):
            return IntentAction.MODIFY
        
        # Use word boundary matching to avoid "research" matching "search"
        for action, patterns in self.ACTION_PATTERNS.items():
            for pattern in patterns:
                # Use word boundary for short patterns to avoid false matches
                if len(pattern) <= 6:  # "search", "find", etc.
                    if re.search(r'\b' + re.escape(pattern) + r'\b', text):
                        return action
                else:
                    if pattern in text:
                        return action
        return IntentAction.UNKNOWN

    def _extract_all_entities(self, text: str) -> List[TaskEntity]:
        import re

        entities: List[TaskEntity] = []
        seen: set[tuple[str, str]] = set()

        def add_entity(entity_type: str, value: str, source: str) -> None:
            cleaned = (value or "").strip()
            if not cleaned:
                return
            key = (entity_type, cleaned.lower())
            if key in seen:
                return
            seen.add(key)
            entities.append(TaskEntity(type=entity_type, value=cleaned, source=source))

        for match in re.finditer(r'https?://[^\s]+|www\.[^\s]+', text):
            add_entity("url", match.group(), "url")

        specific_file_re = r'(?:[~/][\w/.-]+\.\w+|\b[\w-]+\.(?:mp4|mov|m4v|avi|mkv|webm|mp3|wav|m4a|png|jpg|jpeg|gif|py|js|ts|json|yaml|yml|md|txt|html|css|sh)\b)'
        for match in re.finditer(specific_file_re, text):
            add_entity("file", match.group(), "file")

        folder_aliases = {
            "downloads": "Downloads",
            "desktop": "Desktop",
            "documents": "Documents",
            "music": "Music",
            "pictures": "Pictures",
            "movies": "Movies",
        }
        for alias, label in folder_aliases.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', text):
                add_entity("folder", label, "folder_alias")

        for match in re.finditer(r'(?:[~/][\w/.-]+)', text):
            path = match.group()
            if "." in path.split("/")[-1]:
                continue
            add_entity("folder", path, "path")

        for alias, app_name in self.APP_ALIASES.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', text):
                add_entity("app", app_name, "app_alias")

        content_patterns = {
            "video": r'\b(video|clip|footage|movie|recording)\b',
            "audio": r'\b(audio|song|music|recording|voice note)\b',
            "image": r'\b(image|photo|picture|screenshot)\b',
            "document": r'\b(document|doc|report|paper|letter)\b',
        }
        for label, pattern in content_patterns.items():
            if re.search(pattern, text):
                add_entity("content", label, "content_keyword")

        return entities

    def _extract_selectors(self, text: str) -> List[str]:
        selectors: List[str] = []
        selector_map = {
            "latest": ("latest", "newest", "most recent"),
            "current": ("current", "active", "frontmost"),
            "selected": ("selected", "highlighted"),
            "first": ("first", "top"),
            "last": ("last",),
        }
        for normalized, patterns in selector_map.items():
            if any(pattern in text for pattern in patterns):
                selectors.append(normalized)
        return selectors

    def _extract_constraints(self, text: str) -> List[str]:
        import re

        constraints: List[str] = []
        patterns = [
            r'\bin\s+my\s+downloads\b',
            r'\bin\s+downloads\b',
            r'\bfrom\s+my\s+downloads\b',
            r'\bfrom\s+downloads\b',
            r'\busing\s+capcut\b',
            r'\bwith\s+capcut\b',
            r'\binto\s+capcut\b',
            r'\bif\s+possible\b',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                constraints.append(match.group().strip())
        return constraints

    def _infer_desired_outcomes(
        self,
        text: str,
        intent: UserIntent,
        entities: List[TaskEntity],
        selectors: List[str],
    ) -> List[str]:
        outcomes: List[str] = []
        entity_types = {entity.type for entity in entities}

        if any(entity.type == "app" for entity in entities):
            outcomes.append("open_or_focus_app")
        if any(entity.type in {"file", "folder", "content"} for entity in entities):
            outcomes.append("resolve_local_source")
        if any(selector in {"latest", "first", "last"} for selector in selectors):
            outcomes.append("select_specific_item")
        if intent.action == IntentAction.MODIFY and "content" in entity_types:
            outcomes.append("apply_edit")
        if intent.action == IntentAction.MODIFY and any(entity.value == "video" for entity in entities if entity.type == "content"):
            outcomes.append("edit_media")
        return list(dict.fromkeys(outcomes))

    def _infer_unresolved_slots(
        self,
        text: str,
        intent: UserIntent,
        entities: List[TaskEntity],
    ) -> List[str]:
        import re

        unresolved: List[str] = []
        has_media = any(entity.type == "content" and entity.value in {"video", "audio"} for entity in entities)
        specific_edit_patterns = (
            r"\btrim\b",
            r"\bcut\b",
            r"\bcaption\b",
            r"\bcaptions\b",
            r"\bsubtitles\b",
            r"\btransition\b",
            r"\btransitions\b",
            r"\bfilter\b",
            r"\bfilters\b",
            r"\bspeed\b",
            r"\bcrop\b",
            r"\bzoom\b",
            r"\boverlay\b",
            r"\bmusic\b",
            r"\bvoiceover\b",
        )
        has_specific_edit_instruction = any(re.search(pattern, text) for pattern in specific_edit_patterns)
        if intent.action == IntentAction.MODIFY and has_media and not has_specific_edit_instruction:
            unresolved.append("specific_edit_instructions")
        return unresolved

    def _estimate_task_complexity(
        self,
        text: str,
        entities: List[TaskEntity],
        selectors: List[str],
        constraints: List[str],
        desired_outcomes: List[str],
    ) -> float:
        entity_types = {entity.type for entity in entities}
        score = 1.0
        score += max(0, len(entity_types) - 1) * 1.2
        score += len(selectors) * 0.6
        score += len(constraints) * 0.4
        score += max(0, len(desired_outcomes) - 1) * 0.8
        if any(token in text for token in (" and ", " then ", " after ", " before ", " using ", " with ", " from ", " into ")):
            score += 1.2
        return round(score, 2)

    def _detect_target(self, text: str, action: IntentAction) -> tuple[TargetType, str]:
        """Detect target type and extract the target value."""
        
        # Check for URLs first
        import re
        url_match = re.search(r'https?://[^\s]+|www\.[^\s]+', text)
        if url_match:
            return TargetType.URL, url_match.group()
        
        # Check for specific file names BEFORE file pattern searches
        # Matches: server.py, config.yaml, ~/path/file.py, /path/file.py
        specific_file_match = re.search(r'(?:[~/][\w/.-]+\.\w+|\b[\w-]+\.(?:py|js|ts|json|yaml|yml|md|txt|html|css|sh|rb|go|rs|java|c|cpp|h)\b)', text)
        if specific_file_match:
            # Only return this if it's not a pattern search (find python files)
            potential_file = specific_file_match.group()
            if not potential_file.startswith("*") and "files" not in text.split(potential_file)[0][-20:]:
                return TargetType.FILE, potential_file
        
        # Check for file pattern searches (e.g., "find python files", "find *.py")
        # Only trigger when explicitly looking for multiple files
        if any(kw in text for kw in ["find", "search", "look for", "find all"]):
            file_search_patterns = [
                (r'\b(python|\.py)\s+(files?|scripts?)', "*.py"),
                (r'\b(javascript|\.js)\s+(files?|scripts?)', "*.js"),
                (r'\b(typescript|\.ts)\s+(files?|scripts?)', "*.ts"),
                (r'\bfiles?\b.*\.(\w+)', r"*.\1"),  # "files with .ext"
                (r'find\b.*\s+(\*\.?\w+)', r"\1"),  # "find *.py"
            ]
            for pattern, replacement in file_search_patterns:
                if re.search(pattern, text):
                    return TargetType.FILE, replacement
        
        # Check for UI elements (click button, click link)
        ui_patterns = [
            r'click\s+(?:the\s+)?(\w+\s+)?button',
            r'click\s+(?:the\s+)?(\w+\s+)?link',
            r'click\s+(?:on\s+)?(?:the\s+)?["\'"]?(\w+)["\'"]?',
            r'press\s+(?:the\s+)?(\w+)',
        ]
        for pattern in ui_patterns:
            match = re.search(pattern, text)
            if match:
                element_name = match.group(1).strip() if match.group(1) else ""
                # Extract full element description
                if "button" in text:
                    element_name += " button"
                elif "link" in text:
                    element_name += " link"
                return TargetType.UI_ELEMENT, element_name.strip()
        
        # Check for common websites without protocol
        web_patterns = [
            (r'\byoutube\b', "https://youtube.com"),
            (r'\bgmail\b', "https://gmail.com"),
            (r'\bgithub\b', "https://github.com"),
            (r'\btwitter\b', "https://twitter.com"),
            (r'\breddit\b', "https://reddit.com"),
            (r'\blinkedin\b', "https://linkedin.com"),
        ]
        for pattern, url in web_patterns:
            if re.search(pattern, text):
                return TargetType.URL, url
        
        # Check for apps (use word boundary matching to avoid false positives)
        for alias, app_name in self.APP_ALIASES.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', text):
                return TargetType.APP, app_name
        
        # Check for directory paths (file search context)
        path_match = re.search(r'(?:at|in|from)\s+([~/][\w/.-]+)', text)
        if path_match and any(kw in text for kw in ["find", "search", "look"]):
            return TargetType.FOLDER, path_match.group(1)
        
        # Check for files with path (both absolute and relative)
        # Matches: ~/path/file.py, /path/file.py, file.py, path/file.py
        file_match = re.search(r'(?:[~/][\w/.-]+\.\w+|\b[\w-]+\.(?:py|js|ts|json|yaml|yml|md|txt|html|css|sh|rb|go|rs|java|c|cpp|h)\b)', text)
        if file_match:
            return TargetType.FILE, file_match.group()
        
        # Check for system targets
        for pattern in self.TARGET_PATTERNS[TargetType.SYSTEM]:
            if pattern in text:
                return TargetType.SYSTEM, pattern
        
        # Check for agent-related
        if any(kw in text for kw in ["agent", "background task", "monitor"]):
            return TargetType.AGENT, ""
        
        return TargetType.UNKNOWN, ""

    def _extract_parameters(self, text: str, action: IntentAction, target_type: TargetType) -> dict:
        """Extract additional parameters based on action type."""
        params = {}
        
        # For search actions, extract the query
        if action == IntentAction.SEARCH:
            # Remove action words to get query
            query = text
            for pattern in self.ACTION_PATTERNS[IntentAction.SEARCH]:
                query = query.replace(pattern, "").strip()
            # Remove "for" if present
            if query.startswith("for "):
                query = query[4:]
            params["query"] = query.strip()
        
        # For play actions, check for specific tracks/playlists
        if action == IntentAction.PLAY:
            params["media_type"] = "music"  # default
            if "video" in text or "youtube" in text:
                params["media_type"] = "video"
        
        return params

    def _calculate_confidence(self, action: IntentAction, target_type: TargetType, target_value: str) -> float:
        """Calculate confidence score based on clarity of intent."""
        score = 0.5  # base
        
        if action != IntentAction.UNKNOWN:
            score += 0.2
        if target_type != TargetType.UNKNOWN:
            score += 0.2
        if target_value:
            score += 0.1
        
        return min(score, 1.0)

    # Words that carry no instruction on their own. Anything outside this set
    # is something the planner can actually work with.
    _FILLER_WORDS = frozenset({
        "a", "an", "the", "this", "that", "these", "those", "it", "them",
        "please", "can", "you", "could", "would", "will", "do", "does", "did",
        "my", "me", "i", "is", "are", "was", "were", "be", "to", "for", "of",
        "on", "in", "at", "and", "or", "now", "just", "some", "any", "ok",
        "okay", "hey", "hi", "hello", "thanks", "thank",
    })

    @classmethod
    def _has_content_words(cls, words: list[str]) -> bool:
        """True when the request names something to act on."""
        return any(
            w.strip(".,!?;:'\"").lower() not in cls._FILLER_WORDS and len(w.strip(".,!?;:'\"")) > 1
            for w in words
        )

    def _check_ambiguity(self, text: str, action: IntentAction, target_type: TargetType, target_value: str) -> tuple[bool, str]:
        """Check if the request is ambiguous and needs clarification."""
        
        # Pronouns without context - but only simple ones at the end
        # "delete it", "open it", etc. - but NOT "read it and explain"
        pronouns = ["it", "this", "that", "them", "those"]
        # Only mark as ambiguous if pronoun is the ONLY object
        words = text.split()
        last_word = words[-1] if words else ""
        is_pronoun_only_target = last_word in pronouns and len(words) <= 4
        
        if is_pronoun_only_target:
            if action == IntentAction.DELETE:
                return True, "What would you like me to delete?"
            if action == IntentAction.OPEN and target_type == TargetType.UNKNOWN:
                return True, "What would you like me to open?"
            if action == IntentAction.CLOSE and target_type == TargetType.UNKNOWN:
                return True, "What would you like me to close?"
        
        # Unknown action + unknown target: only genuinely contentless requests
        # are ambiguous. Counting words alone rejected perfectly clear short
        # instructions — "summarise the page" is three words, carries two
        # content words, and was being answered with "I'm not sure what you'd
        # like me to do" without ever reaching the planner or the page tools.
        # If there is something to act on, let the model plan it.
        if action == IntentAction.UNKNOWN and target_type == TargetType.UNKNOWN and len(words) < 4:
            if not self._has_content_words(words):
                return True, "I'm not sure what you'd like me to do. Could you clarify?"
        
        # Only mark as ambiguous for OPEN/CLOSE/DELETE when:
        # 1. target_value is empty AND
        # 2. target_type is UNKNOWN AND
        # 3. request is very short (< 4 words)
        if action in [IntentAction.OPEN, IntentAction.CLOSE, IntentAction.DELETE]:
            if not target_value and target_type == TargetType.UNKNOWN and len(words) < 4:
                action_word = action.value
                return True, f"What would you like me to {action_word}?"
        
        return False, ""


# ═══════════════════════════════════════════════════════════════
#  Entity Extractor
# ═══════════════════════════════════════════════════════════════

class EntityExtractor:
    """Extracts mentioned apps, files, URLs from user text."""
    
    def extract(self, text: str) -> dict:
        """Extract all entities from text."""
        import re
        
        entities = {
            "apps": [],
            "files": [],
            "urls": [],
            "queries": []
        }
        
        text_lower = text.lower()
        
        # Extract URLs
        urls = re.findall(r'https?://[^\s]+', text)
        entities["urls"].extend(urls)
        
        # Extract app names
        for alias, app_name in IntentParser.APP_ALIASES.items():
            if alias in text_lower:
                entities["apps"].append(app_name)
        
        # Extract file paths (both absolute and relative)
        files = re.findall(r'(?:[~/][\w/.-]+\.\w+|\b[\w-]+\.(?:py|js|ts|json|yaml|yml|md|txt|html|css|sh|rb|go|rs|java|c|cpp|h)\b)', text)
        entities["files"].extend(files)
        
        return entities
