import pytest
from unittest.mock import MagicMock, patch
import os
from data import ai_scraper

def test_ai_config_disabled():
    with patch('data.ai_scraper.load_config') as mock_load:
        mock_load.return_value = {
            "data_sources": {
                "scrapegraphai": {
                    "enabled": False
                }
            }
        }
        config = ai_scraper._get_ai_config()
        assert config is None

def test_ai_config_loading():
    with patch('data.ai_scraper.load_config') as mock_load:
        mock_load.return_value = {
            "data_sources": {
                "scrapegraphai": {
                    "enabled": True,
                    "model": "test-model",
                    "api_key": "test-key",
                    "saas_api_key": "saas-key"
                }
            }
        }
        config = ai_scraper._get_ai_config()
        assert config is not None
        assert config["local"]["llm"]["api_key"] == "test-key"
        assert config["saas_api_key"] == "saas-key"

@patch('data.ai_scraper._get_ai_config')
@patch('data.ai_scraper.ScrapeGraphSaaS')
def test_scrape_url_saas_success(mock_saas_cls, mock_config):
    mock_config.return_value = {"saas_api_key": "key", "local": None}
    mock_instance = mock_saas_cls.return_value
    mock_instance.extract.return_value = MagicMock(status="success", data={"data": "saas"})
    
    result = ai_scraper.scrape_url("http://example.com", "prompt")
    assert result == {"data": "saas"}
    mock_instance.extract.assert_called_once()

@patch('data.ai_scraper._get_ai_config')
@patch('data.ai_scraper.SmartScraperGraph')
@patch('data.ai_scraper.ScrapeGraphSaaS')
def test_scrape_url_local_fallback(mock_saas_cls, mock_local_cls, mock_config):
    # SaaS fails, should fallback to local
    mock_config.return_value = {"saas_api_key": "key", "local": {"llm": {"api_key": "key"}}}
    mock_saas_instance = mock_saas_cls.return_value
    mock_saas_instance.extract.return_value = MagicMock(status="error", error="failed")
    
    mock_local_instance = mock_local_cls.return_value
    mock_local_instance.run.return_value = {"data": "local"}
    
    result = ai_scraper.scrape_url("http://example.com", "prompt")
    assert result == {"data": "local"}
    mock_local_instance.run.assert_called_once()

@patch('data.ai_scraper._get_ai_config')
@patch('data.ai_scraper.ScrapeGraphSaaS')
def test_search_and_scrape_saas_success(mock_saas_cls, mock_config):
    mock_config.return_value = {"saas_api_key": "key", "local": None}
    mock_instance = mock_saas_cls.return_value
    mock_instance.search.return_value = MagicMock(
        status="success", 
        data={"results": [{"title": "News", "content": "market up"}]}
    )
    mock_instance.extract.return_value = MagicMock(
        status="success",
        data="BULLISH"
    )
    
    result = ai_scraper.search_and_scrape("query", "prompt")
    assert result == "BULLISH"
