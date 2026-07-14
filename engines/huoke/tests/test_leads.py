"""
Tests for the Leads module: store, scoring, dedup, platform profiles.
"""

import os
import tempfile
from pathlib import Path

import pytest

from src.leads.store import (
    LeadsStore,
    normalize_name,
    normalize_phone,
    normalize_email,
)


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = LeadsStore(db_path=path)
    yield s
    Path(path).unlink(missing_ok=True)


# ===========================================================================
# Normalization
# ===========================================================================

class TestNormalization:

    def test_normalize_name_basic(self):
        assert normalize_name("John Smith") == "john smith"

    def test_normalize_name_extra_spaces(self):
        assert normalize_name("  John   Smith  ") == "john smith"

    def test_normalize_name_accents(self):
        assert normalize_name("José García") == "jose garcia"

    def test_normalize_name_suffix(self):
        assert normalize_name("John Smith Jr") == "john smith"
        assert normalize_name("Robert III") == "robert"

    def test_normalize_name_empty(self):
        assert normalize_name("") == ""

    def test_normalize_phone(self):
        assert normalize_phone("+1 (555) 123-4567") == "15551234567"
        assert normalize_phone("") == ""

    def test_normalize_email(self):
        assert normalize_email("  John@Example.COM  ") == "john@example.com"
        assert normalize_email("") == ""


# ===========================================================================
# Lead CRUD
# ===========================================================================

class TestLeadCRUD:

    def test_add_and_get(self, store):
        lid = store.add_lead(name="Alice Smith", email="alice@example.com",
                             company="Acme", source_platform="linkedin")
        lead = store.get_lead(lid)
        assert lead is not None
        assert lead["name"] == "Alice Smith"
        assert lead["email"] == "alice@example.com"
        assert lead["company"] == "Acme"
        assert lead["status"] == "new"

    def test_update(self, store):
        lid = store.add_lead(name="Bob")
        store.update_lead(lid, company="BigCorp", status="contacted")
        lead = store.get_lead(lid)
        assert lead["company"] == "BigCorp"
        assert lead["status"] == "contacted"

    def test_delete(self, store):
        lid = store.add_lead(name="ToDelete")
        assert store.delete_lead(lid) is True
        assert store.get_lead(lid) is None

    def test_list_filter_status(self, store):
        store.add_lead(name="A", dedup=False)
        lid2 = store.add_lead(name="B", dedup=False)
        store.update_lead(lid2, status="qualified")
        results = store.list_leads(status="qualified")
        assert len(results) == 1
        assert results[0]["name"] == "B"

    def test_list_filter_search(self, store):
        store.add_lead(name="John Smith", company="Acme", dedup=False)
        store.add_lead(name="Jane Doe", company="BigCo", dedup=False)
        results = store.list_leads(search="john")
        assert len(results) == 1

    def test_list_order(self, store):
        l1 = store.add_lead(name="Low", dedup=False)
        l2 = store.add_lead(name="High", dedup=False)
        store.update_lead(l1, score=10)
        store.update_lead(l2, score=50)
        results = store.list_leads(order_by="score DESC")
        assert results[0]["name"] == "High"

    def test_count(self, store):
        store.add_lead(name="A", dedup=False)
        store.add_lead(name="B", dedup=False)
        assert store.count_leads() == 2
        store.update_lead(1, status="contacted")
        assert store.count_leads(status="contacted") == 1

    def test_tags(self, store):
        lid = store.add_lead(name="Tagged", tags=["marketing", "tech"])
        lead = store.get_lead(lid)
        assert isinstance(lead["tags"], list)
        assert "marketing" in lead["tags"]


# ===========================================================================
# Platform Profiles
# ===========================================================================

class TestPlatformProfiles:

    def test_add_and_get(self, store):
        lid = store.add_lead(name="ProfileUser")
        store.add_platform_profile(lid, "linkedin",
                                   profile_url="https://linkedin.com/in/user",
                                   username="profileuser")
        profiles = store.get_platform_profiles(lid)
        assert len(profiles) == 1
        assert profiles[0]["platform"] == "linkedin"
        assert profiles[0]["username"] == "profileuser"

    def test_multiple_platforms(self, store):
        lid = store.add_lead(name="MultiUser")
        store.add_platform_profile(lid, "linkedin", username="user_li")
        store.add_platform_profile(lid, "instagram", username="user_ig")
        store.add_platform_profile(lid, "facebook", username="user_fb")
        profiles = store.get_platform_profiles(lid)
        platforms = [p["platform"] for p in profiles]
        assert len(platforms) == 3
        assert set(platforms) == {"linkedin", "instagram", "facebook"}

    def test_find_by_profile(self, store):
        lid = store.add_lead(name="FindMe")
        store.add_platform_profile(lid, "instagram", profile_id="ig_12345")
        found = store.find_by_profile("instagram", "ig_12345")
        assert found == lid

    def test_find_by_profile_miss(self, store):
        assert store.find_by_profile("twitter", "nonexist") is None

    def test_upsert_profile(self, store):
        lid = store.add_lead(name="Upsert")
        store.add_platform_profile(lid, "instagram", username="old_name")
        store.add_platform_profile(lid, "instagram", username="new_name")
        profiles = store.get_platform_profiles(lid)
        assert len(profiles) == 1
        assert profiles[0]["username"] == "new_name"


