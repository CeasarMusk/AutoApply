"""Answer memory, planning, and the form scanner/filler against local copies of each ATS's markup."""

import pytest

from autoapply.apply import fill, scan
from autoapply.apply.answers import AnswerStore, ResumeFacts, match_option, standard_key
from autoapply.apply.runner import Applier, Planned
from autoapply.models import Job

from .conftest import sample_resume


# ---------------------------------------------------------------------------
# answers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,key", [
    ("Are you legally authorized to work in the United States?", "authorized"),
    ("Are you currently eligible to work legally in the United States of America?", "authorized"),
    ("Do you now or in the future require immigration support or visa sponsorship?", "sponsorship"),
    ("Are you authorized to work in the United States without any future visa sponsorship?", "authorized_no_sponsorship"),
    ("How did you hear about this job?", "how_heard"),
    ("Have you ever been previously employed by Spotify?", "previously_employed"),
    ("Have you previously worked for Stripe?", "previously_employed"),
    ("I identify my race/ethnicity as (please mark all that apply):", "race"),
    ("Are you Hispanic/Latino?", "hispanic"),
    ("Veteran Status:", "veteran"),
    ("First Name*", "first_name"),
    ("Preferred First Name", "preferred_name"),
    ("Current location", "location"),
    ("LinkedIn Profile", "linkedin"),
    ("Website or GitHub", "github"),
    ("Full Legal Name", "full_name"),
    ("SMS", "sms_consent"),
    ("Do you consent to receive text messages about your application?", "sms_consent"),
    ("WhatsApp", "whatsapp_consent"),
    ("Education History: Start Date", "edu_start"),
    ("Education History: End Date", "edu_end"),
    ("Earliest start date", "start_date"),
    # long questions that merely mention a fact word are not that fact
    ("Do you acknowledge that this is a hybrid role and you will come to our location four days a week?", None),
    ("Tell us about a product initiative you led that improved user engagement", None),
])
def test_standard_keys(question, key):
    assert standard_key(question) == key


def test_match_option():
    yes_no = ["No", "Yes"]
    assert match_option("Yes", yes_no) == 1 and match_option("no", yes_no) == 0
    decline = ["Man", "Woman", "I don't wish to answer"]
    assert match_option("Decline to self-identify", decline) == 2
    assert match_option("United States", ["Canada", "United States of America"]) == 1
    assert match_option("United States", ["United States +1", "Afghanistan +93"]) == 0
    assert match_option("US", ["Canada", "United States"]) == 1
    assert match_option("No", ["No", "Yes - Intern", "Yes - Full Time Employment"]) == 0
    assert match_option("Purple", yes_no) is None


def test_store_roundtrip_and_similar(isolated_home):
    st = AnswerStore()
    assert st.remember("Are you eligible to work in the US?", "Yes") == "authorized"
    st.remember("Do you have experience with Kafka in production?", "Yes")
    st.save()
    st = AnswerStore.load()
    assert st.lookup("Are you legally authorized to work in the United States?")[:2] == ("Yes", "saved")
    assert st.lookup("Do you have experience with Kafka in production?")[1] == "saved"
    assert st.lookup("Do you have production experience with Kafka?")[1] == "similar"
    assert st.lookup("What is your favorite color?") is None


def test_resume_facts():
    f = ResumeFacts.from_resume(sample_resume()).values
    assert (f["first_name"], f["last_name"], f["city"], f["state"]) == ("Alex", "Rivera", "Austin", "TX")
    assert f["current_company"] == "Acme" and f["current_title"] == "Backend Engineer"


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def _job():
    return Job(id=1, source="greenhouse", company_slug="acme", external_id="1", company="Acme",
               title="Engineer", url="x", dedupe_key="k")


