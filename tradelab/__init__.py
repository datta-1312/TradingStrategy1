"""tradelab: a skeptical, systematic trading-strategy research pipeline.

Market analysis -> opportunity scan -> strategy ideation -> implementation ->
realistic backtesting -> out-of-sample evaluation -> iteration -> final selection.
"""
from .config import Criteria, ResearchConfig, config_from_preset

__version__ = "0.1.0"
__all__ = ["Criteria", "ResearchConfig", "config_from_preset", "__version__"]