# ===========================================================================
# Interactions
# ===========================================================================

class TestInteractions:

    def test_add_and_get(self, store):
        lid = store.add_lead(name="ChatUser")
        store.add_interaction(lid, "linkedin", "send_message",
                              direction="outbound", content="Hello!")
        ixs = store.get_interactions(lid)
        assert len(ixs) == 1
        assert ixs[0]["action"] == "send_message"
        assert ixs[0]["content"] == "Hello!"

    def test_filter_by_platform(self, store):
        lid = store.add_lead(name="Multi")
        store.add_interaction(lid, "linkedin", "send_message")
        store.add_interaction(lid, "instagram", "follow")
        store.add_interaction(lid, "linkedin", "view_profile")

        li_ixs = store.get_interactions(lid, platform="linkedin")
        assert len(li_ixs) == 2
        ig_ixs = store.get_interactions(lid, platform="instagram")
        assert len(ig_ixs) == 1

    def test_interaction_count(self, store):
        lid = store.add_lead(name="Counter")
        store.add_interaction(lid, "fb", "send_message", direction="outbound")
        store.add_interaction(lid, "fb", "reply", direction="inbound")
        store.add_interaction(lid, "fb", "like", direction="outbound")
        assert store.interaction_count(lid) >= 3


# ===========================================================================
# Dedup / Matching
# ===========================================================================

class TestDedup:

    def test_email_dedup(self, store):
        l1 = store.add_lead(name="John", email="john@test.com")
        l2 = store.add_lead(name="John Smith", email="john@test.com")
        assert l1 == l2  # same lead

    def test_phone_dedup(self, store):
        l1 = store.add_lead(name="Jane", phone="+1-555-1234567")
        l2 = store.add_lead(name="Jane D", phone="15551234567")
        assert l1 == l2

    def test_name_dedup(self, store):
        l1 = store.add_lead(name="Robert Johnson")
        l2 = store.add_lead(name="Robert  Johnson ")
        assert l1 == l2

    def test_no_false_dedup(self, store):
        l1 = store.add_lead(name="Alice Smith", email="alice@a.com", dedup=True)
        l2 = store.add_lead(name="Bob Jones", email="bob@b.com", dedup=True)
        assert l1 != l2

    def test_merge_fills_blanks(self, store):
        l1 = store.add_lead(name="John", email="john@test.com")
        store.add_lead(name="John", email="john@test.com",
                       company="Acme", phone="+15551234567")
        lead = store.get_lead(l1)
        assert lead["company"] == "Acme"
        assert lead["phone"] == "+15551234567"

    def test_find_match(self, store):
        store.add_lead(name="Alice", email="alice@test.com", dedup=False)
        match = store.find_match(email="alice@test.com")
        assert match is not None

    def test_find_match_none(self, store):
        assert store.find_match(email="nobody@test.com") is None


# ===========================================================================
# Scoring
# ===========================================================================

class TestScoring:

    def test_basic_score(self, store):
        lid = store.add_lead(name="Scored", email="s@test.com", company="Co")
        score = store.update_score(lid)
        assert score > 0  # email=5 + company=3

    def test_interaction_boost(self, store):
        lid = store.add_lead(name="Active")
        score1 = store.update_score(lid)
        store.add_interaction(lid, "linkedin", "reply", direction="inbound")
        store.add_interaction(lid, "linkedin", "reply", direction="inbound")
        score2 = store.update_score(lid)
        assert score2 > score1

    def test_multi_platform_boost(self, store):
        lid = store.add_lead(name="MultiPlatform")
        store.add_platform_profile(lid, "linkedin")
        score1 = store.update_score(lid)
        store.add_platform_profile(lid, "instagram")
        store.add_platform_profile(lid, "facebook")
        score2 = store.update_score(lid)
        assert score2 > score1

    def test_bulk_update(self, store):
        store.add_lead(name="A", email="a@t.com", dedup=False)
        store.add_lead(name="B", email="b@t.com", dedup=False)
        count = store.bulk_update_scores()
        assert count == 2


# ===========================================================================
# Pipeline Stats
# ===========================================================================

class TestPipelineStats:

    def test_stats(self, store):
        store.add_lead(name="A", dedup=False)
        l2 = store.add_lead(name="B", dedup=False)
        store.update_lead(l2, status="contacted")
        store.add_platform_profile(1, "linkedin")
        store.add_interaction(1, "linkedin", "msg")

        stats = store.pipeline_stats()
        assert stats["total_leads"] == 2
        assert stats["by_status"]["new"] == 1
        assert stats["by_status"]["contacted"] == 1
        assert stats["by_platform"]["linkedin"] == 1
        assert stats["total_interactions"] == 1
