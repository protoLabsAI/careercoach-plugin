"""#57: a follow-up can be closed, instead of faking it with a far-future due date."""

import test_hunt

hunt_tools = test_hunt.hunt_tools
mods = test_hunt.mods
net = test_hunt.net
watch_env = test_hunt.watch_env

SEED = {"name": "Ada Lovelace", "company": "Acme", "next_action": "send portfolio", "due": "2000-01-01"}


def test_close_clears_the_follow_up_and_says_so(hunt_tools, mods):
    log = hunt_tools["careercoach_log_contact"]
    log.invoke(SEED)
    assert len(mods.contacts.due_followups()) == 1
    out = log.invoke({"name": "Ada Lovelace", "company": "Acme", "close": True})
    assert "closed" in out.lower()
    row = mods.contacts.load_contacts()[0]
    assert (row["next_action"], row["due"]) == ("", "")
    assert mods.contacts.due_followups() == []


def test_close_can_log_the_final_touch(hunt_tools, mods):
    log = hunt_tools["careercoach_log_contact"]
    log.invoke(SEED)
    log.invoke({"name": "Ada Lovelace", "company": "Acme", "note": "they passed", "close": True})
    assert mods.contacts.load_contacts()[0]["touches"][-1]["note"] == "they passed"


def test_close_with_a_new_next_step_is_refused_and_writes_nothing(hunt_tools, mods):
    log = hunt_tools["careercoach_log_contact"]
    log.invoke(SEED)
    out = log.invoke(
        {"name": "Ada Lovelace", "company": "Acme", "next_action": "call", "due": "2030-01-01", "close": True}
    )
    assert "either" in out.lower()
    assert mods.contacts.load_contacts()[0]["next_action"] == "send portfolio"


def test_closing_an_unknown_contact_does_not_create_one(hunt_tools, mods):
    out = hunt_tools["careercoach_log_contact"].invoke({"name": "Nobody", "close": True})
    assert "no contact" in out.lower()
    assert mods.contacts.load_contacts() == []


def test_closing_with_nothing_open_says_so(hunt_tools, mods):
    log = hunt_tools["careercoach_log_contact"]
    log.invoke({"name": "Ada Lovelace", "company": "Acme", "note": "met"})
    assert "no open follow-up" in log.invoke({"name": "Ada Lovelace", "close": True}).lower()


def test_empty_fields_still_mean_unchanged(hunt_tools, mods):
    log = hunt_tools["careercoach_log_contact"]
    log.invoke(SEED)
    log.invoke({"name": "Ada Lovelace", "company": "Acme", "note": "pinged"})
    row = mods.contacts.load_contacts()[0]
    assert (row["next_action"], row["due"]) == ("send portfolio", "2000-01-01")
