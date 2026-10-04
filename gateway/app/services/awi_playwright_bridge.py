"""
AWI Playwright Bridge — Phase 9
===============================

Bidirectional translation between AWI actions and real browser DOM manipulation.

Based on arXiv:2506.10953v1 — Compatibility with user interfaces section.
Enables AWI agents to interact with ANY existing web page without requiring
website-specific selectors or modifications.

Key Features:
- AWI-to-DOM: Convert semantic AWI actions to Playwright commands
- DOM-to-AWI: Extract semantic meaning from DOM for state representation
- Selector optimization: Generate robust CSS/XPath selectors
- State observation: Track DOM mutations for accurate state reporting

Architecture:
1. Agent sends AWI action (e.g., "search_and_sort")
2. Bridge translates to Playwright commands using semantic patterns
3. Commands execute in real browser
4. Bridge extracts resulting state as AWI representation
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING
from uuid import uuid4

from app.core.time import utc_now
from app.core.url_guard import check_outbound_url

if TYPE_CHECKING:
    from playwright.async_api import Page, BrowserContext

logger = logging.getLogger(__name__)

# Viewport bounds for browser contexts. The size feeds a real Chromium
# context, so an unbounded one is a memory-exhaustion lever; mirrored by the
# DOMBridgeSessionRequest schema bounds.
VIEWPORT_WIDTH_RANGE = (320, 3840)
VIEWPORT_HEIGHT_RANGE = (240, 2160)

# Scroll offsets are clamped to this magnitude (pixels).
_SCROLL_LIMIT = 20000
# The only script page.evaluate ever runs for a command. Offsets travel as a
# serialized argument, never as source text.
_SCROLL_SCRIPT = "([dx, dy]) => window.scrollBy(dx, dy)"
# The exact expression _handle_scroll emits for an EVALUATE command.
_SCROLL_EXPRESSION = re.compile(r"window\.scrollBy\((-?\d{1,5}), (-?\d{1,5})\)")

_REDACTED = "[REDACTED]"
# Inputs whose values are credentials or session secrets. Their values are
# never returned from page-state extraction or echoed by action previews.
_SENSITIVE_INPUT_TYPES = frozenset({"password", "hidden"})
_SENSITIVE_AUTOCOMPLETE_TOKENS = frozenset(
    {
        "current-password",
        "new-password",
        "one-time-code",
        "cc-number",
        "cc-csc",
        "cc-exp",
        "cc-exp-month",
        "cc-exp-year",
    }
)
_SENSITIVE_FIELD_NAME = re.compile(
    r"pass(?:word|wd|code|phrase)|secret|token|one[-_ ]?time|card[-_ ]?num|"
    r"(?<![a-z])(?:pwd|otp|ssn|cvv|cvc|csc|cc[-_ ]?(?:num|number|csc|exp))(?![a-z])",
    re.IGNORECASE,
)


def _selector_literal(value: Any) -> str:
    """Quote caller text as one Playwright/CSS string literal.

    JSON string escaping (backslash and double quote) is also valid CSS and
    Playwright selector escaping, so a quote in the text can neither close
    the literal nor smuggle a ``>>`` engine chain into the selector.
    """
    return json.dumps(str(value), ensure_ascii=False)


def _is_sensitive_field_name(*names: Any) -> bool:
    """Whether a field name, id or selector names a credential field."""
    return any(name and _SENSITIVE_FIELD_NAME.search(str(name)) for name in names)


def _is_sensitive_input(item: dict[str, Any]) -> bool:
    if str(item.get("type") or "").lower() in _SENSITIVE_INPUT_TYPES:
        return True
    autocomplete = str(item.get("autocomplete") or "").lower().split()
    if any(token in _SENSITIVE_AUTOCOMPLETE_TOKENS for token in autocomplete):
        return True
    return _is_sensitive_field_name(item.get("name"), item.get("id"))


def _redact_input_values(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace credential input values with a marker (empty stays empty)."""
    redacted = []
    for item in inputs:
        entry = dict(item)
        if _is_sensitive_input(entry):
            entry["value"] = _REDACTED if entry.get("value") else ""
        redacted.append(entry)
    return redacted


def _clamp_scroll(amount: int) -> int:
    return max(-_SCROLL_LIMIT, min(_SCROLL_LIMIT, amount))


class BrowserSessionLimitExceeded(RuntimeError):
    """Raised when DOM bridge session capacity is exhausted."""


class TranslationMode(str, Enum):
    """Browser automation mode."""

    PLAYWRIGHT = "playwright"
    CDP_DIRECT = "cdp_direct"
    SELENIUM_COMPAT = "selenium_compat"


class CommandType(str, Enum):
    """Types of Playwright commands."""

    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS = "press"
    HOVER = "hover"
    SCROLL = "scroll"
    GOTO = "goto"
    SET_INPUT_FILES = "set_input_files"
    EVALUATE = "evaluate"
    WAIT_FOR_SELECTOR = "wait_for_selector"
    WAIT_FOR_TIMEOUT = "wait_for_timeout"


@dataclass
class DOMElement:
    """Represents a DOM element with AWI metadata."""

    tag: str
    text_content: str
    attributes: dict[str, str]
    xpath: str
    css_selector: str
    bounding_box: Optional[dict[str, float]] = None
    is_interactive: bool = False
    role: Optional[str] = None
    label: Optional[str] = None
    semantic_hash: str = ""

    @property
    def element_id(self) -> str:
        content = f"{self.tag}:{self.text_content[:50] if self.text_content else ''}:{self.semantic_hash}"
        return hashlib.md5(content.encode()).hexdigest()[:12]


@dataclass
class PlaywrightCommand:
    """A command to execute in the browser."""

    command_type: CommandType
    target: str
    value: Any = None
    options: dict[str, Any] = field(default_factory=dict)
    wait_for_selectors: list[str] = field(default_factory=list)
    wait_for_timeout_ms: int = 0
    estimated_duration_ms: int = 500
    # The value is a credential (e.g. a password FILL): it is executed but
    # never echoed back by preview_action.
    sensitive: bool = False


@dataclass
class BridgeSession:
    """Browser context for a bridge session."""

    session_id: str
    browser_context_id: Optional[str] = None
    page_id: Optional[str] = None
    current_url: Optional[str] = None
    page_title: Optional[str] = None
    elements_cache: dict[str, DOMElement] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    last_activity: datetime = field(default_factory=utc_now)

    # Real Playwright objects (not dataclass fields, set at runtime)
    _page: Optional["Page"] = field(default=None, repr=False)
    _context: Optional["BrowserContext"] = field(default=None, repr=False)


@dataclass
class ExecutionResult:
    """Result of executing commands in the browser."""

    success: bool
    commands_executed: int
    new_url: Optional[str]
    new_title: Optional[str]
    error: Optional[str] = None
    elements_found: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0