def test_plan_sources(tmp_path):
    st = AnswerStore({"standard": {"authorized": "Yes", "gender": "Decline to self-identify"}})
    app = Applier(_job(), sample_resume(), tmp_path / "r.pdf", interactive=False, store=st, ask_optional=False)
    F = scan.Field
    plans = app.plan([
        F("a1", "text", "First Name", True),
        F("a2", "file", "Resume/CV", True, id="resume"),
        F("a3", "combobox", "Are you legally authorized to work in the US?", True, options=["No", "Yes"]),
        F("a4", "combobox", "Gender", True, options=["Male", "Female", "Decline To Self Identify"]),
        F("a5", "radio", "Will you require sponsorship?", True, options=["Yes", "No"]),  # sensitive, unsaved
        F("a6", "textarea", "Why do you want to work here?", True),
        F("a7", "text", "Nickname", False),
        F("a8", "file", "Cover letter", False, id="cover_letter"),
    ])
    got = {p.field.aa: (p.source, p.value) for p in plans}
    assert got["a1"] == ("resume", "Alex")
    assert got["a2"] == ("file", str(tmp_path / "r.pdf"))
    assert got["a3"] == ("saved", "Yes")
    assert got["a4"] == ("saved", "Decline To Self Identify")
    assert got["a5"][0] == "ask"          # never guessed
    assert got["a6"][0] == "essay"
    assert got["a7"][0] == "skip" and got["a8"][0] == "skip"


def test_optional_questions_are_asked_by_default(tmp_path):
    F = scan.Field
    app = Applier(_job(), sample_resume(), tmp_path / "r.pdf", interactive=False, store=AnswerStore({}))
    assert app.plan_field(F("a", "text", "Nickname", False)).source == "ask"
    assert app.plan_field(F("b", "textarea", "Anything else?", False)).source == "ask"  # not AI-drafted
    assert app.plan_field(F("c", "file", "Cover letter", False, id="cover_letter")).source == "ask"
    assert app.plan_field(F("d", "text", "Pronouns", False)).source == "ask"


def test_optional_consent_is_asked_then_reused(tmp_path):
    F = scan.Field
    opts = ["Yes - I consent to receiving SMS text messages", "No - I do not consent to receiving SMS text messages"]
    app = Applier(_job(), sample_resume(), tmp_path / "r.pdf", interactive=False, store=AnswerStore({}), ask_optional=False)
    assert app.plan_field(F("s", "radio", "SMS", False, options=opts)).source == "ask"  # optional, but asked once
    st = AnswerStore({"standard": {"sms_consent": opts[1]}})
    app = Applier(_job(), sample_resume(), tmp_path / "r.pdf", interactive=False, store=st)
    assert app.plan_field(F("s", "yesno", "Can we text you (SMS)?", False, options=["Yes", "No"])).value == "No"


def test_education_dates_from_resume(tmp_path):
    from autoapply.models import Education

    r = sample_resume()
    r.education = [Education(school="UT Austin", start="Aug 2023", end="May 2027")]
    app = Applier(_job(), r, tmp_path / "r.pdf", interactive=False, store=AnswerStore({}))
    F = scan.Field
    assert app.plan_field(F("a", "monthyear", "Education History: Start Date", False)).value == "2023-08-01"
    assert app.plan_field(F("b", "monthyear", "Education History: End Date", False)).value == "2027-05-01"


def test_saved_answer_not_in_options_is_asked(tmp_path):
    st = AnswerStore({"standard": {"how_heard": "A friend"}})
    app = Applier(_job(), None, tmp_path / "r.pdf", interactive=False, store=st)
    p = app.plan_field(scan.Field("a", "select", "How did you hear about us?", True, options=["LinkedIn", "Indeed"]))
    assert p.source == "ask" and "isn't an option" in p.note


def test_transcript_upload(tmp_path):
    F = scan.Field
    fields = [F("t1", "file", "Unofficial Transcript", True, id="transcript"),
              F("t2", "file", "Academic records (optional)", False)]
    with_t = Applier(_job(), None, tmp_path / "r.pdf", interactive=False, store=AnswerStore({}),
                     transcript_file=tmp_path / "t.pdf")
    assert [(p.source, p.value) for p in with_t.plan(fields)] == [("file", str(tmp_path / "t.pdf"))] * 2
    without = Applier(_job(), None, tmp_path / "r.pdf", interactive=False, store=AnswerStore({}))
    assert [p.source for p in without.plan(fields)] == ["ask", "ask"]  # optional asked by default
    quiet = Applier(_job(), None, tmp_path / "r.pdf", interactive=False, store=AnswerStore({}), ask_optional=False)
    assert [p.source for p in quiet.plan(fields)] == ["ask", "skip"]


