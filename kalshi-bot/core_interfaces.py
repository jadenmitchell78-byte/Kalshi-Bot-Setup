"""
core_interfaces.py — Formal Interfaces and Contracts for PRO Trading Bot Suite.
Enforces modular architecture, swap-in/out capability, and clear interface boundaries.
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple, List

class ISwapAdapter(ABC):
    """
    Contract for execution of token swaps and on-chain liquidity checks.
    Allows swapping between Uniswap V2 (Base), Jupiter (Solana), or future protocols.
    """
    @abstractmethod
    def get_token_price_and_liquidity(self, pair_address: str, token_address: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Retrieves the current price of the token and the available quote liquidity.
        Returns Tuple of (price_in_quote, liquidity_in_quote).
        """
        pass

    @abstractmethod
    def execute_buy(self, token_address: str, amount: float, quote_address: Optional[str] = None) -> bool:
        """
        Executes a swap from quote token to target token.
        Returns True if transaction is successful, False otherwise.
        """
        pass

    @abstractmethod
    def execute_sell(self, token_address: str, amount: Optional[float] = None, quote_address: Optional[str] = None) -> bool:
        """
        Executes a swap from target token back to quote token.
        If amount is None, executes a full sell (100% of balance).
        Returns True if transaction is successful, False otherwise.
        """
        pass

    @abstractmethod
    def execute_partial_sell(self, token_address: str, fraction: float, quote_address: Optional[str] = None) -> bool:
        """
        Executes a partial sell of the held token (e.g. 50% for staged profit locking).
        Returns True if successful, False otherwise.
        """
        pass


class IAlertProvider(ABC):
    """
    Contract for observability, notification routing, and user alerts.
    Allows routing signals to Telegram, Discord, UI dashboard toasts, or console logs.
    """
    @abstractmethod
    def send_telegram(self, message: str) -> None:
        """Sends raw markdown text alert via Telegram."""
        pass

    @abstractmethod
    def send_discord_embed(self, title: str, description: str, fields: List[Dict[str, Any]], color: int) -> None:
        """Sends rich embed message via Discord webhook."""
        pass

    @abstractmethod
    def alert_discovery(self, symbol: str, token_address: str, price: float, liquidity: float) -> None:
        """Triggers scanner discovery notification across all active channels."""
        pass

    @abstractmethod
    def alert_trade_enter(self, symbol: str, token_address: str, amount: float, price: float, liquidity: float) -> None:
        """Triggers trade entry confirmation alert."""
        pass

    @abstractmethod
    def alert_trade_close(self, symbol: str, buy_price: float, sell_price: float, pnl_pct: float, reason: str) -> None:
        """Triggers trade exit / closure notification (detailing exit reasons and PNL)."""
        pass


class ISafetyChecker(ABC):
    """
    Contract for data validation, fraud protection, and rug pull screening.
    Supports asynchronous check implementation (e.g., GoPlus Security, Helius, or local filters).
    """
    @abstractmethod
    def check_token_safety(self, token_address: str) -> bool:
        """
        Runs synchronous validation filters.
        Returns True if token is deemed safe for trading, False otherwise.
        """
        pass

    @abstractmethod
    def check_token_safety_async(self, token_address: str, timeout_sec: float = 4.0) -> bool:
        """
        Runs async/non-blocking validation filters with absolute execution timeout.
        Returns True if token passes, False if fails or times out.
        """
        pass
