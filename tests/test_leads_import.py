from app.leads_import import import_csv
from app.models import Lead, Suppression


def test_import_maps_columns_and_skips_bad_rows(session):
    session.add(Suppression(value="blocked.com", kind="domain", reason="manual"))
    session.commit()
    data = ("﻿E-mail,First Name,Company,City\n"
            "jane@acme.com,Jane,Acme,Austin\n"
            "JANE@acme.com,Jane,Acme,Austin\n"
            "info@acme.com,,Acme,\n"
            "bob@blocked.com,Bob,Blocked,\n"
            "not-an-email,,,\n").encode()

    result = import_csv(session, data, "test.csv", check_mx=False)
    session.commit()

    assert result.added == 1
    reasons = dict(result.skipped)
    assert reasons["jane@acme.com"] == "duplicate in this file"
    assert "role address" in reasons["info@acme.com"]
    assert "suppression" in reasons["bob@blocked.com"]
    assert "not-an-email" in reasons
    lead = session.query(Lead).one()
    assert (lead.first_name, lead.company, lead.fields["city"]) == ("Jane", "Acme", "Austin")


def test_reimport_updates_existing_lead(session):
    import_csv(session, b"email,company\njane@acme.com,Acme\n", "a.csv", check_mx=False)
    session.commit()
    result = import_csv(session, b"email,title\njane@acme.com,CEO\n", "b.csv", check_mx=False)
    session.commit()
    assert (result.added, result.updated) == (0, 1)
    lead = session.query(Lead).one()
    assert (lead.company, lead.title) == ("Acme", "CEO")