def test_keep_transcript_replaces_old_copy(tmp_path, monkeypatch):
    from autoapply import config
    from autoapply.apply.runner import keep_transcript, saved_transcript

    monkeypatch.setenv("AUTOAPPLY_HOME", str(tmp_path / "home"))
    (tmp_path / "a.docx").write_bytes(b"old")
    (tmp_path / "b.pdf").write_bytes(b"new")
    keep_transcript(tmp_path / "a.docx")
    dest = keep_transcript(tmp_path / "b.pdf")
    assert dest == tmp_path / "home" / "transcript.pdf" and dest.read_bytes() == b"new"
    assert not (tmp_path / "home" / "transcript.docx").exists()
    assert saved_transcript() == dest and config.load_config().transcript_file == str(dest)


# ---------------------------------------------------------------------------
# scanner + filler on local pages shaped like each ATS
# ---------------------------------------------------------------------------

GREENHOUSE_LIKE = """
<form>
 <div class="field"><label for="first_name">First Name<span>*</span></label><input id="first_name" type="text" aria-required="true"></div>
 <div class="field"><label for="email">Email*</label><input id="email" type="text"></div>
 <div><label id="lbl-q1">Are you eligible to work in the US?*</label>
   <input id="question_1" role="combobox" aria-labelledby="lbl-q1" aria-controls="menu-q1"
     onfocus="document.getElementById('menu-q1').style.display='block'">
   <div id="menu-q1" role="listbox" style="display:none">
     <div role="option" onclick="document.getElementById('question_1').value=this.innerText">No</div>
     <div role="option" onclick="document.getElementById('question_1').value=this.innerText">Yes</div>
   </div></div>
 <div><label>Resume/CV</label><label for="resume">Attach</label><input id="resume" type="file" class="visually-hidden"></div>
</form>"""

LEVER_LIKE = """
<form>
 <ul>
  <li class="application-question"><div class="application-label">Full name<span>✱</span></div>
    <div class="application-field"><input name="name" type="text" required></div></li>
  <li class="application-question"><div class="application-label">Have you ever been previously employed by Acme?<span>✱</span></div>
    <div class="application-field"><ul>
      <li><label><input type="checkbox" name="cards[x][field0]" value="No">No</label></li>
      <li><label><input type="checkbox" name="cards[x][field0]" value="Yes">Yes</label></li></ul></div></li>
  <li class="application-question"><div class="application-label">Why Acme?</div>
    <div class="application-field"><textarea name="cards[y][field0]"></textarea></div></li>
  <li><div class="application-label">Office</div><select name="office"><option value="">Select...</option><option>New York, NY</option><option>Toronto</option></select></li>
 </ul>
</form>"""

ASHBY_LIKE = """
<style>.req::after{content:"*"}</style>
<form>
 <div><label for="_systemfield_name" class="title req">Name</label><input id="_systemfield_name" name="_systemfield_name" type="text"></div>
 <div class="q"><label class="title req">Are you authorized to work in the United States without any future visa sponsorship?</label>
   <div><input type="checkbox" name="question_9" style="display:none">
     <button type="button" onclick="this.setAttribute('aria-pressed','true')">Yes</button>
     <button type="button" onclick="this.setAttribute('aria-pressed','true')">No</button></div></div>
 <div><label for="ac" class="title">Autofill from resume</label><input id="ac" type="file"></div>
</form>"""


@pytest.fixture(scope="module")
def browser():
    pw_mod = pytest.importorskip("playwright.sync_api")
    with pw_mod.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:  # browser not installed
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def _scan(browser, html):
    page = browser.new_page()
    page.set_content(html)
    return page, {f.label: f for f in scan.scan(page)}


