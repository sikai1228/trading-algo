from trumpbot.news.base import NewsMonitor
from trumpbot.news.matcher import MatchResult, NewsMatcher
from trumpbot.news.rss import RSSPoller
from trumpbot.news.truthsocial import TruthSocialScraper
from trumpbot.news.twitter import TwitterScraper

__all__ = [
    "MatchResult",
    "NewsMatcher",
    "NewsMonitor",
    "RSSPoller",
    "TruthSocialScraper",
    "TwitterScraper",
]
