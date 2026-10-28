"""Auction scrapers registry."""

from .base import BaseScraper, ScrapeResult
from .copart import CopartScraper
from .master import MasterScraper
from .sodre import SodreSantoroScraper
from .superbid import SuperbidScraper

SCRAPERS = {
    "copart": CopartScraper(),
    "superbid": SuperbidScraper(),
    "sodre_santoro": SodreSantoroScraper(),
    "leilao_master": MasterScraper(),
}

SCRAPER_LABELS = {key: scraper.label for key, scraper in SCRAPERS.items()}

__all__ = ["SCRAPERS", "SCRAPER_LABELS", "BaseScraper", "ScrapeResult"]