def test_scan_and_fill_greenhouse_like(browser, tmp_path):
    page, fields = _scan(browser, GREENHOUSE_LIKE)
    assert fields["First Name"].required and fields["First Name"].kind == "text"
    q = fields["Are you eligible to work in the US?"]
    assert q.kind == "combobox" and q.required
    scan.load_combobox_options(page, q)
    assert q.options == ["No", "Yes"]
    resume = next(f for f in fields.values() if f.kind == "file")
    assert resume.id == "resume"
    fill.fill(page, fields["First Name"], "Alex")
    fill.fill(page, q, "Yes")
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    fill.fill(page, resume, str(pdf))
    assert page.input_value("#first_name") == "Alex"
    assert page.input_value("#question_1") == "Yes"
    assert page.evaluate("document.getElementById('resume').files[0].name") == "r.pdf"
    page.close()


def test_scan_and_fill_lever_like(browser):
    page, fields = _scan(browser, LEVER_LIKE)
    assert fields["Full name"].required
    prev = fields["Have you ever been previously employed by Acme?"]
    assert prev.kind == "checkboxes" and prev.options == ["No", "Yes"] and prev.required
    assert fields["Why Acme?"].kind == "textarea" and not fields["Why Acme?"].required
    assert fields["Office"].options == ["New York, NY", "Toronto"]
    fill.fill(page, prev, ["No"])
    fill.fill(page, fields["Office"], "Toronto")
    vals = scan.read_values(page)
    assert vals[prev.aa] == ["No"]
    assert vals[fields["Office"].aa] == "Toronto"
    page.close()


def test_scan_and_fill_ashby_like(browser):
    page, fields = _scan(browser, ASHBY_LIKE)
    assert fields["Name"].required  # CSS ::after asterisk
    yn = fields["Are you authorized to work in the United States without any future visa sponsorship?"]
    assert yn.kind == "yesno" and yn.options == ["Yes", "No"] and yn.required
    assert not any("autofill" in l.lower() for l in fields)  # the autofill upload box is ignored
    fill.fill(page, yn, "Yes")
    assert page.locator("button", has_text="Yes").get_attribute("aria-pressed") == "true"
    page.close()


# Shapes copied from a real Ashby form (Notion): inputs without id/name, a date picker, and
# multi-select checkboxes that each get their own `name`.
ASHBY_NOTION_LIKE = """
<form>
 <div class="ashby-application-form-field-entry"><label for="_systemfield_location">Current Location</label>
   <div><input placeholder="Start typing..." role="combobox" aria-autocomplete="list" value=""><button type="button">v</button></div></div>
 <div class="ashby-application-form-field-entry"><label for="gd">Graduation Date</label>
   <div class="react-datepicker-wrapper"><input type="text" placeholder="Pick date..." class="ashby-application-form-input-date" required></div></div>
 <div class="ashby-application-form-field-entry"><label class="title">Degree Type</label>
   <div><input type="checkbox" name="Bachelors" id="q1_dt-labeled-checkbox-0"><label for="q1_dt-labeled-checkbox-0">Undergraduate/Bachelors</label></div>
   <div><input type="checkbox" name="Masters" id="q1_dt-labeled-checkbox-1"><label for="q1_dt-labeled-checkbox-1">Master's</label></div></div>
 <div class="ashby-application-form-field-entry"><label for="li">LinkedIn Profile</label><input id="li" name="li" type="text"></div>
</form>"""


def test_scan_ashby_unnamed_inputs_dates_and_checkbox_groups(browser):
    page, fields = _scan(browser, ASHBY_NOTION_LIKE)
    assert fields["Current Location"].kind == "combobox"
    date = fields["Graduation Date"]
    assert date.kind == "date" and date.required
    deg = fields["Degree Type"]
    assert deg.kind == "checkboxes" and deg.options == ["Undergraduate/Bachelors", "Master's"]
    assert "Undergraduate/Bachelors" not in fields  # options aren't separate questions any more
    fill.fill(page, date, "2027-05-01")
    assert page.locator("input[placeholder='Pick date...']").input_value() == "05/01/2027"
    fill.fill(page, deg, ["Undergraduate/Bachelors"])
    assert page.locator("#q1_dt-labeled-checkbox-0").is_checked()
    page.close()


