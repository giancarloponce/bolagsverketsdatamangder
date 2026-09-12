import types
import unittest


streamlit = types.ModuleType("streamlit")
streamlit.set_page_config = lambda *args, **kwargs: None
streamlit.title = lambda *args, **kwargs: None
streamlit.sidebar = types.SimpleNamespace(
    header=lambda *args, **kwargs: None,
    multiselect=lambda *args, **kwargs: [],
    slider=lambda *args, **kwargs: 0,
    checkbox=lambda *args, **kwargs: False,
)
streamlit.cache_data = lambda *args, **kwargs: (lambda f: f)
streamlit.progress = lambda *args, **kwargs: None
streamlit.empty = lambda *args, **kwargs: None
streamlit.success = lambda *args, **kwargs: None
streamlit.error = lambda *args, **kwargs: None
streamlit.balloons = lambda *args, **kwargs: None
streamlit.subheader = lambda *args, **kwargs: None
streamlit.columns = lambda *args, **kwargs: (None, None)
streamlit.metric = lambda *args, **kwargs: None
streamlit.plotly_chart = lambda *args, **kwargs: None
streamlit.dataframe = lambda *args, **kwargs: None
streamlit.download_button = lambda *args, **kwargs: None
streamlit.caption = lambda *args, **kwargs: None
streamlit.button = lambda *args, **kwargs: False
streamlit.checkbox = lambda *args, **kwargs: False

import sys
sys.modules["streamlit"] = streamlit

import factoring_lead_app as app


class ScoringTests(unittest.TestCase):
    def test_empty_company_has_low_base_score(self):
        score = app.calculate_score({"sni": "", "bolag_age": 0, "omsattning": 0, "resultat": 0, "kundfordringar": 0, "kassalikviditet": None, "kortfristiga_skulder": 0, "eget_kapital": 0, "balansomslutning": 0})
        self.assertLess(score, 25)

    def test_high_quality_financials_score_high(self):
        score = app.calculate_score({
            "sni": "46",
            "bolag_age": 8,
            "omsattning": 50_000_000,
            "resultat": 7_000_000,
            "kundfordringar": 12_000_000,
            "kassalikviditet": 0.75,
            "kortfristiga_skulder": 25_000_000,
            "eget_kapital": 18_000_000,
            "balansomslutning": 60_000_000,
        })
        self.assertGreater(score, 60)


if __name__ == "__main__":
    unittest.main()
