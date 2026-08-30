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

from mock_app.data import (
    MEMBERS,
    VALID_HOLD_REASONS,
    VALID_LOAN_PURPOSES,
    VALID_PRODUCTS,
    apply_loan,
    close_account,
    create_sub_account,
    find_account,
    pay_bill,
    place_hold,
    toggle_card,
    transfer_funds,
    update_contact,
)

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


@app.get("/member/<member_no>/transfer")
def transfer_new(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    return render_template("transfer_form.html", m=member, errors=[], form={})


@app.post("/member/<member_no>/transfer/execute")
def transfer_execute(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    from_no = (request.form.get("from_acct") or "").strip()
    to_no = (request.form.get("to_acct") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    ok, result = transfer_funds(member, from_no, to_no, amount)
    if not ok:
        return render_template(
            "transfer_form.html", m=member, errors=[result],
            form={"from_acct": from_no, "to_acct": to_no, "amount": amount},
        )
    return redirect(url_for("transfer_confirm", member_no=member_no, ref=result))


@app.get("/member/<member_no>/transfer/confirm/<ref>")
def transfer_confirm(member_no: str, ref: str):
    member = MEMBERS.get(member_no)
    return render_template("transfer_confirm.html", m=member, ref=ref)


@app.get("/member/<member_no>/update")
def update_new(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    return render_template("update_form.html", m=member, errors=[])


@app.post("/member/<member_no>/update/save")
def update_save(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    phone = (request.form.get("phone") or "").strip()
    address = (request.form.get("address") or "").strip()
    if not phone and not address:
        return render_template("update_form.html", m=member, errors=["Enter a phone number or address to update."])
    update_contact(member, phone, address)
    return redirect(url_for("update_confirm", member_no=member_no))


@app.get("/member/<member_no>/update/confirm")
def update_confirm(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    return render_template("update_confirm.html", m=member)


@app.get("/member/<member_no>/accounts/<account_no>/close")
def close_new(member_no: str, account_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    acct = find_account(member, account_no)
    return render_template("close_form.html", m=member, acct=acct, errors=[])


@app.post("/member/<member_no>/accounts/<account_no>/close/execute")
def close_execute(member_no: str, account_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    ok, result = close_account(member, account_no)
    acct = find_account(member, account_no)
    if not ok:
        return render_template("close_form.html", m=member, acct=acct, errors=[result])
    return redirect(url_for("close_confirm", member_no=member_no, account_no=account_no))


@app.get("/member/<member_no>/accounts/<account_no>/close/confirm")
def close_confirm(member_no: str, account_no: str):
    member = MEMBERS.get(member_no)
    return render_template("close_confirm.html", m=member, account_no=account_no)


@app.get("/member/<member_no>/cards")
def cards_list(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    return render_template("cards.html", m=member)


@app.post("/member/<member_no>/cards/<last4>/toggle")
def cards_toggle(member_no: str, last4: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    toggle_card(member, last4)
    return redirect(url_for("cards_list", member_no=member_no))


@app.get("/member/<member_no>/loan/new")
def loan_new(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    return render_template("loan_form.html", m=member, purposes=VALID_LOAN_PURPOSES, errors=[], form={})


@app.post("/member/<member_no>/loan/apply")
def loan_apply(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    purpose = (request.form.get("purpose") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    ok, result = apply_loan(member, purpose, amount)
    if not ok:
        return render_template(
            "loan_form.html", m=member, purposes=VALID_LOAN_PURPOSES, errors=[result],
            form={"purpose": purpose, "amount": amount},
        )
    return redirect(url_for("loan_confirm", member_no=member_no, ref=result))


@app.get("/member/<member_no>/loan/confirm/<ref>")
def loan_confirm(member_no: str, ref: str):
    member = MEMBERS.get(member_no)
    return render_template("loan_confirm.html", m=member, ref=ref)


@app.get("/member/<member_no>/billpay")
def billpay_new(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    return render_template("billpay_form.html", m=member, errors=[], form={})


@app.post("/member/<member_no>/billpay/pay")
def billpay_pay(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    account_no = (request.form.get("account_no") or "").strip()
    payee_id = (request.form.get("payee_id") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    ok, result = pay_bill(member, account_no, payee_id, amount)
    if not ok:
        return render_template(
            "billpay_form.html", m=member, errors=[result],
            form={"account_no": account_no, "payee_id": payee_id, "amount": amount},
        )
    return redirect(url_for("billpay_confirm", member_no=member_no, ref=result))


@app.get("/member/<member_no>/billpay/confirm/<ref>")
def billpay_confirm(member_no: str, ref: str):
    member = MEMBERS.get(member_no)
    return render_template("billpay_confirm.html", m=member, ref=ref)


@app.get("/member/<member_no>/accounts/<account_no>/history")
def account_history(member_no: str, account_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    acct = find_account(member, account_no)
    txns = member.transactions.get(account_no, [])
    return render_template("history.html", m=member, acct=acct, txns=txns)


@app.get("/member/<member_no>/hold")
def hold_new(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    return render_template("hold_form.html", m=member, reasons=VALID_HOLD_REASONS, errors=[], form={})


@app.post("/member/<member_no>/hold/place")
def hold_place(member_no: str):
    member = MEMBERS.get(member_no)
    if member is None:
        return render_template("not_found.html", member_no=member_no)
    account_no = (request.form.get("account_no") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    ok, result = place_hold(member, account_no, reason)
    if not ok:
        return render_template(
            "hold_form.html", m=member, reasons=VALID_HOLD_REASONS, errors=[result],
            form={"account_no": account_no, "reason": reason},
        )
    return redirect(url_for("hold_confirm", member_no=member_no, ref=result))


@app.get("/member/<member_no>/hold/confirm/<ref>")
def hold_confirm(member_no: str, ref: str):
    member = MEMBERS.get(member_no)
    return render_template("hold_confirm.html", m=member, ref=ref)


def main() -> None:
    port = int(os.environ.get("MOCK_PORT", "5000"))
    app.run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