# Ashby (Ramp): education Start/End Date as Month + Year selects with a label on the wrapper;
# SMS / WhatsApp consent radios.
_MONTHS = "".join(f'<option value="{i}">{m}</option>' for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1))
_YEARS = "".join(f'<option value="{y}">{y}</option>' for y in range(2027, 2000, -1))
_MY = (f'<div><select><option disabled hidden value="">Month...</option>{_MONTHS}</select></div>'
       f'<div><select><option disabled hidden value="">Year...</option>{_YEARS}</select></div>')
ASHBY_RAMP_LIKE = f"""
<form>
 <div data-field-path="_systemfield_education_history"><label for="_systemfield_education_history">Education History</label>
   <div><label for="eh-degree">Degree</label><input id="eh-degree" type="text"></div>
   <div><label for="eh-startDate">Start Date</label><div id="eh-startDate">{_MY}</div></div>
   <div><label for="eh-endDate">End Date</label><div id="eh-endDate">{_MY}</div></div></div>
 <ul><li><p class="_consentRadioGroupItemLabel_q1pga_28">SMS</p><div>
   <label><input type="radio" name="smsConsent" value="given"><div><p><strong>Yes</strong> - I consent to receiving SMS text messages</p></div></label>
   <label><input type="radio" name="smsConsent" value="notGiven"><div><p><strong>No</strong> - I do not consent to receiving SMS text messages</p></div></label>
 </div></li></ul>
</form>"""


def test_scan_and_fill_month_year_selects_and_consent(browser):
    page, fields = _scan(browser, ASHBY_RAMP_LIKE)
    start, end = fields["Education History: Start Date"], fields["Education History: End Date"]
    assert start.kind == end.kind == "monthyear"
    assert fields["Degree"].kind == "text"  # plain fields keep their own label
    sms = fields["SMS"]
    assert sms.kind == "radio" and sms.options[1].startswith("No - I do not consent")
    fill.fill(page, start, "2023-08-01")
    fill.fill(page, end, "May 2027")
    fill.fill(page, sms, "No")
    vals = scan.read_values(page)
    assert vals[start.aa] == "August 2023" and vals[end.aa] == "May 2027"
    assert vals[sms.aa] == ["No - I do not consent to receiving SMS text messages"]
    page.close()


def test_to_iso_date_and_degree_matching():
    from autoapply.apply.answers import to_iso_date

    assert to_iso_date("Spring 2027") == "2027-05-01"
    assert to_iso_date("May 2027") == "2027-05-01"
    assert to_iso_date("2027-12") == "2027-12-01"
    assert to_iso_date("12/15/2026") == "2026-12-15"
    assert to_iso_date("soon") is None
    assert match_option("Bachelor's Degree", ["Undergraduate/Bachelors", "Master's", "PhD", "MBA"]) == 0
    assert match_option("BS", ["High school", "Bachelor of Science (BS)", "MS"]) == 1


def test_rescan_keeps_page_values_and_reuses_drafts(tmp_path):
    app = Applier(_job(), sample_resume(), tmp_path / "r.pdf", interactive=False, store=AnswerStore({}))
    F = scan.Field
    essay_f = F("e", "textarea", "Why us?", True)
    name_f = F("n", "text", "First Name", True)
    plans = app.plan([essay_f, name_f], {"e": "my edited answer", "n": ""})
    assert (plans[0].source, plans[0].value) == ("page", "my edited answer")  # not re-drafted or overwritten
    assert plans[1].source == "resume"
    app.drafts["Why us?"] = "earlier draft"
    p = app.plan([essay_f], {"e": ""})
    app.draft_essays(p)
    assert (p[0].source, p[0].value) == ("ai", "earlier draft")  # no second AI call


def test_form_url_keeps_regional_greenhouse_links():
    from autoapply.apply import runner
    from autoapply.models import Job

    eu = "https://job-boards.eu.greenhouse.io/agency/jobs/4835696101"
    job = Job(source="greenhouse", company_slug="agency", external_id="4835696101", company="A", title="t",
              url=eu, apply_url=eu, dedupe_key="k")
    assert runner.form_url(job) == eu
    job.apply_url = "https://stripe.com/jobs/search?gh_jid=1"
    assert runner.form_url(job) == "https://job-boards.greenhouse.io/agency/jobs/4835696101"
