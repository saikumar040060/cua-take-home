"""In-memory data store for the mock 'Meridian Credit Union' back-office tool.

All data is synthetic. It intentionally *looks* like regulated financial data
(names, SSN fragments, account numbers) so the redaction layer has something
real to protect in logs/artifacts.

The first three members (10023, 10456, 10777) are fixed and load-bearing --
the existing test suite and recorded specs/artifacts reference their exact
member numbers, account numbers, and field values. Do not change them.
Everything from member 20001 up is generated (seeded, so it's stable across
restarts) to give a larger, more realistic population to record capabilities
against.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field


@dataclass
class Account:
    number: str
    product: str
    balance: str
    opened: str
    status: str = "OPEN"


@dataclass
class Card:
    number_masked: str
    kind: str  # "Debit" or "Credit"
    status: str = "ACTIVE"  # ACTIVE | LOCKED


@dataclass
class Transaction:
    date: str
    description: str
    amount: str  # signed, e.g. "-42.10" or "+500.00"


@dataclass
class Payee:
    name: str
    payee_id: str


@dataclass
class Member:
    member_no: str
    name: str
    ssn_last4: str
    dob: str
    phone: str
    address: str
    accounts: list[Account] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    transactions: dict[str, list[Transaction]] = field(default_factory=dict)
    payees: list[Payee] = field(default_factory=list)


MEMBERS: dict[str, Member] = {
    "10023": Member(
        member_no="10023",
        name="Marisol Vega",
        ssn_last4="4417",
        dob="03/14/1987",
        phone="(313) 555-0164",
        address="482 Fenkell Ave, Detroit, MI 48238",
        accounts=[
            Account("724401-S01", "Regular Savings", "$4,812.55", "06/02/2015"),
            Account("724401-C01", "Free Checking", "$1,209.18", "06/02/2015"),
        ],
    ),
    "10456": Member(
        member_no="10456",
        name="Dmitri Okafor",
        ssn_last4="8830",
        dob="11/29/1971",
        phone="(248) 555-0122",
        address="19 Larch Ct, Royal Oak, MI 48067",
        accounts=[
            Account("731958-S01", "Regular Savings", "$22,940.03", "01/17/2009"),
        ],
    ),
    "10777": Member(
        member_no="10777",
        name="Hannah Whitfield",
        ssn_last4="2201",
        dob="07/07/1996",
        phone="(734) 555-0189",
        address="880 Packard St Apt 3, Ann Arbor, MI 48104",
        accounts=[
            Account("744102-C01", "Free Checking", "$318.77", "09/30/2021"),
        ],
    ),
}

VALID_PRODUCTS = ["Holiday Club", "Money Market", "Vacation Club", "Youth Savings"]
VALID_LOAN_PURPOSES = ["Auto", "Home Improvement", "Debt Consolidation", "Personal"]
VALID_HOLD_REASONS = ["Suspected Fraud", "Legal / Levy", "Deceased Member", "Chargeback Pending"]
VALID_PAYEE_IDS = None  # set below, once payees exist

_ref_counter = itertools.count(1)
SUB_ACCOUNTS: list[dict] = []
LOAN_APPLICATIONS: list[dict] = []
BILL_PAYMENTS: list[dict] = []
ACCOUNT_HOLDS: list[dict] = []


# --------------------------------------------------------------------- #
# Synthetic member generation (seeded, stable across restarts)
# --------------------------------------------------------------------- #

_FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Amara", "Wei", "Fatima", "Hiro", "Priya",
    "Diego", "Elena", "Kwame", "Ingrid", "Santiago", "Yuki", "Noor", "Mateo", "Aisha",
    "Lars", "Chidi", "Zara", "Kenji", "Leilani", "Omar", "Ines", "Rafael", "Anya",
]
_LAST_NAMES = [
    "Vega", "Okafor", "Whitfield", "Kowalski", "Nguyen", "Patel", "Alvarez", "Muller",
    "Johansson", "Osei", "Rossi", "Dubois", "Tanaka", "Kim", "Silva", "Ivanov", "Haddad",
    "Costa", "Larsen", "Abara", "Petrov", "Santos", "Nakamura", "Reyes", "Brandt",
    "Okoye", "Lindqvist", "Bianchi", "Moreau", "Novak", "Adeyemi", "Sorensen", "Diallo",
]
_STREETS = [
    "Fenkell Ave", "Larch Ct", "Packard St", "Woodward Ave", "Grand River Ave",
    "Michigan Ave", "Jefferson Ave", "Gratiot Ave", "Livernois Ave", "Van Dyke Ave",
    "Harper Ave", "Mack Ave", "Cass Ave", "Trumbull St", "Rosa Parks Blvd",
]
_CITIES = [
    ("Detroit", "MI", "482"), ("Royal Oak", "MI", "480"), ("Ann Arbor", "MI", "481"),
    ("Dearborn", "MI", "481"), ("Warren", "MI", "480"), ("Sterling Heights", "MI", "483"),
    ("Livonia", "MI", "481"), ("Troy", "MI", "480"), ("Southfield", "MI", "480"),
    ("Pontiac", "MI", "483"),
]
_ACCOUNT_PRODUCTS = ["Regular Savings", "Free Checking", "Money Market", "Holiday Club"]
_TXN_DESCRIPTIONS = [
    "POS PURCHASE - GROCERY", "ACH DEPOSIT - PAYROLL", "ATM WITHDRAWAL",
    "ONLINE TRANSFER", "CHECK CARD PURCHASE", "MONTHLY DIVIDEND", "FEE - MAINTENANCE",
    "MOBILE DEPOSIT", "BILL PAYMENT", "POS PURCHASE - FUEL",
]


def _gen_members(count: int, *, start_no: int, seed: int) -> dict[str, Member]:
    rng = random.Random(seed)
    out: dict[str, Member] = {}
    for i in range(count):
        member_no = str(start_no + i)
        first = rng.choice(_FIRST_NAMES)
        last = rng.choice(_LAST_NAMES)
        city, state, zip_prefix = rng.choice(_CITIES)
        street_no = rng.randint(1, 9999)
        street = rng.choice(_STREETS)
        area_code = rng.choice(["313", "248", "734", "586", "810"])
        phone = f"({area_code}) 555-{rng.randint(0, 9999):04d}"
        ssn4 = f"{rng.randint(0, 9999):04d}"
        dob = f"{rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/{rng.randint(1950, 2005)}"
        zip_code = f"{zip_prefix}{rng.randint(10, 99)}"

        n_accounts = rng.randint(1, 3)
        products = rng.sample(_ACCOUNT_PRODUCTS, k=min(n_accounts, len(_ACCOUNT_PRODUCTS)))
        accounts = []
        base_acct_no = rng.randint(700000, 799999)
        for j, product in enumerate(products):
            balance = rng.uniform(4.0, 45000.0)
            opened_year = rng.randint(2005, 2025)
            accounts.append(
                Account(
                    number=f"{base_acct_no}-{'S' if 'Sav' in product or 'Money' in product or 'Club' in product else 'C'}{j:02d}",
                    product=product,
                    balance=f"${balance:,.2f}",
                    opened=f"{rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/{opened_year}",
                )
            )

        cards = []
        if rng.random() < 0.7:
            cards.append(Card(number_masked=f"**** **** **** {rng.randint(1000, 9999)}", kind="Debit"))
        if rng.random() < 0.3:
            cards.append(Card(number_masked=f"**** **** **** {rng.randint(1000, 9999)}", kind="Credit"))

        transactions: dict[str, list[Transaction]] = {}
        for acct in accounts:
            n_txn = rng.randint(0, 6)
            txns = []
            for _ in range(n_txn):
                amt = rng.uniform(-250.0, 800.0)
                sign = "+" if amt >= 0 else "-"
                txns.append(
                    Transaction(
                        date=f"{rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/2026",
                        description=rng.choice(_TXN_DESCRIPTIONS),
                        amount=f"{sign}{abs(amt):,.2f}",
                    )
                )
            transactions[acct.number] = txns

        payees = []
        if rng.random() < 0.5:
            payees.append(Payee(name=rng.choice(["City Utilities", "Statewide Insurance", "NetStream Cable", "Metro Gas Co"]), payee_id=f"PY{rng.randint(1000,9999)}"))

        out[member_no] = Member(
            member_no=member_no,
            name=f"{last}, {first}",
            ssn_last4=ssn4,
            dob=dob,
            phone=phone,
            address=f"{street_no} {street}, {city}, {state} {zip_code}",
            accounts=accounts,
            cards=cards,
            transactions=transactions,
            payees=payees,
        )
    return out


MEMBERS.update(_gen_members(120, start_no=20001, seed=42))

# Stable public-demo records.  The customer UI historically used these
# Meridian-style member and account numbers, but a hosted public demo must not
# depend on a private bank endpoint or its credentials.  Keeping equivalent,
# explicitly synthetic records in the bundled legacy app lets the exact same
# recorded browser capabilities run end to end in an isolated environment.
_PUBLIC_DEMO_MEMBERS = {
    "100987": Member(
        member_no="100987",
        name="Lee, Jordan",
        ssn_last4="0987",
        dob="04/18/1989",
        phone="(313) 555-0101",
        address="100 Demo Way, Detroit, MI 48201",
        accounts=[
            Account("100987-MMKT-11", "Money Market", "$8,420.17", "03/12/2018"),
            Account("100987-S0001-9", "Regular Savings", "$2,195.44", "03/12/2018"),
        ],
    ),
    "100234": Member(
        member_no="100234",
        name="Lovelace, Ada",
        ssn_last4="0234",
        dob="12/10/1990",
        phone="(248) 555-0102",
        address="234 Demo Way, Royal Oak, MI 48067",
        accounts=[
            Account("100234-S0001-6", "Regular Savings", "$5,730.28", "09/08/2016"),
            Account("100234-MMKT-16", "Money Market", "$14,006.91", "09/08/2016"),
        ],
    ),
    "101555": Member(
        member_no="101555",
        name="Patel, Priya",
        ssn_last4="1555",
        dob="07/22/1986",
        phone="(734) 555-0103",
        address="1555 Demo Way, Ann Arbor, MI 48104",
        accounts=[
            Account("101555-CERT-4", "Certificate", "$25,000.00", "01/15/2022"),
            Account("101555-S0001-5", "Regular Savings", "$3,882.63", "01/15/2022"),
        ],
    ),
    "102777": Member(
        member_no="102777",
        name="Okafor, Amara",
        ssn_last4="2777",
        dob="02/03/1979",
        phone="(586) 555-0104",
        address="2777 Demo Way, Warren, MI 48089",
        accounts=[
            Account("102777-MMKT-3", "Money Market", "$11,480.75", "05/20/2014"),
            Account("102777-MMKT-4", "Money Market", "$6,104.09", "05/20/2014"),
        ],
    ),
    "103001": Member(
        member_no="103001",
        name="Nguyen, Minh",
        ssn_last4="3001",
        dob="10/29/1994",
        phone="(810) 555-0105",
        address="3001 Demo Way, Troy, MI 48083",
        accounts=[
            Account("103001-MMKT-4", "Money Market", "$9,955.32", "08/11/2020"),
            Account("103001-MMKT-7", "Money Market", "$18,241.80", "08/11/2020"),
        ],
    ),
}

for _demo_member in _PUBLIC_DEMO_MEMBERS.values():
    for _account in _demo_member.accounts:
        _demo_member.transactions[_account.number] = [
            Transaction("08/28/2026", "ACH DEPOSIT - PAYROLL", "+1,250.00"),
            Transaction("08/27/2026", "POS PURCHASE - GROCERY", "-64.31"),
        ]
    MEMBERS[_demo_member.member_no] = _demo_member

# One deliberately zero-balance account, so close_account has a real success
# path to record/replay against, not just the nonzero-balance rejection.
MEMBERS["20001"].accounts.append(
    Account("712280-S03", "Free Checking", "$0.00", "01/01/2026")
)
MEMBERS["20001"].transactions["712280-S03"] = []


# --------------------------------------------------------------------- #
# Business operations
# --------------------------------------------------------------------- #


def create_sub_account(member_no: str, product: str, nickname: str, deposit: str) -> str:
    ref = f"SA-2026-{next(_ref_counter):04d}"
    SUB_ACCOUNTS.append(
        {
            "ref": ref,
            "member_no": member_no,
            "product": product,
            "nickname": nickname,
            "deposit": deposit,
        }
    )
    return ref


def find_account(member: Member, account_no: str) -> Account | None:
    return next((a for a in member.accounts if a.number == account_no), None)


def _parse_balance(balance_str: str) -> float:
    return float(balance_str.replace("$", "").replace(",", ""))


def _format_balance(value: float) -> str:
    return f"${value:,.2f}"


def transfer_funds(member: Member, from_no: str, to_no: str, amount: str) -> tuple[bool, str]:
    """Returns (ok, message_or_ref)."""
    from_acct = find_account(member, from_no)
    to_acct = find_account(member, to_no)
    if from_acct is None or to_acct is None:
        return False, "One or both accounts were not found on this member."
    if from_acct.status != "OPEN":
        return False, f"Source account {from_no} is not OPEN and cannot be debited."
    try:
        amt = float(str(amount).replace(",", "").replace("$", ""))
    except ValueError:
        return False, "Transfer amount must be a dollar amount."
    if amt <= 0:
        return False, "Transfer amount must be greater than zero."
    available = _parse_balance(from_acct.balance)
    if amt > available:
        return False, "Insufficient funds in the source account."
    from_acct.balance = _format_balance(available - amt)
    to_acct.balance = _format_balance(_parse_balance(to_acct.balance) + amt)
    ref = f"XFR-2026-{next(_ref_counter):04d}"
    return True, ref


def update_contact(member: Member, phone: str, address: str) -> None:
    if phone:
        member.phone = phone
    if address:
        member.address = address


def close_account(member: Member, account_no: str) -> tuple[bool, str]:
    acct = find_account(member, account_no)
    if acct is None:
        return False, "Account not found."
    if acct.status == "CLOSED":
        return False, "Account is already closed."
    if _parse_balance(acct.balance) > 0:
        return False, "Account balance must be $0.00 before closing."
    acct.status = "CLOSED"
    return True, f"Account {account_no} closed."


def toggle_card(member: Member, card_last4: str) -> tuple[bool, str]:
    card = next((c for c in member.cards if c.number_masked.endswith(card_last4)), None)
    if card is None:
        return False, "Card not found."
    card.status = "LOCKED" if card.status == "ACTIVE" else "ACTIVE"
    return True, card.status


def apply_loan(member: Member, purpose: str, amount: str) -> tuple[bool, str]:
    if purpose not in VALID_LOAN_PURPOSES:
        return False, "Loan purpose is required."
    try:
        amt = float(str(amount).replace(",", "").replace("$", ""))
    except ValueError:
        return False, "Loan amount must be a dollar amount."
    if amt < 500:
        return False, "Minimum loan amount is $500.00."
    if amt > 75000:
        return False, "Amount exceeds the maximum self-service loan limit of $75,000.00."
    ref = f"LN-2026-{next(_ref_counter):04d}"
    LOAN_APPLICATIONS.append(
        {"ref": ref, "member_no": member.member_no, "purpose": purpose, "amount": amount}
    )
    return True, ref


def pay_bill(member: Member, account_no: str, payee_id: str, amount: str) -> tuple[bool, str]:
    acct = find_account(member, account_no)
    payee = next((p for p in member.payees if p.payee_id == payee_id), None)
    if acct is None or payee is None:
        return False, "Account or payee not found."
    try:
        amt = float(str(amount).replace(",", "").replace("$", ""))
    except ValueError:
        return False, "Payment amount must be a dollar amount."
    if amt <= 0:
        return False, "Payment amount must be greater than zero."
    available = _parse_balance(acct.balance)
    if amt > available:
        return False, "Insufficient funds for this payment."
    acct.balance = _format_balance(available - amt)
    ref = f"BP-2026-{next(_ref_counter):04d}"
    BILL_PAYMENTS.append(
        {"ref": ref, "member_no": member.member_no, "account_no": account_no, "payee": payee.name, "amount": amount}
    )
    return True, ref


def place_hold(member: Member, account_no: str, reason: str) -> tuple[bool, str]:
    acct = find_account(member, account_no)
    if acct is None:
        return False, "Account not found."
    if reason not in VALID_HOLD_REASONS:
        return False, "A valid hold reason is required."
    if acct.status == "HOLD":
        return False, "Account already has an active hold."
    acct.status = "HOLD"
    ref = f"HD-2026-{next(_ref_counter):04d}"
    ACCOUNT_HOLDS.append(
        {"ref": ref, "member_no": member.member_no, "account_no": account_no, "reason": reason}
    )
    return True, ref
