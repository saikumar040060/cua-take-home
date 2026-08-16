"""Mock legacy bank back-office tool: "Meridian Credit Union — Teller Console".

Deliberately "legacy-ish" per the brief's environment description:
  * server-rendered HTML, table-based layout, <font> tags
  * non-semantic class names ("c1", "bx", "tbl-a"), no data-testid anywhere
  * inputs identified only by proximity ("Member No:" in an adjacent <td>)

Runtime conditions (the interesting part) are injectable via the MOCK_CHAOS env
var so evidence runs are deterministic and reproducible:

  MOCK_CHAOS=none          normal behaviour (default)
  MOCK_CHAOS=interstitial  first navigation to a member page per browser session
                           is intercepted by a "session expiring" warning page
                           with a Continue button (a *recoverable condition*)
  MOCK_CHAOS=slow          member detail page takes ~2.5s to respond
                           (a *recoverable condition*: transient slowness)
  MOCK_CHAOS=broken        member detail page renders a vendor error screen
                           (an unexpected state -> *hard failure* on replay)

Business outcomes that occur regardless of chaos mode:
  * member lookup for an unknown number -> "No matching member record" page
  * sub-account form with a bad product/deposit -> validation error page

Run:  MOCK_CHAOS=none python -m mock_app.app  (defaults to port 5000)
"""

from __future__ import annotations

import os
import time

from flask import Flask, redirect, render_template, request, session, url_for

from mock_app.data import MEMBERS, VALID_PRODUCTS, create_sub_account

app = Flask(__name__)
app.secret_key = "mock-only-not-a-real-secret"

DEFAULT_CHAOS = os.environ.get("MOCK_CHAOS", "none")


def chaos() -> str:
    """Active chaos mode: per-request cookie override, else process default.

    The cookie override exists so the test suite can exercise every runtime
    condition against a single server instance; evidence runs use the env
    var so the condition is fixed for the whole recorded session.
    """
    return request.cookies.get("chaos", DEFAULT_CHAOS)


@app.get("/")
def home():
    return render_template("home.html")


@app.get("/member/search")
def member_search():
    member_no = (request.args.get("mno") or "").strip()
    member = MEMBERS.get(member_no)
    if member is None:
        # Expected business outcome: a legitimate "not found" result, not an error.
        return render_template("not_found.html", member_no=member_no)
    return redirect(url_for("member_detail", member_no=member_no))


@app.get("/member/<member_no>")
def member_detail(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)

    if chaos() == "broken":
        return render_template("vendor_error.html"), 500

    if chaos() == "slow":
        time.sleep(2.5)

    if chaos() == "interstitial" and not session.get("interstitial_shown"):
        session["interstitial_shown"] = True
        return render_template(
            "interstitial.html", next_url=url_for("member_detail", member_no=member_no)
        )

    return render_template("member.html", m=member)


@app.post("/session/extend")
def session_extend():
    # The interstitial's Continue button posts here; we just resume.
    return redirect(request.form.get("next") or url_for("home"))


@app.get("/member/<member_no>/subacct/new")
def subacct_new(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    return render_template(
        "subacct_form.html", m=member, products=VALID_PRODUCTS, errors=[], form={}
    )


@app.post("/member/<member_no>/subacct/create")
def subacct_create(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)

    product = (request.form.get("prod") or "").strip()
    nickname = (request.form.get("nick") or "").strip()
    deposit = (request.form.get("dep") or "").strip()

    errors = []
    if product not in VALID_PRODUCTS:
        errors.append("Product type is required.")
    if not nickname:
        errors.append("Sub-account nickname is required.")
    try:
        if float(deposit.replace(",", "").replace("$", "")) < 5.0:
            errors.append("Initial deposit must be at least $5.00.")
    except ValueError:
        errors.append("Initial deposit must be a dollar amount.")

    if errors:
        # Expected business outcome: validation rejection, rendered legacy-style.
        return render_template(
            "subacct_form.html",
            m=member,
            products=VALID_PRODUCTS,
            errors=errors,
            form={"prod": product, "nick": nickname, "dep": deposit},
        )

    ref = create_sub_account(member_no, product, nickname, deposit)
    return redirect(url_for("subacct_confirm", member_no=member_no, ref=ref))


@app.get("/member/<member_no>/subacct/confirm/<ref>")
def subacct_confirm(member_no: str, ref: str):
    member = MEMBERS.get(member_no)
    return render_template("subacct_confirm.html", m=member, ref=ref)


def main() -> None:
    port = int(os.environ.get("MOCK_PORT", "5000"))
    app.run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
