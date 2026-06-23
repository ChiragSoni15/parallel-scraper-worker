"""Zero-cost parallel placeId discovery + deep metadata scraper."""
from parallel_scraper.config import ParallelConfig, COLUMN_CATALOG, ALWAYS_INCLUDED
from parallel_scraper.scraper import ParallelScraper

__all__ = ["ParallelScraper", "ParallelConfig", "COLUMN_CATALOG", "ALWAYS_INCLUDED"]
__version__ = "0.1.0"