class AWIPlaywrightBridge:
    """
    Bidirectional translation between AWI actions and browser DOM.

    This bridge allows AWI agents to interact with ANY web page without
    needing website-specific selectors. It provides:

    1. AWI-to-DOM Translation: Convert semantic actions like "search_and_sort"
       to specific Playwright commands targeting actual DOM elements.

    2. DOM-to-AWI Translation: Extract semantic meaning from DOM for
       state representation (e.g., "This is a search form with 3 fields").

    3. Selector Generation: Create robust CSS/XPath selectors that survive
       minor page changes.

    Semantic Patterns:
    The bridge uses semantic patterns to find elements. These are matched
    against common HTML patterns for buttons, inputs, forms, etc.

    Example patterns:
    - search_input: <input type="search"> or <input placeholder="*search*">
    - add_to_cart: <button class="add-to-cart"> or <a data-action="add-cart">
    - checkout: <button aria-label="checkout"> or <a class="checkout">
    """

    SEMANTIC_PATTERNS: dict[str, dict[str, Any]] = {
        "search_input": {
            "tags": ["input", "textarea"],
            "attributes": [
                "type~=search",
                "placeholder~=search",
                "aria-label~=search",
                "role=searchbox",
                "name~=search",
                "id~=search",
            ],
        },
        "email_input": {
            "tags": ["input"],
            "attributes": [
                "type=email",
                "name~=email",
                "id~=email",
                "autocomplete=email",
            ],
        },
        "password_input": {
            "tags": ["input"],
            "attributes": ["type=password"],
        },
        "text_input": {
            "tags": ["input"],
            "attributes": ["type=text", "type=search"],
        },
        "submit_button": {
            "tags": ["button", "input"],
            "attributes": [
                "type=submit",
                "role=button",
                "aria-label~=submit",
                "aria-label~=search",
                "aria-label~=go",
                "class~=submit",
                "class~=btn-primary",
            ],
        },
        "add_to_cart": {
            "tags": ["button", "a", "div"],
            "attributes": [
                "aria-label~=cart",
                "aria-label~=add",
                "data-action~=add.*cart",
                "class~=add-to-cart",
                "class~=addToCart",
                "class~=ATC",
                "id~=add-cart",
            ],
        },
        "checkout": {
            "tags": ["button", "a"],
            "attributes": [
                "aria-label~=checkout",
                "class~=checkout",
                "id~=checkout",
                "class~=proceed.*checkout",
                "text~=checkout",
            ],
        },
        "product_card": {
            "tags": ["div", "article", "li", "figure"],
            "attributes": [
                "role=article",
                "class~=product",
                "class~=item",
                "class~=product-card",
                "class~=item-card",
            ],
        },
        "sort_dropdown": {
            "tags": ["select", "button", "div"],
            "attributes": [
                "aria-label~=sort",
                "class~=sort",
                "role=listbox",
                "class~=sort.*dropdown",
                "id~=sort",
            ],
        },
        "filter_checkbox": {
            "tags": ["input"],
            "attributes": ["type=checkbox"],
        },
        "filter_dropdown": {
            "tags": ["select"],
            "attributes": ["class~=filter", "id~=filter"],
        },
        "pagination": {
            "tags": ["nav", "div"],
            "attributes": [
                "aria-label~=pagination",
                "class~=pagination",
                "class~=pager",
            ],
        },
        "login_button": {
            "tags": ["button", "a", "input"],
            "attributes": [
                "aria-label~=login",
                "aria-label~=sign.*in",
                "text~=login",
                "text~=sign.*in",
                "class~=login",
            ],
        },
        "logout_button": {
            "tags": ["button", "a"],
            "attributes": [
                "aria-label~=logout",
                "aria-label~=sign.*out",
                "text~=logout",
                "text~=sign.*out",
                "class~=logout",
            ],
        },
        "cart_icon": {
            "tags": ["a", "button", "div"],
            "attributes": [
                "aria-label~=cart",
                "class~=cart",
                "class~=shopping-cart",
                "id~=cart",
            ],
        },
        "form_field": {
            "tags": ["input", "textarea", "select"],
            "attributes": ["name", "id", "aria-label", "placeholder"],
        },
        "modal_close": {
            "tags": ["button", "a"],
            "attributes": [
                "aria-label~=close",
                "class~=close",
                "class~=modal-close",
                "role=button",
            ],
        },
        "image_gallery": {
            "tags": ["div"],
            "attributes": ["class~=gallery", "class~=image.*viewer", "role=img"],
        },
        "tab_button": {
            "tags": ["button", "a"],
            "attributes": ["role=tab", "class~=tab"],
        },
    }

    SORT_VALUE_MAPPINGS: dict[str, list[str]] = {
        "price_low": ["price-asc", "low_to_high", "price:asc", "pricelow", "price_low"],
        "price_high": [
            "price-desc",
            "high_to_low",
            "price:desc",
            "pricehigh",
            "price_high",
        ],
        "relevance": ["relevance", "best_match", "relevancy", ""],
        "newest": ["newest", "date-desc", "created:desc", "date_new", "newest_first"],
        "rating": ["rating", "best_rating", "avg_rating:desc", "top_rated"],
        "popularity": ["popular", "best_selling", "most_popular", "popularity"],
        "name_asc": ["name-asc", "name:asc", "alphabetical"],
        "name_desc": ["name-desc", "name:desc"],
    }

    def __init__(
        self,
        mode: TranslationMode = TranslationMode.PLAYWRIGHT,
        headless: bool = True,
        browser_type: str = "chromium",
        default_timeout_ms: int = 30000,
        max_sessions: int = 8,
        session_ttl_seconds: int = 900,
    ):
        """
        Initialize the AWI Playwright Bridge.

        Args:
            mode: Browser automation mode (playwright, cdp_direct, selenium_compat).
            headless: Run browser in headless mode.
            browser_type: Browser to use (chromium, firefox, webkit).
            default_timeout_ms: Default timeout for commands.
        """
        self._mode = mode
        self._headless = headless
        self._browser_type = browser_type
        self._default_timeout_ms = default_timeout_ms
        self._max_sessions = max_sessions
        self._session_ttl_seconds = session_ttl_seconds

        self._sessions: dict[str, BridgeSession] = {}

        # Typed Any: populated lazily from the optional `playwright` package,
        # which may not be installed (see _init_playwright's ImportError guard).
        self._playwright: Any = None
        self._browser: Any = None
        self._playwright_initialized = False

    # ─────────────────────────────────────────────────────────────────────────
    # Session Management
    # ─────────────────────────────────────────────────────────────────────────

    async def create_session(
        self,
        target_url: str,
        headless: Optional[bool] = None,
        viewport: Optional[tuple[int, int]] = None,
    ) -> BridgeSession:
        """
        Create a new browser session for AWI interaction.

        Args:
            target_url: URL to navigate to.
            headless: Override default headless setting.
            viewport: (width, height) tuple for viewport.

        Returns:
            BridgeSession with session_id and initial state.
        """
        block_reason = await check_outbound_url(target_url)
        if block_reason:
            raise ValueError(f"navigation blocked ({block_reason}): {target_url}")

        if viewport is not None:
            width, height = viewport
            min_w, max_w = VIEWPORT_WIDTH_RANGE
            min_h, max_h = VIEWPORT_HEIGHT_RANGE
            if not (min_w <= width <= max_w and min_h <= height <= max_h):
                raise ValueError(
                    f"viewport must be between {min_w}x{min_h} and "
                    f"{max_w}x{max_h}, got {width}x{height}"
                )

        await self.cleanup_expired_sessions()

        if len(self._sessions) >= self._max_sessions:
            raise BrowserSessionLimitExceeded(
                f"DOM bridge session limit exceeded ({self._max_sessions})"
            )

        session_id = str(uuid4())

        session = BridgeSession(
            session_id=session_id,
            current_url=target_url,
        )

        if viewport:
            session_id = session_id  # Keep for later use

        self._sessions[session_id] = session

        if not self._playwright_initialized:
            await self._init_playwright()

        try:
            await self._create_browser_context(session, headless, viewport)
            await self._navigate_to(session, target_url)
        except Exception as e:
            logger.warning(f"Browser setup for session {session_id}: {e}")

        logger.info(f"Created DOM bridge session {session_id} for {target_url}")

        return session

    async def destroy_session(self, session_id: str) -> bool:
        """
        Destroy a browser session and cleanup resources.

        Args:
            session_id: The session to destroy.

        Returns:
            True if session was destroyed, False if not found.
        """
        session = self._sessions.get(session_id)
        if not session:
            return False

        if session.browser_context_id:
            try:
                await self._close_browser_context(session)
            except Exception as e:
                logger.debug(f"Error closing context for {session_id}: {e}")

        del self._sessions[session_id]

        logger.info(f"Destroyed DOM bridge session {session_id}")

        return True

    async def get_session(self, session_id: str) -> Optional[BridgeSession]:
        """Get a session by ID."""
        await self.cleanup_expired_sessions()
        return self._sessions.get(session_id)

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions."""
        await self.cleanup_expired_sessions()
        return [
            {
                "session_id": s.session_id,
                "current_url": s.current_url,
                "created_at": s.created_at.isoformat(),
                "last_activity": s.last_activity.isoformat(),
            }
            for s in self._sessions.values()
        ]

    async def cleanup_expired_sessions(self) -> dict[str, int]:
        """Destroy idle browser sessions so forgotten contexts do not pile up."""
        if self._session_ttl_seconds <= 0:
            return {"expired": 0, "active": len(self._sessions)}

        cutoff = utc_now() - timedelta(seconds=self._session_ttl_seconds)
        expired_session_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if session.last_activity < cutoff
        ]

        for session_id in expired_session_ids:
            await self.destroy_session(session_id)

        return {"expired": len(expired_session_ids), "active": len(self._sessions)}

    # ─────────────────────────────────────────────────────────────────────────
    # AWI-to-DOM Translation
    # ─────────────────────────────────────────────────────────────────────────

    async def translate_action(
        self,
        session_id: str,
        action: str,
        parameters: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """
        Translate an AWI action to Playwright commands.

        This is the core of the bridge: taking semantic AWI actions
        and generating specific DOM manipulation commands.

        Args:
            session_id: Browser session.
            action: AWI action name (e.g., "search_and_sort", "add_to_cart").
            parameters: Action parameters (e.g., {"query": "laptop", "sort_by": "price"}).

        Returns:
            List of Playwright commands to execute.

        Raises:
            ValueError: If session not found or action unsupported.
        """
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        handlers = {
            "search_and_sort": self._handle_search_and_sort,
            "add_to_cart": self._handle_add_to_cart,
            "checkout": self._handle_checkout,
            "fill_form": self._handle_fill_form,
            "login": self._handle_login,
            "logout": self._handle_logout,
            "navigate_to": self._handle_navigate_to,
            "click_button": self._handle_click_button,
            "select_option": self._handle_select_option,
            "scroll": self._handle_scroll,
            "upload_file": self._handle_upload_file,
            "click_element": self._handle_click_element,
            "fill_field": self._handle_fill_field,
            "hover_element": self._handle_hover_element,
            "close_modal": self._handle_close_modal,
        }

        handler = handlers.get(action.lower())
        if not handler:
            raise ValueError(f"Unsupported AWI action: {action}")

        try:
            commands = await handler(session, parameters)
            session.last_activity = utc_now()
            return commands
        except Exception as e:
            logger.error(f"Action translation failed for {action}: {e}")
            raise

    async def preview_action(
        self,
        session_id: str,
        action: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Preview what commands will be generated for an action.

        Useful for debugging and understanding what the bridge will do.

        Args:
            session_id: Browser session.
            action: AWI action name.
            parameters: Action parameters.

        Returns:
            Dict with commands and element counts.
        """
        commands = await self.translate_action(session_id, action, parameters)
        session = self._sessions.get(session_id)

        elements_found = {}
        if session is not None:
            for semantic_type in self.SEMANTIC_PATTERNS.keys():
                try:
                    element = await self._find_semantic_element(session, semantic_type)
                    if element:
                        elements_found[semantic_type] = 1
                except Exception:
                    pass

        estimated_duration = sum(c.estimated_duration_ms for c in commands)

        return {
            "session_id": session_id,
            "action": action,
            "commands": [
                {
                    "type": c.command_type.value,
                    "target": c.target,
                    "value": _REDACTED if c.sensitive and c.value else c.value,
                    "options": c.options,
                }
                for c in commands
            ],
            "estimated_duration_ms": estimated_duration,
            "elements_found": elements_found,
        }

    async def execute_commands(
        self,
        session_id: str,
        commands: list[PlaywrightCommand],
    ) -> ExecutionResult:
        """
        Execute a list of Playwright commands.

        Args:
            session_id: Browser session.
            commands: List of commands to execute.

        Returns:
            ExecutionResult with success status and results.
        """
        session = self._sessions.get(session_id)
        if not session:
            return ExecutionResult(
                success=False,
                commands_executed=0,
                new_url=None,
                new_title=None,
                error=f"Session {session_id} not found",
            )

        start_time = utc_now()
        executed = 0
        error = None

        for cmd in commands:
            try:
                await self._execute_single_command(session, cmd)
                executed += 1

                if cmd.command_type == CommandType.GOTO:
                    session.current_url = cmd.value

            except Exception as e:
                error = str(e)
                logger.error(f"Command {cmd.command_type.value} failed: {e}")
                break

        session.last_activity = utc_now()
        duration_ms = int((utc_now() - start_time).total_seconds() * 1000)

        return ExecutionResult(
            success=error is None,
            commands_executed=executed,
            new_url=session.current_url,
            new_title=session.page_title,
            error=error,
            duration_ms=duration_ms,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Action Handlers
    # ─────────────────────────────────────────────────────────────────────────

    async def _handle_search_and_sort(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle search_and_sort action."""
        commands = []
        query = params.get("query", "")
        sort_by = params.get("sort_by")

        search_element = await self._find_semantic_element(session, "search_input")

        if search_element:
            commands.append(
                PlaywrightCommand(
                    command_type=CommandType.FILL,
                    target=search_element.css_selector,
                    value=query,
                    wait_for_selectors=[
                        "[data-loading=false]",
                        ".results",
                        ".products",
                        ".items",
                    ],
                    estimated_duration_ms=300,
                )
            )

            commands.append(
                PlaywrightCommand(
                    command_type=CommandType.PRESS,
                    target=search_element.css_selector,
                    value="Enter",
                    wait_for_timeout_ms=1000,
                    estimated_duration_ms=500,
                )
            )
        else:
            logger.warning(f"No search input found for session {session.session_id}")

        if sort_by:
            sort_element = await self._find_semantic_element(session, "sort_dropdown")

            if sort_element:
                if sort_element.tag == "select":
                    sort_value = self._get_sort_option_value(sort_by)
                    commands.append(
                        PlaywrightCommand(
                            command_type=CommandType.SELECT,
                            target=sort_element.css_selector,
                            value=sort_value,
                            estimated_duration_ms=400,
                        )
                    )
                else:
                    commands.append(
                        PlaywrightCommand(
                            command_type=CommandType.CLICK,
                            target=sort_element.css_selector,
                            estimated_duration_ms=300,
                        )
                    )

        return commands

    async def _handle_add_to_cart(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle add_to_cart action."""
        product_identifier = params.get("product", "")

        cart_element = await self._find_semantic_element(session, "add_to_cart")

        if not cart_element:
            raise ValueError("No add-to-cart element found on page")

        commands = [
            PlaywrightCommand(
                command_type=CommandType.CLICK,
                target=cart_element.css_selector,
                options={"timeout": 10000},
                wait_for_selectors=[".cart-updated", "[data-cart-count]"],
                wait_for_timeout_ms=500,
                estimated_duration_ms=800,
            )
        ]

        if product_identifier:
            pass

        return commands

    async def _handle_checkout(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle checkout action."""
        checkout_element = await self._find_semantic_element(session, "checkout")

        if not checkout_element:
            raise ValueError("No checkout element found on page")

        return [
            PlaywrightCommand(
                command_type=CommandType.CLICK,
                target=checkout_element.css_selector,
                options={"timeout": 15000},
                wait_for_selectors=[
                    "[data-checkout-page]",
                    "#checkout",
                    ".payment-form",
                ],
                estimated_duration_ms=1000,
            )
        ]

    async def _handle_fill_form(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle fill_form action."""
        commands = []
        form_data = params.get("data", {})

        for field_name, value in form_data.items():
            field_type = self._infer_field_type(field_name, value)
            element = await self._find_semantic_element(session, field_type)

            if not element:
                element = await self._find_element_by_label(session, field_name)

            if not element:
                element = await self._find_element_by_name(session, field_name)

            if element:
                commands.append(
                    PlaywrightCommand(
                        command_type=CommandType.FILL,
                        target=element.css_selector,
                        value=str(value),
                        estimated_duration_ms=200,
                        sensitive=(
                            field_type == "password_input"
                            or _is_sensitive_field_name(field_name)
                            or _is_sensitive_input(element.attributes)
                        ),
                    )
                )
            else:
                logger.warning(f"Field not found: {field_name}")

        return commands

    async def _handle_login(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle login action."""
        commands = []

        email = params.get("email", "")
        password = params.get("password", "")
        remember = params.get("remember", False)

        email_element = await self._find_semantic_element(session, "email_input")

        if not email_element:
            email_element = await self._find_element_by_label(session, "email")
            if not email_element:
                email_element = await self._find_element_by_name(session, "email")

        if email_element:
            commands.append(
                PlaywrightCommand(
                    command_type=CommandType.FILL,
                    target=email_element.css_selector,
                    value=email,
                    estimated_duration_ms=200,
                )
            )

        password_element = await self._find_semantic_element(session, "password_input")

        if not password_element:
            password_element = await self._find_element_by_label(session, "password")
            if not password_element:
                password_element = await self._find_element_by_name(session, "password")

        if password_element:
            commands.append(
                PlaywrightCommand(
                    command_type=CommandType.FILL,
                    target=password_element.css_selector,
                    value=password,
                    estimated_duration_ms=200,
                    sensitive=True,
                )
            )

        if remember:
            remember_element = await self._find_semantic_element(
                session, "filter_checkbox"
            )
            if remember_element:
                commands.append(
                    PlaywrightCommand(
                        command_type=CommandType.CLICK,
                        target=remember_element.css_selector,
                        estimated_duration_ms=100,
                    )
                )

        login_button = await self._find_semantic_element(session, "login_button")

        if not login_button:
            login_button = await self._find_element_by_label(session, "login")
            if not login_button:
                login_button = await self._find_semantic_element(
                    session, "submit_button"
                )

        if login_button:
            commands.append(
                PlaywrightCommand(
                    command_type=CommandType.CLICK,
                    target=login_button.css_selector,
                    wait_for_selectors=["[data-logged-in]", ".dashboard", "#account"],
                    estimated_duration_ms=1000,
                )
            )

        return commands

    async def _handle_logout(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle logout action."""
        logout_button = await self._find_semantic_element(session, "logout_button")

        if not logout_button:
            logout_button = await self._find_element_by_label(session, "logout")

        if not logout_button:
            raise ValueError("No logout button found on page")

        return [
            PlaywrightCommand(
                command_type=CommandType.CLICK,
                target=logout_button.css_selector,
                wait_for_selectors=["[data-logged-out]", "#login", ".login-page"],
                estimated_duration_ms=500,
            )
        ]

    async def _handle_navigate_to(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle navigate_to action."""
        url = params.get("url")

        if not url:
            raise ValueError("URL required for navigate_to")

        block_reason = await check_outbound_url(url)
        if block_reason:
            raise ValueError(f"navigation blocked ({block_reason}): {url}")

        return [
            PlaywrightCommand(
                command_type=CommandType.GOTO,
                target=url,
                wait_for_selectors=["body"],
                estimated_duration_ms=2000,
            )
        ]

    async def _handle_click_button(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle click_button action."""
        button_identifier = params.get("button", "")

        element = await self._find_semantic_element(session, "submit_button")

        if not element and button_identifier:
            element = await self._find_element_by_label(session, button_identifier)

        if not element and button_identifier:
            element = await self._find_element_by_text(session, button_identifier)

        if not element:
            raise ValueError(f"Button not found: {button_identifier}")

        return [
            PlaywrightCommand(
                command_type=CommandType.CLICK,
                target=element.css_selector,
                estimated_duration_ms=500,
            )
        ]

    async def _handle_select_option(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle select_option action."""
        selector_label = params.get("selector", "sort_dropdown")
        value = params.get("value", "")

        element = await self._find_semantic_element(session, selector_label)

        if not element:
            raise ValueError(f"Select element not found: {selector_label}")

        if element.tag == "select":
            return [
                PlaywrightCommand(
                    command_type=CommandType.SELECT,
                    target=element.css_selector,
                    value=value,
                    estimated_duration_ms=300,
                )
            ]
        else:
            return [
                PlaywrightCommand(
                    command_type=CommandType.CLICK,
                    target=element.css_selector,
                    estimated_duration_ms=200,
                )
            ]

    async def _handle_scroll(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle scroll action."""
        direction = params.get("direction", "down")
        # Only a plain integer is accepted. The offsets are signed here in
        # Python (never by prefixing "-" to text, which made a negative
        # amount "--300"), and _execute_single_command re-parses the
        # expression and passes the integers to a fixed script as data.
        try:
            amount = int(params.get("amount", 300))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("scroll amount must be an integer")
        amount = _clamp_scroll(amount)

        if direction == "up":
            dx, dy = 0, -amount
        elif direction == "left":
            dx, dy = -amount, 0
        elif direction == "right":
            dx, dy = amount, 0
        else:
            dx, dy = 0, amount

        return [
            PlaywrightCommand(
                command_type=CommandType.EVALUATE,
                target=f"window.scrollBy({dx}, {dy})",
                wait_for_timeout_ms=200,
                estimated_duration_ms=200,
            )
        ]

    async def _handle_upload_file(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle upload_file action."""
        file_path = params.get("file_path")
        field_name = params.get("field_name", "file")

        if not file_path:
            raise ValueError("file_path required for upload_file")

        file_path = self._confine_upload_path(file_path)

        file_input = await self._find_element_by_label(session, field_name)

        if not file_input:
            file_input = await self._find_element_by_name(session, field_name)

        if not file_input:
            file_input = await self._find_semantic_element(session, "form_field")

        if not file_input:
            raise ValueError("File input not found on page")

        return [
            PlaywrightCommand(
                command_type=CommandType.SET_INPUT_FILES,
                target=file_input.css_selector,
                value=file_path,
                estimated_duration_ms=500,
            )
        ]

    @staticmethod
    def _confine_upload_path(file_path: str) -> str:
        """Resolve an upload path and require it to stay inside AWI_UPLOAD_DIR.

        The path feeds Playwright's set_input_files, which reads the file
        from OUR filesystem and sends it to the remote page — without
        confinement any host file (keys, credentials) is one action away
        from exfiltration. Relative paths are interpreted against the
        configured upload directory; absolute paths must resolve inside it.
        """
        from app.core.config import get_settings

        upload_dir = get_settings().AWI_UPLOAD_DIR
        if not upload_dir:
            raise ValueError(
                "file uploads are disabled (AWI_UPLOAD_DIR is not configured)"
            )
        root = Path(upload_dir).resolve()
        candidate = Path(file_path)
        resolved = (
            candidate if candidate.is_absolute() else root / candidate
        ).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("upload path escapes the configured upload directory")
        return str(resolved)

    async def _handle_click_element(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle click_element action with explicit selector."""
        selector = params.get("selector", "")

        if not selector:
            raise ValueError("selector required for click_element")

        return [
            PlaywrightCommand(
                command_type=CommandType.CLICK,
                target=selector,
                options=params.get("options", {}),
                estimated_duration_ms=500,
            )
        ]

    async def _handle_fill_field(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle fill_field action with explicit selector."""
        selector = params.get("selector", "")
        value = params.get("value", "")

        if not selector:
            raise ValueError("selector required for fill_field")

        return [
            PlaywrightCommand(
                command_type=CommandType.FILL,
                target=selector,
                value=value,
                estimated_duration_ms=200,
                sensitive=_is_sensitive_field_name(selector),
            )
        ]

    async def _handle_hover_element(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle hover_element action."""
        selector = params.get("selector", "")

        if not selector:
            raise ValueError("selector required for hover_element")

        return [
            PlaywrightCommand(
                command_type=CommandType.HOVER,
                target=selector,
                estimated_duration_ms=200,
            )
        ]

    async def _handle_close_modal(
        self,
        session: BridgeSession,
        params: dict[str, Any],
    ) -> list[PlaywrightCommand]:
        """Handle close_modal action."""
        close_button = await self._find_semantic_element(session, "modal_close")

        if not close_button:
            close_button = await self._find_element_by_text(session, "close")
            if not close_button:
                close_button = await self._find_element_by_text(session, "×")

        if not close_button:
            raise ValueError("No modal close button found")

        return [
            PlaywrightCommand(
                command_type=CommandType.CLICK,
                target=close_button.css_selector,
                estimated_duration_ms=200,
            )
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # DOM Element Discovery
    # ─────────────────────────────────────────────────────────────────────────

    async def _find_semantic_element(
        self,
        session: BridgeSession,
        semantic_type: str,
    ) -> Optional[DOMElement]:
        """Find a DOM element by semantic type."""
        pattern = self.SEMANTIC_PATTERNS.get(semantic_type)
        if not pattern:
            return None

        selectors = self._build_selectors_from_pattern(pattern)

        for selector in selectors:
            try:
                elements = await self._query_elements(session, selector)
                if elements:
                    return elements[0]
            except Exception:
                pass

        return None

    async def _find_element_by_label(
        self,
        session: BridgeSession,
        label: str,
    ) -> Optional[DOMElement]:
        """Find a form element by its associated label."""
        label_lower = label.lower()
        slug = label_lower.replace(" ", "-")

        # Caller text is always quoted via _selector_literal so it cannot
        # break out of the literal (or chain engines with ">>").
        label_patterns = [
            f"label:has-text({_selector_literal(label)})",
            f"[aria-label={_selector_literal(label)}]",
            f"[aria-label*={_selector_literal(label_lower)}]",
            f"[id={_selector_literal(slug)}]",
            f"[id={_selector_literal('label-' + slug)}]",
        ]

        for selector in label_patterns:
            try:
                elements = await self._query_elements(session, selector)
                if elements:
                    element = elements[0]

                    if element.tag == "label":
                        for_attr = element.attributes.get("for")
                        if for_attr:
                            literal = _selector_literal(for_attr)
                            input_selector = f"[id={literal}], [name={literal}]"
                            inputs = await self._query_elements(session, input_selector)
                            if inputs:
                                return inputs[0]

                    return element
            except Exception:
                pass

        return None

    async def _find_element_by_name(
        self,
        session: BridgeSession,
        name: str,
    ) -> Optional[DOMElement]:
        """Find an element by its name attribute."""
        name_lower = name.lower()

        selectors = [
            f"[name={_selector_literal(name)}]",
            f"[name={_selector_literal(name_lower)}]",
            f"[data-name={_selector_literal(name)}]",
        ]

        for selector in selectors:
            try:
                elements = await self._query_elements(session, selector)
                if elements:
                    return elements[0]
            except Exception:
                pass

        return None

    async def _find_element_by_text(
        self,
        session: BridgeSession,
        text: str,
    ) -> Optional[DOMElement]:
        """Find an element containing specific text."""
        literal = _selector_literal(text)
        selectors = [
            f"text={literal}",
            f"*:text-is({literal})",
            f"button:has-text({literal})",
            f"a:has-text({literal})",
        ]

        for selector in selectors:
            try:
                elements = await self._query_elements(session, selector)
                if elements:
                    return elements[0]
            except Exception:
                pass

        return None

    async def _query_elements(
        self,
        session: BridgeSession,
        selector: str,
    ) -> list[DOMElement]:
        """
        Query elements using Playwright page.query_selector_all().

        Args:
            session: The browser session.
            selector: CSS selector to query.

        Returns:
            List of DOMElement objects populated from real DOM.
        """
        if not session._page:
            return []

        try:
            elements = []
            playwright_elements = await session._page.query_selector_all(selector)

            for el in playwright_elements:
                tag = await el.evaluate("el => el.tagName")
                text_content = await el.inner_text()
                bounding_box = await el.bounding_box()

                attributes = {}
                for attr in [
                    "id",
                    "name",
                    "type",
                    "href",
                    "placeholder",
                    "aria-label",
                    "role",
                    "class",
                ]:
                    try:
                        val = await el.get_attribute(attr)
                        if val:
                            attributes[attr] = val
                    except Exception:
                        pass

                css_selector = await el.evaluate("""
                    el => {
                        if (el.id) return '#' + el.id;
                        const path = [];
                        while (el.parentElement && path.length < 5) {
                            let sibling = el, name = el.tagName.toLowerCase();
                            while (sibling = sibling.previousElementSibling) {}
                            path.unshift(name + (sibling ? '+' : ':first-child'));
                            el = el.parentElement;
                        }
                        return path.join(' > ');
                    }
                """)

                element = DOMElement(
                    tag=tag.lower(),
                    text_content=text_content or "",
                    attributes=attributes,
                    xpath="",  # Could add XPath generation if needed
                    css_selector=css_selector,
                    bounding_box=bounding_box,
                    is_interactive=tag.lower()
                    in ["button", "a", "input", "select", "textarea"],
                    role=attributes.get("role"),
                    label=attributes.get("aria-label") or text_content[:50]
                    if text_content
                    else None,
                )
                elements.append(element)

            return elements

        except Exception as e:
            logger.warning(f"Element query failed for selector '{selector}': {e}")
            return []

    def _build_selectors_from_pattern(self, pattern: dict[str, Any]) -> list[str]:
        """Build CSS selectors from semantic pattern."""
        selectors = []
        tags = pattern.get("tags", ["*"])
        attributes = pattern.get("attributes", [])

        for tag in tags:
            if attributes:
                attr_selectors = "".join(f"[{attr}]" for attr in attributes)
                selectors.append(f"{tag}{attr_selectors}")
            else:
                selectors.append(tag)

        return selectors

    def _infer_field_type(self, field_name: str, value: Any) -> str:
        """Infer semantic field type from field name."""
        name_lower = field_name.lower()

        if "email" in name_lower or "@" in str(value):
            return "email_input"
        elif "password" in name_lower:
            return "password_input"
        elif "search" in name_lower:
            return "search_input"
        elif "file" in name_lower or "upload" in name_lower:
            return "form_field"
        elif "phone" in name_lower or "tel" in name_lower:
            return "text_input"
        elif "address" in name_lower:
            return "text_input"
        elif "zip" in name_lower or "postal" in name_lower:
            return "text_input"

        return "form_field"

    def _get_sort_option_value(self, sort_by: str) -> str:
        """Map sort_by parameter to actual option value."""
        sort_lower = sort_by.lower()

        for key, values in self.SORT_VALUE_MAPPINGS.items():
            if sort_lower in [key] + [v.lower() for v in values]:
                return values[0] if values else sort_by

        return sort_by

    # ─────────────────────────────────────────────────────────────────────────
    # DOM-to-AWI Translation (State Extraction)
    # ─────────────────────────────────────────────────────────────────────────

    async def extract_state_representation(
        self,
        session_id: str,
        representation_type: str = "summary",
        include_elements: bool = True,
    ) -> dict[str, Any]:
        """
        Extract a state representation from the current DOM.

        Args:
            session_id: Browser session.
            representation_type: Type of representation (summary, accessibility_tree, json_structure).
            include_elements: Whether to include element details.

        Returns:
            AWI representation dict.
        """
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        extractors = {
            "summary": self._extract_summary,
            "accessibility_tree": self._extract_accessibility_tree,
            "json_structure": self._extract_json_structure,
            "full_dom": self._extract_full_dom,
        }

        extractor = extractors.get(representation_type, self._extract_summary)

        representation = await extractor(session, include_elements)

        return {
            "session_id": session_id,
            "url": session.current_url,
            **representation,
        }

    async def _extract_summary(
        self,
        session: BridgeSession,
        include_elements: bool,
    ) -> dict[str, Any]:
        """Extract a semantic summary of the current page."""
        page_type = self._classify_page_type(session)

        interactive_elements = []
        if include_elements:
            interactive_elements = await self._get_interactive_elements(session)

        return {
            "title": session.page_title or "",
            "page_type": page_type,
            "main_content": await self._get_main_content(session),
            "interactive_elements": interactive_elements,
            "forms": await self._extract_forms(session),
            "navigation": await self._extract_navigation(session),
        }

    async def _extract_accessibility_tree(
        self,
        session: BridgeSession,
        include_elements: bool,
    ) -> dict[str, Any]:
        """Extract accessibility-focused element tree."""
        return {
            "title": session.page_title or "",
            "page_type": self._classify_page_type(session),
            "tree": {
                "role": "document",
                "name": session.page_title,
                "children": [],
            },
        }

    async def _extract_json_structure(
        self,
        session: BridgeSession,
        include_elements: bool,
    ) -> dict[str, Any]:
        """Extract JSON-serializable DOM structure."""
        return {
            "title": session.page_title or "",
            "page_type": self._classify_page_type(session),
            "forms": await self._extract_forms(session),
            "buttons": await self._get_buttons(session),
            "links": await self._get_links(session),
            "inputs": await self._get_inputs(session),
        }

    async def _extract_full_dom(
        self,
        session: BridgeSession,
        include_elements: bool,
    ) -> dict[str, Any]:
        """Extract full DOM content."""
        return {
            "title": session.page_title or "",
            "html": await self._get_page_html(session),
            "page_type": self._classify_page_type(session),
        }

    def _classify_page_type(self, session: BridgeSession) -> str:
        """Classify the page type based on URL and content."""
        url = (session.current_url or "").lower()
        title = (session.page_title or "").lower()

        combined = f"{url} {title}"

        if any(x in combined for x in ["checkout", "payment", "order", "cart"]):
            return "checkout"
        elif any(x in combined for x in ["login", "signin", "sign-in", "auth"]):
            return "login"
        elif any(x in combined for x in ["shop", "product", "store", "catalog"]):
            return "product_listing"
        elif any(x in combined for x in ["cart", "basket"]):
            return "cart"
        elif any(
            x in combined for x in ["account", "profile", "settings", "dashboard"]
        ):
            return "account"
        elif any(x in combined for x in ["search", "results"]):
            return "search_results"
        elif any(x in combined for x in ["product", "detail", "item"]):
            return "product_detail"

        return "generic"

    async def _get_main_content(self, session: BridgeSession) -> str:
        """Get main page content text using Playwright."""
        if not session._page:
            return ""

        try:
            content = await session._page.evaluate("""
                () => {
                    const main = document.querySelector('main, [role="main"], article, .content, #content');
                    if (main) return main.innerText;
                    return document.body.innerText.slice(0, 2000);  // Limit to 2000 chars
                }
            """)
            return content or ""
        except Exception as e:
            logger.warning(f"Failed to get main content: {e}")
            return ""

    async def _get_page_html(self, session: BridgeSession) -> str:
        """Get full page HTML."""
        if not session._page:
            return ""

        try:
            return await session._page.content()
        except Exception as e:
            logger.warning(f"Failed to get page HTML: {e}")
            return ""

    async def _get_interactive_elements(self, session: BridgeSession) -> list[dict]:
        """Get all interactive elements."""
        interactive = []

        for semantic_type in [
            "submit_button",
            "add_to_cart",
            "checkout",
            "login_button",
        ]:
            element = await self._find_semantic_element(session, semantic_type)
            if element:
                interactive.append(
                    {
                        "semantic_type": semantic_type,
                        "tag": element.tag,
                        "text": element.text_content,
                        "selector": element.css_selector,
                    }
                )

        return interactive

    async def _extract_forms(self, session: BridgeSession) -> list[dict]:
        """Extract form information using Playwright."""
        if not session._page:
            return []

        try:
            forms = await session._page.evaluate("""
                () => {
                    const forms = document.querySelectorAll('form');
                    return Array.from(forms).map(form => {
                        const inputs = Array.from(form.querySelectorAll('input, textarea, select')).map(input => ({
                            name: input.name,
                            id: input.id,
                            type: input.type || input.tagName.toLowerCase(),
                            placeholder: input.placeholder,
                            required: input.required,
                        }));
                        return {
                            id: form.id,
                            action: form.action,
                            method: form.method,
                            inputs: inputs,
                        };
                    });
                }
            """)
            return forms or []
        except Exception as e:
            logger.warning(f"Failed to extract forms: {e}")
            return []

    async def _extract_navigation(self, session: BridgeSession) -> list[dict]:
        """Extract navigation structure using Playwright."""
        if not session._page:
            return []

        try:
            nav = await session._page.evaluate("""
                () => {
                    const navs = document.querySelectorAll('nav, [role="navigation"]');
                    const items = [];
                    navs.forEach(n => {
                        const links = Array.from(n.querySelectorAll('a')).slice(0, 20);
                        links.forEach(a => {
                            items.push({
                                text: a.innerText || '',
                                href: a.href,
                                visible: a.offsetParent !== null,
                            });
                        });
                    });
                    return items;
                }
            """)
            return nav or []
        except Exception as e:
            logger.warning(f"Failed to extract navigation: {e}")
            return []

    async def _get_buttons(self, session: BridgeSession) -> list[dict]:
        """Get button elements using Playwright."""
        if not session._page:
            return []

        try:
            buttons = await session._page.evaluate("""
                () => {
                    const btns = document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]');
                    return Array.from(btns).map(btn => ({
                        text: btn.innerText || btn.value || '',
                        id: btn.id,
                        name: btn.name,
                        type: btn.type || 'button',
                        disabled: btn.disabled,
                        visible: btn.offsetParent !== null,
                    }));
                }
            """)
            return buttons or []
        except Exception as e:
            logger.warning(f"Failed to get buttons: {e}")
            return []

    async def _get_links(self, session: BridgeSession) -> list[dict]:
        """Get link elements using Playwright."""
        if not session._page:
            return []

        try:
            links = await session._page.evaluate("""
                () => {
                    const anchors = document.querySelectorAll('a[href]');
                    return Array.from(anchors).slice(0, 50).map(a => ({
                        text: a.innerText || '',
                        href: a.href,
                        id: a.id,
                        visible: a.offsetParent !== null,
                    }));
                }
            """)
            return links or []
        except Exception as e:
            logger.warning(f"Failed to get links: {e}")
            return []

    async def _get_inputs(self, session: BridgeSession) -> list[dict]:
        """Get input elements using Playwright.

        Credential values (password/hidden inputs, credential autocomplete
        hints, credential-like names) are redacted: password values never
        leave the page, and _redact_input_values enforces the full policy.
        """
        if not session._page:
            return []

        try:
            inputs = await session._page.evaluate("""
                () => {
                    const inputs = document.querySelectorAll('input, textarea, select');
                    return Array.from(inputs).map(input => ({
                        name: input.name,
                        id: input.id,
                        type: input.type || input.tagName.toLowerCase(),
                        placeholder: input.placeholder,
                        autocomplete: input.getAttribute('autocomplete') || '',
                        value: input.type === 'password'
                            ? (input.value ? '[REDACTED]' : '')
                            : (input.value || ''),
                        required: input.required,
                        disabled: input.disabled,
                    }));
                }
            """)
            return _redact_input_values(inputs or [])
        except Exception as e:
            logger.warning(f"Failed to get inputs: {e}")
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # Command Execution
    # ─────────────────────────────────────────────────────────────────────────

    async def _execute_single_command(
        self,
        session: BridgeSession,
        command: PlaywrightCommand,
    ) -> Any:
        """
        Execute a single Playwright command using real Playwright API.

        Commands are mapped to Playwright calls:
        - CLICK -> page.click(selector, **options)
        - FILL -> page.fill(selector, value)
        - SELECT -> page.select_option(selector, value)
        - PRESS -> page.keyboard.press(key)
        - HOVER -> page.hover(selector)
        - SCROLL -> page.evaluate(fixed scroll script, [0, int value])
        - GOTO -> page.goto(url)
        - EVALUATE -> only the server-generated ``window.scrollBy(dx, dy)``
          expression, re-parsed and run as the fixed scroll script
        - WAIT_FOR_SELECTOR -> page.wait_for_selector(selector)
        - WAIT_FOR_TIMEOUT -> page.wait_for_timeout(ms)
        """
        if not session._page:
            raise ValueError(f"No page available for session {session.session_id}")

        page = session._page

        try:
            # Validate script-bearing commands before any waiting, so a
            # refused command has no side effects at all.
            scroll_offsets = self._scroll_offsets(command)

            # Wait for timeout if specified
            if command.wait_for_timeout_ms > 0:
                await asyncio.sleep(command.wait_for_timeout_ms / 1000)

            # Wait for selectors if specified
            for selector in command.wait_for_selectors:
                try:
                    await page.wait_for_selector(
                        selector, timeout=self._default_timeout_ms
                    )
                except Exception:
                    logger.debug(f"Selector {selector} not found, continuing")

            # Execute the command
            cmd_type = command.command_type

            if cmd_type == CommandType.CLICK:
                await page.click(command.target, **command.options)

            elif cmd_type == CommandType.FILL:
                await page.fill(command.target, str(command.value))

            elif cmd_type == CommandType.SELECT:
                await page.select_option(command.target, command.value)

            elif cmd_type == CommandType.PRESS:
                await page.keyboard.press(command.value)

            elif cmd_type == CommandType.HOVER:
                await page.hover(command.target)

            elif cmd_type == CommandType.SCROLL:
                await page.evaluate(_SCROLL_SCRIPT, list(scroll_offsets or (0, 0)))
                await asyncio.sleep(0.3)  # Allow render

            elif cmd_type == CommandType.GOTO:
                await page.goto(
                    command.target,
                    wait_until="networkidle",
                    timeout=self._default_timeout_ms,
                )

            elif cmd_type == CommandType.EVALUATE:
                await page.evaluate(_SCROLL_SCRIPT, list(scroll_offsets or (0, 0)))

            elif cmd_type == CommandType.SET_INPUT_FILES:
                await page.set_input_files(command.target, command.value)

            elif cmd_type == CommandType.WAIT_FOR_SELECTOR:
                await page.wait_for_selector(
                    command.target, timeout=self._default_timeout_ms
                )

            # Update session state after execution
            session.current_url = page.url
            session.page_title = await page.title()
            session.last_activity = utc_now()

            logger.debug(
                f"Executed {cmd_type.value} on {command.target} for session {session.session_id}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Command execution failed for {command.command_type.value}: {e}"
            )
            raise

    @staticmethod
    def _scroll_offsets(command: PlaywrightCommand) -> Optional[tuple[int, int]]:
        """Validate the script-bearing commands and return (dx, dy).

        page.evaluate runs arbitrary JavaScript in the browser session, so
        it never receives command text. SCROLL needs a plain int value;
        EVALUATE is restricted to the exact ``window.scrollBy(dx, dy)``
        expression _handle_scroll emits. Either way the offsets are
        re-derived as bounded integers and passed to a fixed script as data.
        Returns None for every other command type.
        """
        if command.command_type == CommandType.SCROLL:
            value = command.value
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("scroll value must be an integer")
            return 0, _clamp_scroll(value)

        if command.command_type == CommandType.EVALUATE:
            match = _SCROLL_EXPRESSION.fullmatch(str(command.target))
            if match is None:
                raise ValueError(
                    "EVALUATE command not permitted: only server-generated "
                    "scroll expressions may run"
                )
            dx, dy = (int(group) for group in match.groups())
            if max(abs(dx), abs(dy)) > _SCROLL_LIMIT:
                raise ValueError(
                    "EVALUATE command not permitted: scroll offset out of range"
                )
            return dx, dy

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Browser Initialization
    # ─────────────────────────────────────────────────────────────────────────

    async def _init_playwright(self) -> None:
        """Initialize Playwright browser."""
        if self._mode == TranslationMode.PLAYWRIGHT:
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self._headless
                )
                self._playwright_initialized = True
                logger.info("Playwright browser initialized")
            except ImportError:
                logger.warning(
                    "Playwright not installed. DOM bridge will run in mock mode. "
                    "Install with: pip install playwright && playwright install chromium"
                )
            except Exception as e:
                logger.error(f"Failed to initialize Playwright: {e}")

    async def _create_browser_context(
        self,
        session: BridgeSession,
        headless: Optional[bool],
        viewport: Optional[tuple[int, int]],
    ) -> None:
        """Create a new browser context for a session."""
        if not self._browser:
            logger.warning("Browser not initialized, cannot create context")
            return

        context = await self._browser.new_context(
            viewport={
                "width": viewport[0] if viewport else 1280,
                "height": viewport[1] if viewport else 720,
            },
            user_agent="Mozilla/5.0 (compatible; AWI-Client/1.0; +https://agent-middleware.dev)",
        )

        page = await context.new_page()

        # Store real Playwright objects
        session._context = context
        session._page = page
        session.browser_context_id = str(uuid4())
        session.page_id = str(uuid4())

        logger.info(
            f"Created browser context {session.browser_context_id} for session {session.session_id}"
        )

    async def _navigate_to(self, session: BridgeSession, url: str) -> None:
        """Navigate to a URL."""
        if not session._page:
            logger.warning(f"No page available for session {session.session_id}")
            session.current_url = url
            return

        try:
            await session._page.goto(
                url, wait_until="networkidle", timeout=self._default_timeout_ms
            )
            navigated_url = session._page.url
            session.current_url = url if navigated_url == f"{url}/" else navigated_url
            session.page_title = await session._page.title()
            logger.info(f"Navigated to {url} for session {session.session_id}")
        except Exception as e:
            logger.error(f"Navigation failed for {session.session_id}: {e}")
            session.current_url = url

    async def _close_browser_context(self, session: BridgeSession) -> None:
        """Close a browser context and cleanup page."""
        if session._page:
            try:
                await session._page.close()
                session._page = None
            except Exception as e:
                logger.debug(f"Error closing page: {e}")

        if session._context:
            try:
                await session._context.close()
                session._context = None
            except Exception as e:
                logger.debug(f"Error closing context: {e}")

        logger.info(f"Closed browser context for session {session.session_id}")

    # ─────────────────────────────────────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Cleanup all resources."""
        for session_id in list(self._sessions.keys()):
            await self.destroy_session(session_id)

        if self._browser:
            await self._browser.close()
            self._browser = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        self._playwright_initialized = False

        logger.info("AWI Playwright Bridge closed")


_bridge: Optional[AWIPlaywrightBridge] = None


def get_playwright_bridge() -> AWIPlaywrightBridge:
    """Get or create the AWIPlaywrightBridge singleton."""
    global _bridge
    if _bridge is None:
        from ..core.config import get_settings

        settings = get_settings()
        _bridge = AWIPlaywrightBridge(
            headless=settings.PLAYWRIGHT_HEADLESS,
            browser_type=settings.PLAYWRIGHT_BROWSER_TYPE,
            default_timeout_ms=settings.PLAYWRIGHT_TIMEOUT_MS,
            max_sessions=settings.PLAYWRIGHT_MAX_SESSIONS,
            session_ttl_seconds=settings.PLAYWRIGHT_SESSION_TTL_SECONDS,
        )
    return _bridge
